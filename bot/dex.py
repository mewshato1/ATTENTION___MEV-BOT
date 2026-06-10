# =============================================================================
# dex.py — Чтение реальных цен из DEX-контрактов на BSC
# =============================================================================
# Как это работает:
#   1. Из фабрики DEX узнаём адрес пула (Pair) для двух токенов
#   2. Из пула читаем резервы (reserve0, reserve1) — сколько каждого токена в пуле
#   3. По резервам считаем цену: price = reserve1 / reserve0
#   4. Считаем сколько получим токенов при свапе с учётом комиссии DEX
# =============================================================================

from web3 import Web3
from functools import lru_cache
from typing import Optional, Tuple, Dict
from config import TOKENS, DEX
from abi import FACTORY_ABI, PAIR_ABI, ROUTER_ABI
from logger import get_logger

log = get_logger()

# Кэш адресов пар с TTL — автоочистка устаревших записей
# pair_cache = {"factory:tokenA:tokenB": ("pair_addr", timestamp)}
import time as _time
from config import TOKENS, DEX
try:
    from config import PAIR_CACHE_TTL
except ImportError:
    PAIR_CACHE_TTL = 3600

pair_cache: Dict[str, str] = {}
_pair_timestamps: Dict[str, float] = {}


def _cache_get(key: str) -> Optional[str]:
    """Получить из кэша с проверкой TTL."""
    if key in pair_cache:
        ts = _pair_timestamps.get(key, 0)
        if _time.time() - ts < PAIR_CACHE_TTL:
            return pair_cache[key]
        else:
            del pair_cache[key]
            del _pair_timestamps[key]
    return None


def _cache_set(key: str, value: str):
    """Записать в кэш с отметкой времени."""
    pair_cache[key] = value
    _pair_timestamps[key] = _time.time()


_pair_cache = pair_cache  # алиас для обратной совместимости с multicall.py


def get_pair_address(w3: Web3, factory_address: str, token_a: str, token_b: str) -> Optional[str]:
    """
    Запрашивает у фабрики адрес пула для пары токенов.
    Кэширует с TTL (по умолчанию 1 час).
    """
    cache_key = f"{factory_address}:{token_a}:{token_b}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        factory = w3.eth.contract(
            address=Web3.to_checksum_address(factory_address),
            abi=FACTORY_ABI
        )
        pair_addr = factory.functions.getPair(
            Web3.to_checksum_address(token_a),
            Web3.to_checksum_address(token_b)
        ).call()

        if pair_addr == "0x0000000000000000000000000000000000000000":
            return None

        _cache_set(cache_key, pair_addr)
        return pair_addr

    except Exception as e:
        log.debug(f"getPair ошибка ({factory_address[:10]}): {e}")
        return None


def get_reserves(w3: Web3, pair_address: str, token_a: str, token_b: str) -> Optional[Tuple[int, int]]:
    """
    Читает резервы пула и возвращает их в правильном порядке (tokenA, tokenB).

    Важный нюанс: контракт хранит токены в порядке адресов (token0 < token1 по адресу).
    Поэтому token0 может быть не тот токен который мы ожидаем — нужно проверить.
    """
    try:
        pair = w3.eth.contract(
            address=Web3.to_checksum_address(pair_address),
            abi=PAIR_ABI
        )
        reserves = pair.functions.getReserves().call()
        token0   = pair.functions.token0().call()

        r0, r1 = reserves[0], reserves[1]

        # Если token_a совпадает с token0 — порядок правильный
        # Если нет — переставляем
        if Web3.to_checksum_address(token_a) == Web3.to_checksum_address(token0):
            return r0, r1
        else:
            return r1, r0

    except Exception as e:
        log.debug(f"getReserves ошибка ({pair_address[:10]}): {e}")
        return None


def calc_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
    """
    Считает сколько токенов получим при свапе — формула UniswapV2.

    Формула: amountOut = (amountIn * (10000 - fee) * reserveOut)
                         / (reserveIn * 10000 + amountIn * (10000 - fee))

    fee_bps: комиссия в базисных пунктах (25 = 0.25%, 10 = 0.10%)
    """
    if reserve_in <= 0 or reserve_out <= 0 or amount_in <= 0:
        return 0

    fee_factor   = 10000 - fee_bps
    numerator    = amount_in * fee_factor * reserve_out
    denominator  = reserve_in * 10000 + amount_in * fee_factor
    return numerator // denominator


def get_price(w3: Web3, dex_name: str, token_a: str, token_b: str) -> Optional[float]:
    """
    Возвращает цену token_b в единицах token_a на указанном DEX.
    Например: get_price(w3, "PancakeSwap_V2", WBNB, USDT) → ~580.0 (1 BNB = 580 USDT)

    Это простая цена по резервам — не учитывает проскальзывание.
    Для малых сделок разница несущественна.
    """
    dex = DEX.get(dex_name)
    if not dex:
        log.error(f"DEX не найден в конфиге: {dex_name}")
        return None

    pair_addr = get_pair_address(w3, dex["factory"], token_a, token_b)
    if not pair_addr:
        log.debug(f"Пул {dex_name} {token_a[:8]}../{token_b[:8]}.. не существует")
        return None

    reserves = get_reserves(w3, pair_addr, token_a, token_b)
    if not reserves:
        return None

    r_a, r_b = reserves
    if r_a == 0:
        return None

    price = r_b / r_a
    log.debug(f"{dex_name} {token_a[:8]}/{token_b[:8]}: price={price:.6f}, R_a={r_a/1e18:.2f}, R_b={r_b/1e18:.2f}")
    return price


def get_amount_out_for_trade(
    w3: Web3,
    dex_name: str,
    token_a: str,
    token_b: str,
    amount_in_wei: int
) -> Optional[int]:
    """
    Считает точный выход токенов для конкретного размера сделки,
    учитывая комиссию DEX и формулу constant product AMM.

    Возвращает количество token_b в Wei.
    """
    dex = DEX.get(dex_name)
    if not dex:
        return None

    pair_addr = get_pair_address(w3, dex["factory"], token_a, token_b)
    if not pair_addr:
        return None

    reserves = get_reserves(w3, pair_addr, token_a, token_b)
    if not reserves:
        return None

    r_a, r_b = reserves
    return calc_amount_out(amount_in_wei, r_a, r_b, dex["fee_bps"])


def fetch_all_prices(w3: Web3, token_a_sym: str, token_b_sym: str) -> Dict[str, Optional[float]]:
    """
    Запрашивает цену пары на всех DEX из конфига.
    Возвращает словарь: {dex_name: price}
    """
    token_a = TOKENS.get(token_a_sym)
    token_b = TOKENS.get(token_b_sym)

    if not token_a or not token_b:
        log.error(f"Токены не найдены в конфиге: {token_a_sym}, {token_b_sym}")
        return {}

    results = {}
    for dex_name in DEX:
        price = get_price(w3, dex_name, token_a, token_b)
        results[dex_name] = price

    return results


# =============================================================================
# ОПТИМАЛЬНЫЙ РАЗМЕР СДЕЛКИ
# =============================================================================

def calc_optimal_trade_size(
    reserve_in_buy: int,
    reserve_out_buy: int,
    fee_bps_buy: int,
    reserve_in_sell: int,
    reserve_out_sell: int,
    fee_bps_sell: int,
    max_trade_wei: int,
) -> int:
    """
    Рассчитывает оптимальный размер сделки по формуле AMM.

    На тонких пулах большая сделка вызывает сильный price impact и съедает спред.
    Эта функция находит размер, максимизирующий прибыль.

    Формула основана на производной прибыли по amountIn = 0:
      optimal ≈ sqrt(R_buy * R_sell * fee_factor) - R_buy
    Упрощённая версия для быстрого расчёта.

    Возвращает оптимальный amountIn в Wei, ограниченный max_trade_wei.
    """
    import math

    if reserve_in_buy <= 0 or reserve_out_buy <= 0:
        return max_trade_wei
    if reserve_in_sell <= 0 or reserve_out_sell <= 0:
        return max_trade_wei

    fee_factor_buy  = (10000 - fee_bps_buy) / 10000
    fee_factor_sell = (10000 - fee_bps_sell) / 10000

    # Приближённый оптимум через геометрическое среднее резервов
    # Точная формула требует решения квадратного уравнения,
    # но для практических целей достаточно приближения:
    # optimal_in ≈ sqrt(r_in_buy * r_in_sell * fee_buy * fee_sell) - r_in_buy
    try:
        optimal = int(
            math.sqrt(
                float(reserve_in_buy)
                * float(reserve_in_sell)
                * fee_factor_buy
                * fee_factor_sell
            ) - reserve_in_buy
        )
    except (ValueError, OverflowError):
        return max_trade_wei

    if optimal <= 0:
        return max_trade_wei

    # Ограничиваем сверху
    return min(optimal, max_trade_wei)


def calc_amount_out_from_reserves(
    amount_in: int,
    reserve_in: int,
    reserve_out: int,
    fee_bps: int,
) -> int:
    """
    Рассчитывает amountOut по резервам без RPC (чистая математика).
    Идентична calc_amount_out, но с явным именем для использования в arbitrage.
    """
    return calc_amount_out(amount_in, reserve_in, reserve_out, fee_bps)
