# =============================================================================
# chain.py — Подключение к BSC с оптимизациями V2
# =============================================================================
# Оптимизации:
#   1. Пул RPC с автоматическим failover
#   2. Подписка на новые блоки через WebSocket
#   3. Кэширование gas_price на время блока
#   4. Реалистичный GAS_LIMIT
# =============================================================================

import time
import threading
from web3 import Web3
try:
    from web3.middleware import POAMiddleware as POAMiddleware
except ImportError:
    from web3.middleware import geth_poa_middleware as POAMiddleware  # web3 <6.15
from config import (
    BSC_RPC_HTTP, BSC_RPC_HTTP_FALLBACKS, BSC_RPC_WS, BSC_RPC_WS_FALLBACKS,
    WALLET_ADDRESS, GAS_LIMIT, GAS_LIMIT_SEQ, GAS_PRICE_MULT,
    MAX_GAS_GWEI, CHAIN_ID, USE_DYNAMIC_GAS,
)
from logger import get_logger

log = get_logger()

# ── Кэш gas price ────────────────────────────────────────────────────────────
_gas_cache = {"price": 0, "timestamp": 0}
_GAS_CACHE_TTL = 3.0  # BSC блок каждые ~3с


def connect() -> Web3:
    """Подключается к BSC с автоматическим failover по пулу бесплатных RPC."""
    all_rpcs = [BSC_RPC_HTTP] + BSC_RPC_HTTP_FALLBACKS

    for rpc_url in all_rpcs:
        try:
            log.info(f"Подключение: {rpc_url[:50]}...")
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            w3.middleware_onion.inject(POAMiddleware, layer=0)

            if w3.is_connected():
                block = w3.eth.block_number
                log.success(f"Подключено к BSC: {rpc_url[:40]}... Блок #{block:,}")
                w3._rpc_url = rpc_url
                return w3
        except Exception as e:
            log.warning(f"RPC {rpc_url[:40]}... недоступен: {e}")
            continue

    raise ConnectionError("Ни один BSC RPC не доступен")


def reconnect(w3: Web3) -> Web3:
    """Переподключается к другому RPC при ошибках."""
    current = getattr(w3, '_rpc_url', BSC_RPC_HTTP)
    all_rpcs = [BSC_RPC_HTTP] + BSC_RPC_HTTP_FALLBACKS
    reordered = [r for r in all_rpcs if r != current] + [current]

    for rpc_url in reordered:
        try:
            new_w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            new_w3.middleware_onion.inject(POAMiddleware, layer=0)
            if new_w3.is_connected():
                log.success(f"Переподключено: {rpc_url[:40]}...")
                new_w3._rpc_url = rpc_url
                return new_w3
        except Exception:
            continue

    raise ConnectionError("Все RPC недоступны при переподключении")


def get_gas_price(w3: Web3) -> int:
    """Возвращает цену газа с кэшированием на время блока (~3с)."""
    global _gas_cache
    now = time.time()

    if now - _gas_cache["timestamp"] < _GAS_CACHE_TTL and _gas_cache["price"] > 0:
        return _gas_cache["price"]

    base_price = w3.eth.gas_price
    base_gwei  = base_price / 1e9

    if base_gwei > MAX_GAS_GWEI:
        raise ValueError(f"Газ слишком высокий: {base_gwei:.1f} Gwei > лимита {MAX_GAS_GWEI} Gwei")

    adjusted = int(base_price * GAS_PRICE_MULT)
    _gas_cache["price"]     = adjusted
    _gas_cache["timestamp"] = now

    log.debug(f"Газ: базовый={base_gwei:.2f} Gwei, итого={adjusted/1e9:.2f} Gwei (×{GAS_PRICE_MULT})")
    return adjusted


def get_bnb_balance(w3: Web3, address: str = None) -> float:
    addr = address or WALLET_ADDRESS
    if not addr:
        return 0.0
    balance_wei = w3.eth.get_balance(Web3.to_checksum_address(addr))
    return w3.from_wei(balance_wei, "ether")


def estimate_gas_cost_bnb(w3: Web3, is_flash: bool = True) -> float:
    """Оценка стоимости газа с реалистичным лимитом (300k вместо 500k)."""
    gas_price_wei = get_gas_price(w3)
    gas_limit = GAS_LIMIT if is_flash else GAS_LIMIT_SEQ
    cost_wei = gas_price_wei * gas_limit
    return float(w3.from_wei(cost_wei, "ether"))


def build_tx_params(w3: Web3, gas_limit: int = None) -> dict:
    """
    Собирает параметры транзакции.
    Nonce берётся из NonceManager (0 RPC) вместо get_transaction_count (1 RPC).
    """
    from nonce_manager import nonce_mgr

    if not nonce_mgr.is_synced:
        nonce_mgr.sync(w3)

    return {
        "from":     Web3.to_checksum_address(WALLET_ADDRESS),
        "gas":      gas_limit or GAS_LIMIT,
        "gasPrice": get_gas_price(w3),
        "nonce":    nonce_mgr.get_and_increment(),
        "chainId":  CHAIN_ID,
    }


# =============================================================================
# ПОДПИСКА НА НОВЫЕ БЛОКИ (WebSocket)
# =============================================================================

class BlockWatcher:
    """
    Мгновенная реакция на новый блок через WebSocket вместо polling.
    Выигрыш: 0мс задержка vs 0–2с при time.sleep().
    """

    def __init__(self):
        self._latest_block = 0
        self._event = threading.Event()
        self._running = False
        self._thread = None
        # Все WS-адреса для ротации: основной + fallback (без дублей)
        seen = set()
        self._ws_urls = []
        for url in [BSC_RPC_WS] + BSC_RPC_WS_FALLBACKS:
            if url not in seen:
                seen.add(url)
                self._ws_urls.append(url)
        self._ws_index = 0  # текущий индекс в пуле

    @property
    def latest_block(self) -> int:
        return self._latest_block

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()
        log.info("BlockWatcher: подписка на новые блоки через WebSocket")

    def stop(self):
        self._running = False

    def wait_new_block(self, timeout: float = 5.0) -> bool:
        """Блокирует до нового блока. True=получен, False=timeout."""
        self._event.clear()
        return self._event.wait(timeout=timeout)

    def _next_ws_url(self) -> str:
        """Возвращает следующий WS-адрес по кругу."""
        url = self._ws_urls[self._ws_index % len(self._ws_urls)]
        self._ws_index += 1
        return url

    def _run_ws(self):
        import json
        while self._running:
            ws_url = self._next_ws_url()
            try:
                import websocket
                ws = websocket.WebSocket()
                ws.connect(ws_url, timeout=10)

                subscribe = json.dumps({
                    "id": 1, "method": "eth_subscribe",
                    "params": ["newHeads"], "jsonrpc": "2.0",
                })
                ws.send(subscribe)
                resp = json.loads(ws.recv())

                if "result" not in resp:
                    log.warning(f"BlockWatcher: подписка не удалась ({ws_url[:40]}...): {resp}")
                    # Переключаемся на следующую ноду без паузы
                    continue

                log.info(f"BlockWatcher: подписка активна ({ws_url[:40]}...)")

                while self._running:
                    try:
                        msg = json.loads(ws.recv())
                        block_hex = msg.get("params", {}).get("result", {}).get("number")
                        if block_hex:
                            self._latest_block = int(block_hex, 16)
                            self._event.set()
                    except Exception:
                        break

                ws.close()
                log.warning(f"BlockWatcher: соединение потеряно ({ws_url[:40]}...), переключение...")

            except ImportError:
                log.warning(
                    "websocket-client не установлен. "
                    "pip install websocket-client --break-system-packages. "
                    "Переключаюсь на polling."
                )
                self._running = False
                return
            except Exception as e:
                log.warning(f"BlockWatcher WS ошибка ({ws_url[:40]}...): {e}. Следующая нода...")
                time.sleep(2)


block_watcher = BlockWatcher()
