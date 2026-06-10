# =============================================================================
# dynamic_slippage.py — Динамический расчёт slippage из price impact
# =============================================================================
#
# ПРОБЛЕМА:
#   Фиксированный SLIPPAGE_PCT=0.5% — это компромисс:
#   - На глубоких пулах (WBNB/USDT на PancakeSwap) реальный impact 0.01%
#     → ставим 0.5% → теряем 0.49% прибыли на amountOutMin
#   - На тонких пулах (экзотик на ApeSwap) реальный impact 3%
#     → ставим 0.5% → TX revert'ится
#
# РЕШЕНИЕ:
#   Считаем РЕАЛЬНЫЙ price impact из резервов:
#   impact = amountIn / (reserveIn + amountIn)
#   slippage = impact × 1.5 (запас)
#
# РЕЗУЛЬТАТ:
#   Глубокий пул: slippage = 0.015% × 1.5 = 0.02%  (экономим прибыль)
#   Тонкий пул:   slippage = 2.0% × 1.5 = 3.0%     (TX не revert'ится)
# =============================================================================

from typing import Optional
from logger import get_logger

log = get_logger()

# Множитель: slippage = price_impact × SAFETY_MULT
SAFETY_MULT = 1.5

# Абсолютные границы (не уходим в крайности)
MIN_SLIPPAGE_PCT = 0.05   # Минимум 0.05% (защита от MEV)
MAX_SLIPPAGE_PCT = 5.0    # Максимум 5% (если больше — пул слишком тонкий)


def calc_price_impact(
    amount_in: int,
    reserve_in: int,
) -> float:
    """
    Рассчитывает price impact в процентах.

    Формула AMM: impact ≈ amountIn / (reserveIn + amountIn) × 100
    Это приближение, но для практических целей достаточно точное.
    """
    if reserve_in <= 0 or amount_in <= 0:
        return 0.0

    impact = amount_in / (reserve_in + amount_in) * 100
    return impact


def calc_dynamic_slippage(
    amount_in: int,
    reserve_in: int,
    safety_mult: float = SAFETY_MULT,
) -> float:
    """
    Рассчитывает оптимальный slippage для конкретной сделки.

    Возвращает slippage в процентах (например 0.3 = 0.3%).
    """
    impact = calc_price_impact(amount_in, reserve_in)
    slippage = impact * safety_mult

    # Ограничиваем
    slippage = max(slippage, MIN_SLIPPAGE_PCT)
    slippage = min(slippage, MAX_SLIPPAGE_PCT)

    return round(slippage, 4)


def calc_min_amount_out_dynamic(
    expected_out: int,
    amount_in: int,
    reserve_in: int,
) -> int:
    """
    Рассчитывает amountOutMin с динамическим slippage.
    Drop-in замена для executor.calc_min_amount_out().
    """
    slippage = calc_dynamic_slippage(amount_in, reserve_in)
    min_out = int(expected_out * (1 - slippage / 100))
    return max(min_out, 1)  # Минимум 1 wei


def calc_slippage_factor(
    amount_in: int,
    reserve_in: int,
) -> int:
    """
    Рассчитывает slippageFactor для Solidity контракта.
    Формат контракта: 9950 = 0.5%, 9990 = 0.1%, 9999 = 0.01%

    slippageFactor = 10000 - (slippage_pct × 100)
    """
    slippage = calc_dynamic_slippage(amount_in, reserve_in)
    factor = int(10000 - slippage * 100)
    factor = max(factor, 9000)  # Минимум 10%
    factor = min(factor, 9999)  # Максимум 0.01%
    return factor
