# =============================================================================
# mempool.py — WebSocket мониторинг мемпула BSC
# =============================================================================
#
# Как работает мемпул:
#   Когда пользователь отправляет транзакцию — она сначала попадает в мемпул
#   (очередь ожидания). Валидаторы берут транзакции из мемпула и включают
#   в блок. Между появлением в мемпуле и включением в блок — окно 1-3 сек.
#
# Что мы делаем в этом окне:
#   1. Видим крупный свап на DEX (жертва)
#   2. Отправляем СВОЙ свап с БОЛЕЕ ВЫСОКИМ газом (front-run: встаём перед жертвой)
#   3. Жертва двигает цену своим свапом
#   4. Отправляем ОБРАТНЫЙ свап (back-run: продаём по новой цене)
#   → Итог: купили дёшево, продали дорого за счёт жертвы
#
# Это sandwich-атака. Этически спорно, технически легально.
#
# ВАЖНО: Требует WebSocket RPC (не HTTP). Публичные WSS медленные.
#         Для реальной работы нужен QuickNode/Alchemy WSS эндпоинт.
# =============================================================================

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable
from web3 import Web3
from web3.types import TxData
from config import BSC_RPC_WS, TOKENS, DEX, MIN_PROFIT_BNB, MAX_TRADE_BNB
from dex import calc_amount_out, get_reserves, get_pair_address
from logger import get_logger

log = get_logger()

# =============================================================================
# КОНСТАНТЫ
# =============================================================================

# Сигнатуры методов DEX-роутера (первые 4 байта calldata)
# Позволяют определить что транзакция делает свап — без декодирования всего calldata
SWAP_SIGNATURES = {
    "0x38ed1739": "swapExactTokensForTokens",
    "0x8803dbee": "swapTokensForExactTokens",
    "0x7ff36ab5": "swapExactETHForTokens",
    "0x4a25d94a": "swapTokensForExactETH",
    "0x18cbafe5": "swapExactTokensForETH",
    "0xfb3bdb41": "swapETHForExactTokens",
    "0x5c11d795": "swapExactTokensForTokensSupportingFeeOnTransferTokens",
    "0xb6f9de95": "swapExactETHForTokensSupportingFeeOnTransferTokens",
}

# Минимальный размер свапа для рассмотрения (в BNB)
# Мелкие транзакции не стоят внимания — профит не покроет газ
MIN_SWAP_BNB = 5.0

# Множитель газа для front-run (должны быть выше жертвы)
FRONTRUN_GAS_MULT = 1.15  # +15% к газу жертвы


# =============================================================================
# СТРУКТУРЫ ДАННЫХ
# =============================================================================

@dataclass
class PendingSwap:
    """Обнаруженный свап в мемпуле."""
    tx_hash:        str
    sender:         str
    router:         str
    dex_name:       str
    method:         str
    token_in:       str        # Адрес входящего токена
    token_out:      str        # Адрес исходящего токена
    amount_in:      int        # В Wei
    amount_out_min: int        # Минимальный выход (slippage жертвы)
    gas_price:      int        # Газ жертвы
    value_bnb:      float      # Размер свапа в BNB (приблизительно)
    detected_at:    float      # Время обнаружения (timestamp)

    def __str__(self):
        return (
            f"PENDING SWAP | {self.dex_name} | {self.method[:25]} | "
            f"in={self.amount_in/1e18:.3f} | "
            f"gas={self.gas_price/1e9:.1f} Gwei | "
            f"tx={self.tx_hash[:12]}..."
        )


@dataclass
class SandwichOpportunity:
    """Найденная возможность для sandwich-атаки."""
    victim_tx:       PendingSwap
    token_in:        str
    token_out:       str
    dex_name:        str
    frontrun_amount: int      # Сколько мы вкладываем в front-run (Wei)
    expected_profit: float    # Ожидаемая прибыль в BNB
    frontrun_gas:    int      # Газ для front-run (выше жертвы)

    def __str__(self):
        return (
            f"🥪 SANDWICH | {self.dex_name} | "
            f"victim={self.victim_tx.tx_hash[:12]} | "
            f"our_in={self.frontrun_amount/1e18:.3f} BNB | "
            f"est_profit={self.expected_profit:+.6f} BNB"
        )


# =============================================================================
# ДЕКОДИРОВАНИЕ ТРАНЗАКЦИЙ
# =============================================================================

# ABI для декодирования calldata свапов
SWAP_INPUT_ABI = {
    "swapExactTokensForTokens": [
        ("amountIn",      "uint256"),
        ("amountOutMin",  "uint256"),
        ("path",          "address[]"),
        ("to",            "address"),
        ("deadline",      "uint256"),
    ],
    "swapExactETHForTokens": [
        ("amountOutMin",  "uint256"),
        ("path",          "address[]"),
        ("to",            "address"),
        ("deadline",      "uint256"),
    ],
    "swapTokensForExactTokens": [
        ("amountOut",     "uint256"),
        ("amountInMax",   "uint256"),
        ("path",          "address[]"),
        ("to",            "address"),
        ("deadline",      "uint256"),
    ],
}


def decode_swap_calldata(w3: Web3, tx: TxData) -> Optional[dict]:
    """
    Декодирует calldata транзакции свапа.
    Возвращает словарь с параметрами или None если не свап.
    """
    if not tx.get("input") or len(tx["input"]) < 10:
        return None

    # Первые 4 байта = сигнатура метода
    selector = tx["input"][:10].lower()
    method   = SWAP_SIGNATURES.get(selector)
    if not method:
        return None

    # Определяем DEX по адресу роутера
    to_addr  = tx["to"].lower() if tx.get("to") else ""
    dex_name = None
    for name, info in DEX.items():
        if info["router"].lower() == to_addr:
            dex_name = name
            break

    if not dex_name:
        return None  # Не наш DEX

    # Декодируем параметры
    try:
        raw = bytes.fromhex(tx["input"][10:])

        if method == "swapExactTokensForTokens":
            # amountIn (32) + amountOutMin (32) + offset (32) + path_len (32) + path...
            amount_in     = int.from_bytes(raw[0:32],   "big")
            amount_out_min = int.from_bytes(raw[32:64], "big")
            # path — массив адресов
            path_offset   = int.from_bytes(raw[64:96],  "big")
            path_len      = int.from_bytes(raw[path_offset:path_offset+32], "big")
            path = []
            for i in range(path_len):
                start = path_offset + 32 + i * 32
                addr  = "0x" + raw[start+12:start+32].hex()
                path.append(Web3.to_checksum_address(addr))

            return {
                "method":         method,
                "dex_name":       dex_name,
                "amount_in":      amount_in,
                "amount_out_min": amount_out_min,
                "token_in":       path[0],
                "token_out":      path[-1],
                "path":           path,
            }

        elif method == "swapExactETHForTokens":
            # ETH-свап: amount_in = tx.value
            amount_out_min = int.from_bytes(raw[0:32], "big")
            path_offset    = int.from_bytes(raw[32:64], "big")
            path_len       = int.from_bytes(raw[path_offset:path_offset+32], "big")
            path = []
            for i in range(path_len):
                start = path_offset + 32 + i * 32
                addr  = "0x" + raw[start+12:start+32].hex()
                path.append(Web3.to_checksum_address(addr))

            return {
                "method":         method,
                "dex_name":       dex_name,
                "amount_in":      tx.get("value", 0),
                "amount_out_min": amount_out_min,
                "token_in":       path[0],
                "token_out":      path[-1],
                "path":           path,
            }

    except Exception as e:
        log.debug(f"Ошибка декодирования calldata {tx.get('hash','')}: {e}")
        return None

    return None


# =============================================================================
# АНАЛИЗ SANDWICH-ВОЗМОЖНОСТИ
# =============================================================================

def analyze_sandwich(
    w3: Web3,
    victim: PendingSwap,
) -> Optional[SandwichOpportunity]:
    """
    Анализирует можно ли сделать sandwich на этой транзакции.

    Логика:
    1. Получаем текущие резервы пула
    2. Симулируем front-run (наш свап перед жертвой)
    3. Симулируем свап жертвы (после нашего)
    4. Симулируем back-run (наш обратный свап)
    5. Считаем чистую прибыль
    """
    wbnb = TOKENS.get("WBNB")
    if not wbnb:
        return None

    # Упрощение: рассматриваем только свапы с WBNB в пути
    # (самые ликвидные пары, проще оценить размер)
    token_in  = victim.token_in
    token_out = victim.token_out
    victim_amount = victim.amount_in

    if victim_amount == 0:
        return None

    dex_info = DEX.get(victim.dex_name)
    if not dex_info:
        return None

    # Получаем резервы пула
    pair_addr = get_pair_address(w3, dex_info["factory"], token_in, token_out)
    if not pair_addr:
        return None

    reserves = get_reserves(w3, pair_addr, token_in, token_out)
    if not reserves:
        return None

    r_in, r_out = reserves
    fee_bps     = dex_info["fee_bps"]

    # Оптимальный размер front-run: примерно sqrt(r_in * victim_amount) / 2
    # Это эвристика — реальный оптимум требует численной оптимизации
    our_amount_in = min(
        int((r_in * victim_amount) ** 0.5 // 2),
        int(MAX_TRADE_BNB * 1e18)
    )

    if our_amount_in < int(0.01 * 1e18):  # Меньше 0.01 BNB — не стоит
        return None

    # --- Шаг 1: Наш front-run ---
    our_out1 = calc_amount_out(our_amount_in, r_in, r_out, fee_bps)
    # Обновляем резервы после нашего свапа
    r_in_1  = r_in  + our_amount_in
    r_out_1 = r_out - our_out1

    # --- Шаг 2: Свап жертвы (двигает цену ещё дальше) ---
    victim_out = calc_amount_out(victim_amount, r_in_1, r_out_1, fee_bps)
    r_in_2  = r_in_1  + victim_amount
    r_out_2 = r_out_1 - victim_out

    # --- Шаг 3: Наш back-run (продаём то что купили) ---
    our_in2  = our_out1  # Продаём всё что купили
    our_out2 = calc_amount_out(our_in2, r_out_2, r_in_2, fee_bps)

    # --- Считаем прибыль ---
    gross_profit = (our_out2 - our_amount_in) / 1e18
    # Газ: два свапа, более высокая цена
    gas_cost     = (victim.gas_price * FRONTRUN_GAS_MULT * 500_000 * 2) / 1e18
    net_profit   = gross_profit - gas_cost

    if net_profit < MIN_PROFIT_BNB:
        log.debug(
            f"Sandwich невыгоден: gross={gross_profit:.6f} "
            f"gas={gas_cost:.6f} net={net_profit:.6f} BNB"
        )
        return None

    frontrun_gas = int(victim.gas_price * FRONTRUN_GAS_MULT)

    opp = SandwichOpportunity(
        victim_tx       = victim,
        token_in        = token_in,
        token_out       = token_out,
        dex_name        = victim.dex_name,
        frontrun_amount = our_amount_in,
        expected_profit = net_profit,
        frontrun_gas    = frontrun_gas,
    )
    log.success(str(opp))
    return opp


# =============================================================================
# WEBSOCKET МОНИТОР
# =============================================================================

class MempoolMonitor:
    """
    Слушает мемпул через WebSocket и анализирует входящие транзакции.

    Использование:
        monitor = MempoolMonitor(w3)
        monitor.on_sandwich(my_callback)
        await monitor.run()
    """

    def __init__(self, w3_http: Web3):
        self.w3            = w3_http
        self._sandwich_cb: Optional[Callable] = None
        self._raw_cb:      Optional[Callable] = None
        self.stats = {
            "seen":       0,
            "swaps":      0,
            "sandwiches": 0,
        }

    def on_sandwich(self, callback: Callable[[SandwichOpportunity], Awaitable]):
        """Регистрирует callback который будет вызван при находке sandwich."""
        self._sandwich_cb = callback

    def on_raw_swap(self, callback: Callable[[PendingSwap], Awaitable]):
        """Регистрирует callback для любого обнаруженного свапа (для логирования)."""
        self._raw_cb = callback

    def _process_tx(self, tx_hash: str) -> Optional[PendingSwap]:
        """
        Получает и анализирует одну транзакцию из мемпула.
        Возвращает PendingSwap если это свап, иначе None.
        """
        try:
            tx = self.w3.eth.get_transaction(tx_hash)
        except Exception:
            return None

        if not tx or not tx.get("to"):
            return None

        self.stats["seen"] += 1

        # Декодируем calldata
        decoded = decode_swap_calldata(self.w3, tx)
        if not decoded:
            return None

        # Оцениваем размер в BNB
        wbnb = TOKENS.get("WBNB", "").lower()
        t_in = decoded["token_in"].lower()
        if t_in == wbnb:
            value_bnb = decoded["amount_in"] / 1e18
        else:
            # Грубая оценка через gas_price (не идеально, но быстро)
            value_bnb = tx.get("value", 0) / 1e18 or decoded["amount_in"] / 1e18

        if value_bnb < MIN_SWAP_BNB:
            log.debug(f"Свап слишком мал ({value_bnb:.2f} BNB) — пропуск")
            return None

        swap = PendingSwap(
            tx_hash        = tx_hash if isinstance(tx_hash, str) else tx_hash.hex(),
            sender         = tx.get("from", ""),
            router         = tx.get("to", ""),
            dex_name       = decoded["dex_name"],
            method         = decoded["method"],
            token_in       = decoded["token_in"],
            token_out      = decoded["token_out"],
            amount_in      = decoded["amount_in"],
            amount_out_min = decoded.get("amount_out_min", 0),
            gas_price      = tx.get("gasPrice", 0),
            value_bnb      = value_bnb,
            detected_at    = time.time(),
        )

        self.stats["swaps"] += 1
        log.info(str(swap))
        return swap

    async def _handle_pending_tx(self, tx_hash: str):
        """Обрабатывает одну pending-транзакцию."""
        swap = self._process_tx(tx_hash)
        if not swap:
            return

        # Вызываем raw callback (для логирования в UI)
        if self._raw_cb:
            try:
                await self._raw_cb(swap)
            except Exception as e:
                log.error(f"raw_cb ошибка: {e}")

        # Анализируем sandwich
        opp = analyze_sandwich(self.w3, swap)
        if not opp:
            return

        self.stats["sandwiches"] += 1

        if self._sandwich_cb:
            try:
                await self._sandwich_cb(opp)
            except Exception as e:
                log.error(f"sandwich_cb ошибка: {e}")

    async def run(self):
        """
        Основной цикл WebSocket.
        Подключается к BSC WSS и слушает pending транзакции.

        BSC не поддерживает eth_subscribe("newPendingTransactions") на всех нодах.
        Публичные WSS могут не давать мемпул. Нужен QuickNode или собственная нода.
        """
        log.info(f"Подключение к WebSocket: {BSC_RPC_WS}")

        try:
            from websockets import connect as ws_connect
        except ImportError:
            log.error("websockets не установлен. Запусти: pip install websockets")
            return

        reconnect_delay = 5

        while True:
            try:
                async with ws_connect(
                    BSC_RPC_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**20,
                ) as ws:
                    log.success("WebSocket подключён")
                    reconnect_delay = 5  # Сбрасываем задержку

                    # Подписываемся на новые pending транзакции
                    subscribe_msg = json.dumps({
                        "id":      1,
                        "method":  "eth_subscribe",
                        "params":  ["newPendingTransactions"],
                        "jsonrpc": "2.0",
                    })
                    await ws.send(subscribe_msg)

                    # Первый ответ — подтверждение подписки
                    response = json.loads(await ws.recv())
                    sub_id   = response.get("result")
                    if not sub_id:
                        log.error(f"Не удалось подписаться на мемпул: {response}")
                        return

                    log.info(f"Подписка на мемпул активна (id: {sub_id})")
                    log.info(f"Мин. размер свапа для анализа: {MIN_SWAP_BNB} BNB")

                    # Слушаем входящие транзакции
                    async for raw_msg in ws:
                        try:
                            msg     = json.loads(raw_msg)
                            tx_hash = msg.get("params", {}).get("result")

                            if tx_hash:
                                # Обрабатываем каждую tx в отдельной таске
                                # чтобы не блокировать получение следующих
                                asyncio.create_task(
                                    self._handle_pending_tx(tx_hash)
                                )
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            log.error(f"Ошибка обработки сообщения: {e}")

            except Exception as e:
                log.warning(f"WebSocket разорван: {e}. Переподключение через {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)  # Exponential backoff

    def print_stats(self):
        log.info(
            f"Мемпул статистика: "
            f"seen={self.stats['seen']} | "
            f"swaps={self.stats['swaps']} | "
            f"sandwiches={self.stats['sandwiches']}"
        )


# =============================================================================
# ТОЧКА ВХОДА ДЛЯ ИЗОЛИРОВАННОГО ЗАПУСКА
# =============================================================================

async def _main_standalone():
    """Запуск только мемпул-монитора без основного цикла арбитража."""
    from chain import connect
    w3 = connect()

    monitor = MempoolMonitor(w3)

    async def on_swap(swap: PendingSwap):
        pass  # Уже залогировано внутри монитора

    async def on_sandwich(opp: SandwichOpportunity):
        log.profit(f"SANDWICH НАЙДЕН: {opp}")

    monitor.on_raw_swap(on_swap)
    monitor.on_sandwich(on_sandwich)

    # Статистика каждые 60 секунд
    async def stats_loop():
        while True:
            await asyncio.sleep(60)
            monitor.print_stats()

    await asyncio.gather(
        monitor.run(),
        stats_loop(),
    )


if __name__ == "__main__":
    asyncio.run(_main_standalone())
