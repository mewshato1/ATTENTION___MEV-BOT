# =============================================================================
# multihop.py — Поиск 2-хоп маршрутов для лучшего выхода
# =============================================================================
#
# ПРОБЛЕМА:
#   Бот проверяет только прямые пары: A→B на DEX1, B→A на DEX2.
#   Но иногда A→X→B на одном DEX даёт БОЛЬШЕ token_b чем прямой A→B.
#
# ПРИМЕР:
#   CAKE→USDT напрямую на BiSwap: 580 USDT (тонкий пул)
#   CAKE→WBNB→USDT на PancakeSwap: 585 USDT (через глубокие пулы)
#   Разница 5 USDT = ~0.86% → это дополнительная прибыль!
#
# КАК ЭТО РАБОТАЕТ В АРБИТРАЖЕ:
#   Стандарт: Buy A→B на DEX1, Sell B→A на DEX2
#   С multihop: Buy A→X→B на DEX1, Sell B→A на DEX2
#   Или: Buy A→B на DEX1, Sell B→X→A на DEX2
#
# РЕАЛИЗАЦИЯ:
#   Сканируем все промежуточные токены X для каждой пары.
#   Если A→X→B > A→B — заменяем маршрут.
#   Всё локально из резервов, 0 RPC.
# =============================================================================

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from config import TOKENS, DEX
from dex import calc_amount_out
from logger import get_logger

log = get_logger()

# Токены которые используются как промежуточные (хабы ликвидности)
HOP_TOKENS = ["WBNB", "USDT", "BUSD", "USDC"]


@dataclass
class HopRoute:
    """Маршрут с промежуточным токеном."""
    token_from:   str     # Исходный токен
    token_hop:    str     # Промежуточный
    token_to:     str     # Целевой
    dex_name:     str     # DEX
    amount_out:   int     # Выход в wei
    direct_out:   int     # Выход прямого маршрута (для сравнения)
    advantage_pct: float  # Насколько лучше прямого (%)

    def __str__(self):
        return (
            f"HOP {self.token_from}→{self.token_hop}→{self.token_to} "
            f"on {self.dex_name} | +{self.advantage_pct:.2f}% vs direct"
        )


def find_best_route(
    sym_from: str,
    sym_to: str,
    dex_name: str,
    amount_in_wei: int,
    reserves: Dict[str, Dict[str, tuple]],
) -> Tuple[int, Optional[str]]:
    """
    Находит лучший маршрут (прямой или через хаб).
    
    Возвращает (best_amount_out, hop_token_or_None).
    hop_token=None означает прямой маршрут лучше.
    """
    dex_info = DEX.get(dex_name)
    if not dex_info:
        return 0, None

    fee = dex_info["fee_bps"]

    # Прямой маршрут
    direct_out = _get_amount_out(reserves, sym_from, sym_to, dex_name, fee, amount_in_wei)

    best_out = direct_out
    best_hop = None

    # Проверяем каждый хаб
    for hop in HOP_TOKENS:
        if hop == sym_from or hop == sym_to:
            continue

        # Шаг 1: from → hop
        hop_mid = _get_amount_out(reserves, sym_from, hop, dex_name, fee, amount_in_wei)
        if not hop_mid or hop_mid <= 0:
            continue

        # Шаг 2: hop → to
        hop_out = _get_amount_out(reserves, hop, sym_to, dex_name, fee, hop_mid)
        if not hop_out or hop_out <= 0:
            continue

        if hop_out > best_out:
            best_out = hop_out
            best_hop = hop

    # Санитарная проверка: если хоп > 6× лучше прямого — прямой пул дренируется
    # нашей сделкой (слишком маленький для данного trade size).
    # Возвращаем 0 чтобы arbitrage пропустил этот DEX вместо использования фантомного маршрута.
    if best_hop and direct_out > 0 and best_out / direct_out > 6.0:
        return 0, None

    return best_out, best_hop


def scan_multihop_improvements(
    reserves: Dict[str, Dict[str, tuple]],
    pairs: List[Tuple[str, str]],
    amount_in_wei: int,
) -> List[HopRoute]:
    """
    Сканирует все пары на всех DEX: есть ли 2-хоп маршрут лучше прямого?
    Возвращает список найденных улучшений.
    """
    improvements = []

    for sym_a, sym_b in pairs:
        for dex_name in DEX:
            fee = DEX[dex_name]["fee_bps"]

            direct_out = _get_amount_out(reserves, sym_a, sym_b, dex_name, fee, amount_in_wei)
            if not direct_out or direct_out <= 0:
                continue

            best_out, best_hop = find_best_route(
                sym_a, sym_b, dex_name, amount_in_wei, reserves
            )

            if best_hop and best_out > direct_out:
                adv = (best_out - direct_out) / direct_out * 100
                if adv > 0.05 and adv < 200.0:  # Минимум 0.05%, максимум 200% (выше = phantom, масштаб-артефакт)
                    improvements.append(HopRoute(
                        token_from=sym_a,
                        token_hop=best_hop,
                        token_to=sym_b,
                        dex_name=dex_name,
                        amount_out=best_out,
                        direct_out=direct_out,
                        advantage_pct=round(adv, 3),
                    ))

    improvements.sort(key=lambda r: r.advantage_pct, reverse=True)

    if improvements:
        log.info(f"Multihop: найдено {len(improvements)} улучшенных маршрутов")
        for imp in improvements[:3]:
            log.debug(str(imp))

    return improvements


def _get_amount_out(
    reserves: Dict[str, Dict[str, tuple]],
    sym_from: str,
    sym_to: str,
    dex_name: str,
    fee_bps: int,
    amount_in: int,
) -> int:
    """Получает amountOut из кэша резервов."""
    # Прямой порядок
    pair_key = f"{sym_from}/{sym_to}"
    dex_res = reserves.get(pair_key, {}).get(dex_name)
    if dex_res:
        r_in, r_out = dex_res
        return calc_amount_out(amount_in, r_in, r_out, fee_bps)

    # Обратный порядок
    pair_key_rev = f"{sym_to}/{sym_from}"
    dex_res = reserves.get(pair_key_rev, {}).get(dex_name)
    if dex_res:
        r_to, r_from = dex_res
        return calc_amount_out(amount_in, r_from, r_to, fee_bps)

    return 0
