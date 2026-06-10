# =============================================================================
# local_sim.py — Локальная симуляция арбитража (0 RPC)
# =============================================================================
#
# ПРОБЛЕМА:
#   flash_contract.simulate() вызывает simulateTwoWay() на контракте —
#   это 1 RPC на каждую найденную возможность.
#   При 5 возможностях за цикл = 5 лишних RPC.
#
# РЕШЕНИЕ:
#   Симулируем ЛОКАЛЬНО: те же вычисления что и контракт,
#   но в Python из уже полученных резервов.
#   На контракт обращаемся ТОЛЬКО для финального исполнения.
#
# ТОЧНОСТЬ:
#   Локальная симуляция использует ту же формулу AMM что и контракт.
#   Расхождение возможно если резервы изменились между сканом и исполнением.
#   Поэтому контракт всё равно проверяет minProfit — безопасность не теряется.
# =============================================================================

from dataclasses import dataclass
from typing import Optional, Dict, Tuple
from config import DEX, TOKENS, FLASH_MIN_PROFIT_BNB
from dex import calc_amount_out
from logger import get_logger

log = get_logger()


@dataclass
class LocalSimResult:
    """Результат локальной симуляции (аналог FlashSimResult)."""
    amount_out:               int
    profit_wei:               int
    profit_bnb:               float
    pancake_debt_wei:         int
    aave_premium_wei:         int
    profitable_after_pancake: bool
    profitable_after_aave:    bool
    recommended_provider:     str  # "AAVE", "PANCAKE", "NONE"

    def __str__(self):
        return (
            f"LocalSim | profit={self.profit_bnb:+.6f} BNB | "
            f"AAVE={'✅' if self.profitable_after_aave else '❌'} "
            f"PancakeSwap={'✅' if self.profitable_after_pancake else '❌'} | "
            f"best={self.recommended_provider}"
        )


def simulate_two_way_local(
    token_a_sym: str,
    token_b_sym: str,
    amount_in_wei: int,
    buy_dex: str,
    sell_dex: str,
    reserves: Dict[str, Dict[str, tuple]],
) -> Optional[LocalSimResult]:
    """
    Симулирует двусторонний арбитраж локально из резервов.
    Полный аналог контрактного simulateTwoWay(), но 0 RPC.

    Параметры:
        reserves: из multicall.get_last_reserves()
                  формат {"WBNB/USDT": {"PancakeSwap_V2": (r_a, r_b), ...}}
    """
    pair_key = f"{token_a_sym}/{token_b_sym}"
    pair_key_rev = f"{token_b_sym}/{token_a_sym}"

    buy_fee  = DEX.get(buy_dex, {}).get("fee_bps", 25)
    sell_fee = DEX.get(sell_dex, {}).get("fee_bps", 25)

    # Резервы для buy: A→B
    buy_reserves = None
    pair_data = reserves.get(pair_key, {})
    pair_data_rev = reserves.get(pair_key_rev, {})

    if buy_dex in pair_data:
        r_a, r_b = pair_data[buy_dex]
        buy_reserves = (r_a, r_b)
    elif buy_dex in pair_data_rev:
        r_b, r_a = pair_data_rev[buy_dex]
        buy_reserves = (r_a, r_b)

    if not buy_reserves:
        return None

    # Резервы для sell: B→A
    sell_reserves = None
    if sell_dex in pair_data:
        r_a, r_b = pair_data[sell_dex]
        sell_reserves = (r_b, r_a)  # Продаём B, получаем A
    elif sell_dex in pair_data_rev:
        r_b, r_a = pair_data_rev[sell_dex]
        sell_reserves = (r_b, r_a)

    if not sell_reserves:
        return None

    # Шаг 1: A → B на buy_dex
    r_in_buy, r_out_buy = buy_reserves
    received_b = calc_amount_out(amount_in_wei, r_in_buy, r_out_buy, buy_fee)
    if not received_b or received_b <= 0:
        return None

    # Шаг 2: B → A на sell_dex
    r_in_sell, r_out_sell = sell_reserves
    received_a = calc_amount_out(received_b, r_in_sell, r_out_sell, sell_fee)
    if not received_a or received_a <= 0:
        return None

    # Расчёт (идентичен контракту)
    profit_wei = received_a - amount_in_wei

    # PancakeSwap flash fee: debt = amount * 1000 / 997
    pancake_debt = (amount_in_wei * 1000 + 996) // 997

    # AAVE fee: premium = amount + amount * 5 / 10000
    aave_premium = amount_in_wei + (amount_in_wei * 5) // 10000

    profitable_pancake = received_a > pancake_debt
    profitable_aave    = received_a > aave_premium

    if profitable_aave:
        rec = "AAVE"
    elif profitable_pancake:
        rec = "PANCAKE"
    else:
        rec = "NONE"

    result = LocalSimResult(
        amount_out               = received_a,
        profit_wei               = profit_wei,
        profit_bnb               = profit_wei / 1e18,
        pancake_debt_wei         = pancake_debt,
        aave_premium_wei         = aave_premium,
        profitable_after_pancake = profitable_pancake,
        profitable_after_aave    = profitable_aave,
        recommended_provider     = rec,
    )

    log.debug(str(result))
    return result


def simulate_opportunity_local(opp, reserves: Dict) -> Optional[LocalSimResult]:
    """
    Обёртка: принимает ArbOpportunity, возвращает LocalSimResult.
    Drop-in замена для flash_client.simulate(opp).
    """
    amount_in_wei = int(opp.trade_amount * 1e18)

    return simulate_two_way_local(
        token_a_sym  = opp.token_a,
        token_b_sym  = opp.token_b,
        amount_in_wei = amount_in_wei,
        buy_dex       = opp.buy_dex,
        sell_dex      = opp.sell_dex,
        reserves      = reserves,
    )
