# =============================================================================
# multicall.py — Батчинг RPC-вызовов через Multicall3
# =============================================================================
#
# ПРОБЛЕМА которую решаем:
#   Стандартный сканер: 6 пар × 3 DEX = 18 RPC-запросов последовательно
#   При задержке 50мс каждый → 18 × 50мс = 900мс на один цикл сканирования
#
# РЕШЕНИЕ — Multicall3:
#   Все 18 вызовов упаковываются в ОДИН запрос к ноде
#   Нода исполняет их параллельно и возвращает все результаты разом
#   18 × 50мс → 1 × 50мс = ускорение в 18 раз
#
# Multicall3 задеплоен на BSC по адресу:
#   0xcA11bde05977b3631167028862bE2a173976CA11
#
# Документация: https://github.com/mds1/multicall
# =============================================================================

from typing import List, Tuple, Optional, Dict, Any
from web3 import Web3
from web3.contract import Contract
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode

from config import MULTICALL3_ADDRESS, MULTICALL_BATCH_SIZE, TOKENS, DEX, DEX_V3, WATCH_PAIRS
from abi import MULTICALL3_ABI, FACTORY_ABI, PAIR_ABI
from dex import pair_cache as _pair_cache
from logger import get_logger

log = get_logger()

# =============================================================================
# БАЗОВЫЙ СЛОЙ: encode / decode вызовов
# =============================================================================

def _encode_get_pair(factory: str, token_a: str, token_b: str) -> bytes:
    """
    Кодирует вызов factory.getPair(tokenA, tokenB) в bytes.
    Selector getPair = keccak256("getPair(address,address)")[:4]
    """
    selector = bytes.fromhex("e6a43905")  # getPair(address,address)
    encoded  = abi_encode(
        ["address", "address"],
        [Web3.to_checksum_address(token_a), Web3.to_checksum_address(token_b)]
    )
    return selector + encoded


def _encode_get_reserves(pair: str) -> bytes:
    """
    Кодирует вызов pair.getReserves().
    Selector getReserves = keccak256("getReserves()")[:4]
    """
    return bytes.fromhex("0902f1ac")  # getReserves()


def _encode_token0(pair: str) -> bytes:
    """Кодирует вызов pair.token0()."""
    return bytes.fromhex("0dfe1681")  # token0()


def _decode_address(data: bytes) -> Optional[str]:
    """Декодирует bytes → address из возврата контракта."""
    if not data or len(data) < 32:
        return None
    try:
        (addr,) = abi_decode(["address"], data)
        return addr
    except Exception:
        return None


def _encode_v3_quote(token_in: str, token_out: str, amount_in: int, fee_pips: int) -> bytes:
    """
    Кодирует вызов QuoterV2.quoteExactInputSingle для PancakeSwap V3.
    Selector: keccak256("quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]

    Возвращает точный amountOut с учётом всех тиков (не виртуальные резервы).
    sqrtPriceLimitX96 = 0 означает отсутствие ограничения по цене.
    """
    selector = Web3.keccak(
        text="quoteExactInputSingle((address,address,uint256,uint24,uint160))"
    )[:4]
    params = abi_encode(
        ["(address,address,uint256,uint24,uint160)"],
        [(
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            amount_in,
            fee_pips,
            0,   # sqrtPriceLimitX96 = 0 → торгуем до конца
        )]
    )
    return selector + params


def _decode_v3_quote(data: bytes) -> Optional[int]:
    """
    Декодирует ответ quoteExactInputSingle → amountOut.
    Возвращает None если данные пустые или декодирование не удалось.
    """
    if not data or len(data) < 32:
        return None
    try:
        result = abi_decode(["uint256", "uint160", "uint32", "uint256"], data)
        amount_out = result[0]
        return amount_out if amount_out > 0 else None
    except Exception:
        return None


def _compute_bnb_rate(sym: str, reserves: Dict[str, Any]) -> float:
    """
    Сколько единиц `sym` равно 1 WBNB — по V2 резервам.
    Используется для расчёта amount_in в Quoter-вызовах.
    Не импортирует triangle.py чтобы избежать циклических импортов.
    """
    if sym == "WBNB":
        return 1.0
    for pair_key, dex_data in reserves.items():
        parts = pair_key.split("/")
        if len(parts) != 2:
            continue
        sym_a, sym_b = parts
        for dex_name, res in dex_data.items():
            if not res or len(res) < 2:
                continue
            r_a, r_b = res[0], res[1]
            if r_a <= 0 or r_b <= 0:
                continue
            if sym_a == sym and sym_b == "WBNB":
                return r_a / r_b   # units of sym per 1 WBNB
            if sym_a == "WBNB" and sym_b == sym:
                return r_b / r_a   # units of sym per 1 WBNB
    return 0.0


def _decode_reserves(data: bytes) -> Optional[Tuple[int, int]]:
    """Декодирует bytes → (reserve0, reserve1) из getReserves()."""
    if not data or len(data) < 96:
        return None
    try:
        r0, r1, _ = abi_decode(["uint112", "uint112", "uint32"], data)
        return r0, r1
    except Exception:
        return None


# =============================================================================
# MULTICALL КЛИЕНТ
# =============================================================================

class MulticallClient:
    """
    Клиент для батчинга view-вызовов через Multicall3.

    Пример использования:
        mc = MulticallClient(w3)

        # Добавляем вызовы
        mc.add(factory_addr, calldata_bytes, "my_call_label")

        # Исполняем всё за один HTTP-запрос
        results = mc.execute()
        data = results["my_call_label"]
    """

    def __init__(self, w3: Web3):
        self.w3       = w3
        self._calls:  List[Tuple[str, bytes, str]] = []  # (target, calldata, label)
        self._contract: Contract = w3.eth.contract(
            address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
            abi=MULTICALL3_ABI,
        )

    def add(self, target: str, calldata: bytes, label: str):
        """Добавляет вызов в батч."""
        self._calls.append((Web3.to_checksum_address(target), calldata, label))

    def clear(self):
        """Очищает накопленные вызовы."""
        self._calls = []

    def execute(self, use_racing: bool = False) -> Dict[str, Optional[bytes]]:
        """
        Отправляет все накопленные вызовы.
        
        use_racing=False (default): стандартный web3 — надёжнее на бесплатных нодах.
        use_racing=True: 3 ноды параллельно — для платных/быстрых нод.
        """
        if not self._calls:
            return {}

        if use_racing:
            try:
                return self._execute_racing()
            except Exception as e:
                log.debug(f"Racing execute failed ({e}), fallback на стандартный")

        return self._execute_standard()

    def _execute_standard(self) -> Dict[str, Optional[bytes]]:
        """Стандартный execute через web3 (одна нода)."""
        results = {}

        for batch_start in range(0, len(self._calls), MULTICALL_BATCH_SIZE):
            batch = self._calls[batch_start : batch_start + MULTICALL_BATCH_SIZE]

            calls_encoded = [
                (target, True, calldata)
                for target, calldata, _ in batch
            ]

            try:
                raw_results = self._contract.functions.aggregate3(
                    calls_encoded
                ).call()

                for i, (success, return_data) in enumerate(raw_results):
                    label = batch[i][2]
                    results[label] = return_data if success else None

            except Exception as e:
                log.error(f"Multicall батч {batch_start//MULTICALL_BATCH_SIZE + 1} упал: {e}")
                for _, _, label in batch:
                    results[label] = None

        return results

    def _execute_racing(self) -> Dict[str, Optional[bytes]]:
        """
        Execute через RPC racing — отправляет multicall на 3 ноды параллельно.
        Берёт первый ответ. Выигрыш: latency = min(node1, node2, node3).
        """
        from async_rpc import get_racer
        racer = get_racer()

        results = {}

        for batch_start in range(0, len(self._calls), MULTICALL_BATCH_SIZE):
            batch = self._calls[batch_start : batch_start + MULTICALL_BATCH_SIZE]

            calls_encoded = [
                (target, True, calldata)
                for target, calldata, _ in batch
            ]

            # Кодируем вызов aggregate3 вручную
            calldata_hex = self._contract.functions.aggregate3(
                calls_encoded
            )._encode_transaction_data()

            # Отправляем через racing (3 ноды параллельно)
            raw_result = racer.eth_call(
                to=MULTICALL3_ADDRESS,
                data=calldata_hex,
            )

            if raw_result is None:
                for _, _, label in batch:
                    results[label] = None
                continue

            # Декодируем ответ
            try:
                raw_bytes = bytes.fromhex(raw_result[2:]) if raw_result.startswith("0x") else bytes.fromhex(raw_result)
                decoded = abi_decode(
                    ["(bool,bytes)[]"],
                    raw_bytes
                )[0]

                for i, (success, return_data) in enumerate(decoded):
                    label = batch[i][2]
                    results[label] = return_data if success else None

            except Exception as e:
                log.error(f"Racing decode error: {e}")
                for _, _, label in batch:
                    results[label] = None

        return results

    def __len__(self):
        return len(self._calls)


# =============================================================================
# ВЫСОКОУРОВНЕВЫЕ ФУНКЦИИ
# =============================================================================

def fetch_pair_addresses_batch(
    w3:     Web3,
    pairs:  List[Tuple[str, str]],  # [(token_a_addr, token_b_addr), ...]
    dex_name: str,
) -> Dict[str, Optional[str]]:
    """
    Получает адреса пулов для всех пар на одном DEX — за один запрос.

    Без multicall: N запросов последовательно
    С multicall:   1 запрос, N результатов
    """
    dex_info = DEX.get(dex_name)
    if not dex_info:
        return {}

    factory = dex_info["factory"]
    mc      = MulticallClient(w3)

    for token_a, token_b in pairs:
        label    = f"{token_a}:{token_b}"
        calldata = _encode_get_pair(factory, token_a, token_b)
        mc.add(factory, calldata, label)

    raw = mc.execute()

    result = {}
    for token_a, token_b in pairs:
        label   = f"{token_a}:{token_b}"
        decoded = _decode_address(raw.get(label))
        if decoded and decoded != "0x0000000000000000000000000000000000000000":
            result[label] = decoded
        else:
            result[label] = None

    return result


def fetch_all_reserves_batch(
    w3:          Web3,
    pair_addrs:  List[Tuple[str, str, str, str]],
    # (pair_addr, token_a_addr, token_b_addr, dex_name)
) -> Dict[str, Optional[Tuple[int, int]]]:
    """
    Читает резервы и token0 для всех пулов за ДВА батча:
      Батч 1: getReserves() для всех пулов
      Батч 2: token0() для всех пулов
    Итого: 2 запроса вместо N*2.
    """
    mc_reserves = MulticallClient(w3)
    mc_token0   = MulticallClient(w3)

    for pair_addr, token_a, token_b, dex_name in pair_addrs:
        label = f"{pair_addr}:{token_a}:{token_b}"
        mc_reserves.add(pair_addr, _encode_get_reserves(pair_addr), label)
        mc_token0.add(pair_addr, _encode_token0(pair_addr), label + ":t0")

    raw_reserves = mc_reserves.execute()
    raw_token0   = mc_token0.execute()

    result = {}
    for pair_addr, token_a, token_b, dex_name in pair_addrs:
        label  = f"{pair_addr}:{token_a}:{token_b}"
        t0_lbl = label + ":t0"

        reserves = _decode_reserves(raw_reserves.get(label))
        token0   = _decode_address(raw_token0.get(t0_lbl))

        if not reserves or not token0:
            result[label] = None
            continue

        r0, r1 = reserves
        # Упорядочиваем резервы: r_a → token_a, r_b → token_b
        if Web3.to_checksum_address(token_a) == Web3.to_checksum_address(token0):
            result[label] = (r0, r1)
        else:
            result[label] = (r1, r0)

    return result


# =============================================================================
# ГЛАВНАЯ ФУНКЦИЯ: полное мультисканирование всех пар × всех DEX
# =============================================================================

def scan_all_prices_multicall(w3: Web3) -> Dict[str, Dict[str, Optional[float]]]:
    """
    V3: PairStore (0 RPC на getPair) + один батч reserves+token0.
    С PairStore: 1 RPC. Без: len(DEX)+1 RPC.
    Побочный эффект: обновляет _last_reserves.
    """
    import time
    t_start = time.perf_counter()

    watch_resolved = []
    for sym_a, sym_b in WATCH_PAIRS:
        addr_a = TOKENS.get(sym_a)
        addr_b = TOKENS.get(sym_b)
        if addr_a and addr_b:
            watch_resolved.append((sym_a, sym_b, addr_a, addr_b))

    # ── Шаг 1: Адреса пулов из PairStore (0 RPC) или fallback ────────────
    all_pair_infos = []
    used_store = False

    try:
        from pair_store import pair_store
        if pair_store.is_loaded:
            # V2 DEXes
            for dex_name in DEX:
                for sym_a, sym_b, addr_a, addr_b in watch_resolved:
                    pair_addr = pair_store.get(dex_name, sym_a, sym_b)
                    if pair_addr:
                        cache_key = f"{DEX[dex_name]['factory']}:{addr_a}:{addr_b}"
                        _pair_cache[cache_key] = pair_addr
                        all_pair_infos.append((pair_addr, addr_a, addr_b, dex_name, sym_a, sym_b))
            # V3 DEXes
            for dex_name in DEX_V3:
                for sym_a, sym_b, addr_a, addr_b in watch_resolved:
                    pair_addr = pair_store.get(dex_name, sym_a, sym_b)
                    if pair_addr:
                        all_pair_infos.append((pair_addr, addr_a, addr_b, dex_name, sym_a, sym_b))
            used_store = True
    except Exception:
        pass

    if not used_store:
        for dex_name in DEX:
            pairs_for_dex = [(a, b) for _, _, a, b in watch_resolved]
            pair_addrs = fetch_pair_addresses_batch(w3, pairs_for_dex, dex_name)
            for sym_a, sym_b, addr_a, addr_b in watch_resolved:
                label = f"{addr_a}:{addr_b}"
                pair_addr = pair_addrs.get(label)
                if pair_addr:
                    cache_key = f"{DEX[dex_name]['factory']}:{addr_a}:{addr_b}"
                    _pair_cache[cache_key] = pair_addr
                    all_pair_infos.append((pair_addr, addr_a, addr_b, dex_name, sym_a, sym_b))

    log.debug(f"Пулы: {len(all_pair_infos)} (store={used_store})")

    # ── Шаг 2: V2 резервы + token0 (только V2 DEX, V3 через Quoter отдельно) ──
    # token0() для пула НИКОГДА не меняется — кэшируем навсегда.
    global _token0_cache

    mc = MulticallClient(w3)
    needs_token0 = []

    for pair_addr, addr_a, addr_b, dex_name, sym_a, sym_b in all_pair_infos:
        if dex_name in DEX_V3:
            continue  # V3 не использует getReserves — обрабатывается Quoter ниже
        label = f"{pair_addr}:{addr_a}:{addr_b}"
        mc.add(pair_addr, _encode_get_reserves(pair_addr), label + ":res")
        if pair_addr not in _token0_cache:
            mc.add(pair_addr, _encode_token0(pair_addr), label + ":t0")
            needs_token0.append((pair_addr, label))

    if needs_token0:
        log.info(f"token0: {len(needs_token0)} новых запросов")

    # ── Параллельный запуск V3 Quoter вместе с V2 ────────────────────────────
    # V3 использует _last_reserves из ПРЕДЫДУЩЕГО блока для расчёта amount_in.
    # Погрешность 1 блок допустима — используется только для линейного масштабирования.
    # Параллельность: V2 (25мс) и V3 (170мс) идут одновременно → итог max(25,170)=170мс.
    import threading
    global _last_reserves
    raw_q: Dict[str, Optional[bytes]] = {}
    mc_q: Optional[MulticallClient] = None
    quoter_meta: List = []  # (label, pair_key, dex_name, amount_a, amount_b, is_fwd)

    if DEX_V3 and _last_reserves:  # пропускаем первый блок — нет предыдущих резервов
        try:
            from config import PANCAKE_V3_QUOTER as _pq, MAX_TRADE_BNB as _mtb
        except ImportError:
            _pq = None

        if _pq:
            mc_q = MulticallClient(w3)
            prev_reserves = dict(_last_reserves)  # snapshot предыдущего блока

            try:
                from pair_store import pair_store as _ps
                _ps_ok = _ps.is_loaded
            except Exception:
                _ps = None
                _ps_ok = False

            for sym_a, sym_b in WATCH_PAIRS:
                pair_key = f"{sym_a}/{sym_b}"
                addr_a = TOKENS.get(sym_a)
                addr_b = TOKENS.get(sym_b)
                if not addr_a or not addr_b:
                    continue
                bnb_rate_a = _compute_bnb_rate(sym_a, prev_reserves)
                bnb_rate_b = _compute_bnb_rate(sym_b, prev_reserves)
                if bnb_rate_a <= 0 or bnb_rate_b <= 0:
                    continue
                amount_a = int(_mtb * bnb_rate_a * 1e18)
                amount_b = int(_mtb * bnb_rate_b * 1e18)
                for dex_name, v3_info in DEX_V3.items():
                    # Пропускаем пулы которых нет в pair_store (не существуют on-chain)
                    if _ps_ok and not _ps.get(dex_name, sym_a, sym_b):
                        continue
                    fee_pips = v3_info["fee_pips"]
                    lbl_fwd = f"{pair_key}:{dex_name}:fwd"
                    lbl_rev = f"{pair_key}:{dex_name}:rev"
                    mc_q.add(_pq, _encode_v3_quote(addr_a, addr_b, amount_a, fee_pips), lbl_fwd)
                    mc_q.add(_pq, _encode_v3_quote(addr_b, addr_a, amount_b, fee_pips), lbl_rev)
                    quoter_meta.append((lbl_fwd, pair_key, dex_name, amount_a, amount_b, True))
                    quoter_meta.append((lbl_rev, pair_key, dex_name, amount_a, amount_b, False))

    def _fetch_v3() -> None:
        nonlocal raw_q
        if mc_q and len(mc_q) > 0:
            log.debug(f"V3 Quoter батч: {len(mc_q)} вызовов")
            raw_q = mc_q.execute(use_racing=True)

    t_v3 = threading.Thread(target=_fetch_v3, daemon=True)
    t_v3.start()

    # V2 execute в главном потоке — идёт параллельно с V3
    raw_results = mc.execute(use_racing=True) if len(mc) > 0 else {}

    for pair_addr, label in needs_token0:
        t0 = _decode_address(raw_results.get(label + ":t0"))
        if t0:
            _token0_cache[pair_addr] = t0

    # ── Шаг 3: V2 цены + сохранение резервов ──────────────────────────────
    prices: Dict[str, Dict[str, Optional[float]]] = {}
    _last_reserves = {}  # global объявлен выше (line ~456)

    for sym_a, sym_b in WATCH_PAIRS:
        prices[f"{sym_a}/{sym_b}"] = {}

    for pair_addr, addr_a, addr_b, dex_name, sym_a, sym_b in all_pair_infos:
        if dex_name in DEX_V3:
            continue  # V3 добавится в prices ниже через Quoter
        label     = f"{pair_addr}:{addr_a}:{addr_b}"
        pair_key  = f"{sym_a}/{sym_b}"
        token0    = _token0_cache.get(pair_addr)

        if not token0:
            prices[pair_key][dex_name] = None
            continue

        reserves = _decode_reserves(raw_results.get(label + ":res"))
        if not reserves:
            prices[pair_key][dex_name] = None
            continue

        r0, r1 = reserves
        token0_is_a = (Web3.to_checksum_address(addr_a) == Web3.to_checksum_address(token0))
        r_a, r_b = (r0, r1) if token0_is_a else (r1, r0)

        if r_a > 0:
            prices[pair_key][dex_name] = r_b / r_a
            if pair_key not in _last_reserves:
                _last_reserves[pair_key] = {}
            _last_reserves[pair_key][dex_name] = (r_a, r_b)
        else:
            prices[pair_key][dex_name] = None

    # ── Шаг 4: Обработка V3 Quoter результатов ────────────────────────────
    # Ждём завершения V3 потока (обычно уже завершён, т.к. V2 decode занимает ~5мс)
    t_v3.join()

    global _last_v3_quotes
    _last_v3_quotes = {}

    for (lbl, pair_key, dex_name, amount_a, amount_b, is_fwd) in quoter_meta:
        amount_out = _decode_v3_quote(raw_q.get(lbl))
        if not amount_out:
            continue

        if pair_key not in _last_v3_quotes:
            _last_v3_quotes[pair_key] = {}
        if dex_name not in _last_v3_quotes[pair_key]:
            _last_v3_quotes[pair_key][dex_name] = {
                "amount_a": amount_a,
                "amount_b": amount_b,
            }

        if is_fwd:
            if amount_a <= 0:
                continue
            effective_price = amount_out / amount_a

            # Санитарная проверка: если V3 эффективная цена отклоняется >4%
            # от медианы V2 — пул пуст в текущем тике (фантомная цена).
            v2_prices = [
                v for k, v in prices[pair_key].items()
                if v and k in DEX
            ]
            if v2_prices:
                v2_median = sorted(v2_prices)[len(v2_prices) // 2]
                if v2_median > 0:
                    deviation = abs(effective_price - v2_median) / v2_median
                    if deviation > 0.04:
                        log.debug(
                            f"V3 skip {pair_key} {dex_name}: "
                            f"price={effective_price:.4f} откл. "
                            f"{deviation*100:.1f}% от V2={v2_median:.4f}"
                        )
                        if pair_key in _last_v3_quotes and dex_name in _last_v3_quotes[pair_key]:
                            del _last_v3_quotes[pair_key][dex_name]
                        continue

            _last_v3_quotes[pair_key][dex_name]["fwd"] = amount_out
            prices[pair_key][dex_name] = effective_price
        else:
            _last_v3_quotes[pair_key][dex_name]["rev"] = amount_out

    t_elapsed = (time.perf_counter() - t_start) * 1000
    v3_count  = sum(
        1 for pq in _last_v3_quotes.values() for dq in pq.values() if dq.get("fwd")
    )
    rpc_calls = (1 if used_store else len(DEX) + 1) + (1 if DEX_V3 else 0)
    dex_count = len(DEX) + len(DEX_V3)
    log.info(
        f"Multicall: {len(WATCH_PAIRS)}x{dex_count} DEX | "
        f"{rpc_calls} RPC | {t_elapsed:.1f}ms | V3 quotes={v3_count} | store={used_store}"
    )

    return prices


# Хранилище последних V2 резервов — используется в arbitrage.py для локального расчёта
_last_reserves: Dict[str, Dict[str, tuple]] = {}

# Кэш token0 — НЕ меняется никогда, запрашиваем один раз за жизнь пула
# {pair_address: token0_address}
_token0_cache: Dict[str, str] = {}

# Котировки V3 из QuoterV2 — обновляются каждый блок вместе с _last_reserves.
# Формат:
# {
#   "WBNB/USDT": {
#     "PancakeSwap_V3_500": {
#       "fwd": 590_000000000000000000,  # amountOut USDT за amount_a WBNB
#       "rev": 1_683050000000000,        # amountOut WBNB за amount_b USDT
#       "amount_a": 500000000000000000,  # amount_in для fwd (0.5 WBNB)
#       "amount_b": 295_000000000000000000,  # amount_in для rev (295 USDT)
#     }
#   }
# }
_last_v3_quotes: Dict[str, Dict[str, dict]] = {}


def get_last_reserves() -> Dict[str, Dict[str, tuple]]:
    """
    Возвращает V2 резервы из последнего scan_all_prices_multicall().
    Формат: {"WBNB/USDT": {"PancakeSwap_V2": (r_a, r_b), ...}, ...}
    """
    return _last_reserves


def get_last_v3_quotes() -> Dict[str, Dict[str, dict]]:
    """
    Возвращает V3 Quoter результаты из последнего scan_all_prices_multicall().
    Используется в arbitrage.py вместо calc_amount_out для V3 DEXes.
    """
    return _last_v3_quotes


def scan_amounts_out_multicall(
    w3:           Web3,
    pair_reserves: Dict[str, Dict[str, Optional[float]]],
    amount_in_bnb: float,
) -> Dict[str, Dict[str, Optional[int]]]:
    """
    Считает точные выходы токенов для конкретного размера сделки
    на основе уже полученных резервов.

    Это чистая математика — дополнительных RPC не требует.
    Возвращает {pair_key: {dex_name: amount_out_wei}}
    """
    result: Dict[str, Dict[str, Optional[int]]] = {}
    amount_in_wei = int(amount_in_bnb * 1e18)

    for sym_a, sym_b in WATCH_PAIRS:
        pair_key = f"{sym_a}/{sym_b}"
        addr_a   = TOKENS.get(sym_a)
        addr_b   = TOKENS.get(sym_b)
        result[pair_key] = {}

        for dex_name, dex_info in DEX.items():
            factory   = dex_info["factory"]
            cache_key = f"{factory}:{addr_a}:{addr_b}"
            pair_addr = _pair_cache.get(cache_key)

            if not pair_addr:
                result[pair_key][dex_name] = None
                continue

            label    = f"{pair_addr}:{addr_a}:{addr_b}"
            # Используем резервы из scan_all_prices_multicall
            # Ищем по всем dex_infos — немного упрощённо
            price = pair_reserves.get(pair_key, {}).get(dex_name)
            if price is None:
                result[pair_key][dex_name] = None
                continue

            # Восстанавливаем приблизительные резервы из цены
            # (точные резервы нужно хранить отдельно — улучшение для следующей итерации)
            # Для оценки достаточно: amountOut ≈ amountIn × price × (1 - fee)
            fee_mult = (10000 - dex_info["fee_bps"]) / 10000
            approx_out = int(amount_in_wei * price * fee_mult)
            result[pair_key][dex_name] = approx_out

    return result
