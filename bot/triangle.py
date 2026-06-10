# =============================================================================
# triangle.py — Локальный сканер треугольного арбитража (0 RPC)
# =============================================================================
#
# ПРОБЛЕМА (старый подход в flash_contract.py):
#   scan_triangle_opportunities() делает simulate_triangle() для каждой
#   комбинации: 6 токенов × 3³ DEX = десятки RPC-вызовов к ноде.
#   На бесплатных нодах это 5-20 секунд (!).
#
# РЕШЕНИЕ:
#   Резервы всех пулов уже получены через multicall (scan_all_prices_multicall).
#   Треугольный расчёт = три последовательных calc_amount_out().
#   Ноль RPC. Чистая математика. Микросекунды вместо секунд.
#
# ПРИМЕР ТРЕУГОЛЬНИКА:
#   WBNB → USDT (PancakeSwap) → CAKE (BiSwap) → WBNB (ApeSwap)
#   Если финальный WBNB > начальный + AAVE fee → прибыль!
#
# ИСПОЛЬЗОВАНИЕ:
#   from triangle import scan_triangles_local
#   results = scan_triangles_local(reserves, amount_bnb=0.5)
# =============================================================================

import time
from typing import List, Tuple, Dict, Optional
from dataclasses import dataclass
from config import TOKENS, DEX, WATCH_PAIRS, FLASH_MIN_PROFIT_BNB
from dex import calc_amount_out
from logger import get_logger

log = get_logger()


@dataclass
class TriangleOpportunity:
    """Найденная треугольная арбитражная возможность."""
    token_a:     str    # Стартовый/финальный токен (обычно WBNB)
    token_b:     str    # Промежуточный 1
    token_c:     str    # Промежуточный 2
    dex1:        str    # DEX для A→B
    dex2:        str    # DEX для B→C
    dex3:        str    # DEX для C→A
    amount_in:   float  # Размер сделки в token_a
    amount_out:  float  # Выход в token_a (до flash fee)
    gross_profit: float # Прибыль до flash fee
    aave_fee:    float  # Комиссия AAVE (0.05%)
    net_profit:  float  # Прибыль после flash fee
    profitable:  bool

    def __str__(self):
        sign = "✅" if self.profitable else "⚠️"
        return (
            f"{sign} TRIANGLE {self.token_a}→{self.token_b}→{self.token_c}→{self.token_a} | "
            f"{self.dex1}/{self.dex2}/{self.dex3} | "
            f"net={self.net_profit:+.6f} BNB"
        )


def _build_triangle_routes() -> List[Tuple[str, str, str]]:
    """
    Строит все возможные треугольники из WATCH_PAIRS.
    Треугольник (A, B, C) значит: существуют пары A/B, B/C, C/A.
    """
    pairs_set = set()
    for a, b in WATCH_PAIRS:
        pairs_set.add((a, b))
        pairs_set.add((b, a))

    triangles = []
    symbols = list(TOKENS.keys())

    for a in symbols:
        for b in symbols:
            if a == b:
                continue
            if (a, b) not in pairs_set:
                continue
            for c in symbols:
                if c in (a, b):
                    continue
                if (b, c) in pairs_set and (c, a) in pairs_set:
                    triangles.append((a, b, c))

    return triangles


# Кэшируем маршруты (не меняются между циклами)
_TRIANGLE_ROUTES = _build_triangle_routes()

# Near-miss: логируем лучший результат ниже порога раз в минуту
_near_miss_last_log: float = 0.0
_NEAR_MISS_LOG_INTERVAL = 60.0   # секунды
_NEAR_MISS_MIN_PCT      = 0.3    # логируем если профит > 30% от порога


def _get_bnb_rate(token_sym: str, reserves: Dict) -> float:
    """
    Возвращает сколько единиц token_sym стоит 1 WBNB.
    Используется для конвертации прибыли из token_a в BNB.
    Пример: USDT → ~600, WBNB → 1.0
    """
    if token_sym == "WBNB":
        return 1.0
    for pair_key, wbnb_is_a in [
        (f"WBNB/{token_sym}", True),
        (f"{token_sym}/WBNB", False),
    ]:
        dex_data = reserves.get(pair_key)
        if not dex_data:
            continue
        for r_a, r_b in dex_data.values():
            if r_a > 0 and r_b > 0:
                return (r_b / r_a) if wbnb_is_a else (r_a / r_b)
    return 0.0


def _get_reserves_for_pair(
    reserves: Dict[str, Dict[str, tuple]],
    sym_from: str,
    sym_to: str,
    dex_name: str,
) -> Optional[Tuple[int, int, int]]:
    """
    Ищет резервы для пары в обоих направлениях.
    Возвращает (reserve_from, reserve_to, fee_bps) или None.
    """
    dex_info = DEX.get(dex_name)
    if not dex_info:
        return None

    fee_bps = dex_info["fee_bps"]

    # Прямое направление: sym_from/sym_to
    pair_key = f"{sym_from}/{sym_to}"
    dex_reserves = reserves.get(pair_key, {}).get(dex_name)
    if dex_reserves:
        r_a, r_b = dex_reserves
        return r_a, r_b, fee_bps

    # Обратное направление: sym_to/sym_from
    pair_key_rev = f"{sym_to}/{sym_from}"
    dex_reserves = reserves.get(pair_key_rev, {}).get(dex_name)
    if dex_reserves:
        r_a, r_b = dex_reserves
        # r_a = резерв sym_to, r_b = резерв sym_from → swap
        return r_b, r_a, fee_bps

    return None


def scan_triangles_local(
    reserves: Dict[str, Dict[str, tuple]],
    amount_bnb: float = 0.5,
    min_profit_bnb: float = None,
) -> List[TriangleOpportunity]:
    """
    Сканирует ВСЕ треугольные арбитражные возможности ЛОКАЛЬНО.
    Использует резервы из multicall — ноль RPC вызовов.

    Параметры:
        reserves:        Резервы из multicall.get_last_reserves()
        amount_bnb:      Размер входа (будет конвертирован в wei)
        min_profit_bnb:  Порог прибыли после AAVE fee

    Возвращает список отсортированный по net_profit (лучшие первые).
    """
    if min_profit_bnb is None:
        min_profit_bnb = FLASH_MIN_PROFIT_BNB

    if not reserves:
        return []

    t_start = time.perf_counter()
    aave_fee_rate = 0.0005  # 0.05%
    dex_names = list(DEX.keys())

    results = []
    checked = 0

    for token_a, token_b, token_c in _TRIANGLE_ROUTES:
        # Конвертируем amount_bnb → реальное кол-во token_a.
        # Без этого прибыль в USDT/CAKE/etc. сравнивалась бы с BNB-порогом напрямую.
        bnb_rate = _get_bnb_rate(token_a, reserves)  # token_a per 1 WBNB
        if bnb_rate <= 0:
            continue
        amount_in_wei = int(amount_bnb * bnb_rate * 1e18)

        for d1 in dex_names:
            # A → B
            res1 = _get_reserves_for_pair(reserves, token_a, token_b, d1)
            if not res1:
                continue
            r_in1, r_out1, fee1 = res1
            received_b = calc_amount_out(amount_in_wei, r_in1, r_out1, fee1)
            if not received_b or received_b <= 0:
                continue

            for d2 in dex_names:
                # B → C
                res2 = _get_reserves_for_pair(reserves, token_b, token_c, d2)
                if not res2:
                    continue
                r_in2, r_out2, fee2 = res2
                received_c = calc_amount_out(received_b, r_in2, r_out2, fee2)
                if not received_c or received_c <= 0:
                    continue

                for d3 in dex_names:
                    checked += 1

                    # C → A
                    res3 = _get_reserves_for_pair(reserves, token_c, token_a, d3)
                    if not res3:
                        continue
                    r_in3, r_out3, fee3 = res3
                    received_a = calc_amount_out(received_c, r_in3, r_out3, fee3)
                    if not received_a or received_a <= 0:
                        continue

                    # Прибыль в token_a, затем конвертируем в BNB
                    gross_ta  = (received_a - amount_in_wei) / 1e18
                    fee_ta    = amount_in_wei / 1e18 * aave_fee_rate
                    net_ta    = gross_ta - fee_ta
                    net_bnb   = net_ta / bnb_rate  # всегда в BNB

                    if net_bnb > 0:
                        opp = TriangleOpportunity(
                            token_a=token_a,
                            token_b=token_b,
                            token_c=token_c,
                            dex1=d1,
                            dex2=d2,
                            dex3=d3,
                            amount_in=amount_bnb * bnb_rate,  # реальный размер займа в token_a
                            amount_out=received_a / 1e18,
                            gross_profit=gross_ta / bnb_rate,
                            aave_fee=fee_ta / bnb_rate,
                            net_profit=net_bnb,
                            profitable=net_bnb >= min_profit_bnb,
                        )
                        results.append(opp)

    results.sort(key=lambda o: o.net_profit, reverse=True)  # net_profit всегда в BNB

    t_elapsed = (time.perf_counter() - t_start) * 1000
    profitable_count = sum(1 for r in results if r.profitable)

    if results:
        log.info(
            f"Triangle scan: {checked} комбинаций | "
            f"{len(results)} прибыльных ({profitable_count} выше порога) | "
            f"{t_elapsed:.1f}ms | 0 RPC"
        )
        if results[0].profitable:
            log.profit(str(results[0]))
    else:
        log.debug(f"Triangle scan: {checked} проверено, 0 найдено | {t_elapsed:.1f}ms")

    # Near-miss: логируем лучший результат ниже порога (раз в минуту)
    global _near_miss_last_log
    near_miss_threshold = min_profit_bnb * _NEAR_MISS_MIN_PCT
    near_misses = [r for r in results if not r.profitable and r.net_profit >= near_miss_threshold]
    if near_misses and (time.time() - _near_miss_last_log) >= _NEAR_MISS_LOG_INTERVAL:
        best = near_misses[0]
        pct  = best.net_profit / min_profit_bnb * 100
        log.info(
            f"📍 Near-miss: {best.token_a}→{best.token_b}→{best.token_c} | "
            f"{best.dex1}/{best.dex2}/{best.dex3} | "
            f"net=+{best.net_profit:.6f} BNB ({pct:.0f}% от порога)"
        )
        _near_miss_last_log = time.time()

    return results
