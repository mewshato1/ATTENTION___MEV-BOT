# =============================================================================
# flash_contract.py — Python-обёртка для FlashArbitrage.sol
# =============================================================================
#
# Иерархия исполнения (выбирается автоматически):
#
#   1. flashAave()        — AAVE V3, fee 0.05%, нет нужды в своём капитале
#                           Требует: токен в пуле AAVE
#
#   2. flashPancake()     — PancakeSwap flashswap, fee ~0.3%
#                           Работает для любого токена с пулом на PancakeSwap
#
#   3. execute_via_contract()  — из старого contract.py, с собственным капиталом
#                           Fallback если флэшлоан недоступен
#
# Перед запуском добавь в .env:
#   FLASH_CONTRACT_ADDRESS=0x...  (из deployed_flash.json)
#   FLASH_PROVIDER=aave           (или pancake)
# =============================================================================

import os
import time
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Tuple, Dict
from web3 import Web3

from config import (
    TOKENS, DEX, WALLET_ADDRESS, PRIVATE_KEY, DRY_RUN, CHAIN_ID,
    FLASH_CONTRACT_ADDRESS, FLASH_PROVIDER, AAVE_POOL_ADDRESS,
    AAVE_SUPPORTED_TOKENS, FLASH_MIN_PROFIT_BNB, TRIANGLE_ARB_MIN_SPREAD,
    WATCH_PAIRS,
)
from chain import build_tx_params
from arbitrage import ArbOpportunity
from dex import get_pair_address
from logger import get_logger

log = get_logger()

# =============================================================================
# ABI FlashArbitrage контракта
# Только те функции которые вызываем из Python
# =============================================================================

FLASH_ABI = [
    # ── Точки входа ──────────────────────────────────────────────────────────
    {
        "name": "flashAave",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "tokenA",    "type": "address"},
            {"name": "tokenB",    "type": "address"},
            {"name": "flashAmount", "type": "uint256"},
            {"name": "buyDex",    "type": "string"},
            {"name": "sellDex",   "type": "string"},
            {"name": "minProfit", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "flashPancake",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "pairAddress", "type": "address"},
            {"name": "tokenA",      "type": "address"},
            {"name": "tokenB",      "type": "address"},
            {"name": "flashAmount", "type": "uint256"},
            {"name": "buyDex",      "type": "string"},
            {"name": "sellDex",     "type": "string"},
            {"name": "minProfit",   "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "flashAaveTriangle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "tokenA",      "type": "address"},
            {"name": "tokenB",      "type": "address"},
            {"name": "tokenC",      "type": "address"},
            {"name": "flashAmount", "type": "uint256"},
            {"name": "dex1",        "type": "string"},
            {"name": "dex2",        "type": "string"},
            {"name": "dex3",        "type": "string"},
            {"name": "minProfit",   "type": "uint256"},
        ],
        "outputs": [],
    },
    # ── View (симуляция, gas = 0) ─────────────────────────────────────────────
    {
        "name": "simulateTwoWay",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA",    "type": "address"},
            {"name": "tokenB",    "type": "address"},
            {"name": "amountIn",  "type": "uint256"},
            {"name": "buyDex",    "type": "string"},
            {"name": "sellDex",   "type": "string"},
        ],
        "outputs": [
            {"name": "amountOut",           "type": "uint256"},
            {"name": "profit",              "type": "int256"},
            {"name": "pancakeDebt",         "type": "uint256"},
            {"name": "aavePremium",         "type": "uint256"},
            {"name": "profitableAfterPancake", "type": "bool"},
            {"name": "profitableAfterAave",    "type": "bool"},
        ],
    },
    {
        "name": "simulateTriangle",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA",   "type": "address"},
            {"name": "tokenB",   "type": "address"},
            {"name": "tokenC",   "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "dex1",     "type": "string"},
            {"name": "dex2",     "type": "string"},
            {"name": "dex3",     "type": "string"},
        ],
        "outputs": [
            {"name": "finalAmount",          "type": "uint256"},
            {"name": "profit",               "type": "int256"},
            {"name": "isProfitableAfterAave","type": "bool"},
        ],
    },
    {
        "name": "compareProviders",
        "type": "function",
        "stateMutability": "pure",
        "inputs": [
            {"name": "token",  "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [
            {"name": "pancakeCost", "type": "uint256"},
            {"name": "aaveCost",    "type": "uint256"},
            {"name": "cheaper",     "type": "string"},
        ],
    },
    {
        "name": "balance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "token", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    # ── Управление ────────────────────────────────────────────────────────────
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "token",  "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "emergencySweep",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "tokens", "type": "address[]"}],
        "outputs": [],
    },
    {
        "name": "setPaused",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_p", "type": "bool"}],
        "outputs": [],
    },
    # ── Событие ───────────────────────────────────────────────────────────────
    {
        "name": "ArbitrageExecuted",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "provider",   "type": "uint8",   "indexed": True},
            {"name": "tokenA",     "type": "address",  "indexed": True},
            {"name": "tokenB",     "type": "address",  "indexed": False},
            {"name": "flashAmount","type": "uint256",  "indexed": False},
            {"name": "profit",     "type": "uint256",  "indexed": False},
            {"name": "timestamp",  "type": "uint256",  "indexed": False},
        ],
    },
]


# =============================================================================
# РЕЗУЛЬТАТ СИМУЛЯЦИИ
# =============================================================================

@dataclass
class FlashSimResult:
    """Результат on-chain симуляции FlashArbitrage."""
    amount_out:              int
    profit_wei:              int      # может быть отрицательным
    profit_bnb:              float
    pancake_debt_wei:        int      # долг если через PancakeSwap
    aave_premium_wei:        int      # долг если через AAVE
    profitable_after_pancake: bool
    profitable_after_aave:   bool
    recommended_provider:    str      # "AAVE" или "PANCAKE" или "NONE"

    def __str__(self):
        return (
            f"FlashSim | profit={self.profit_bnb:+.6f} BNB | "
            f"AAVE={'✅' if self.profitable_after_aave else '❌'} "
            f"PancakeSwap={'✅' if self.profitable_after_pancake else '❌'} | "
            f"best={self.recommended_provider}"
        )


# =============================================================================
# КЛИЕНТ
# =============================================================================

class FlashContractClient:
    """
    Основной клиент для взаимодействия с FlashArbitrage.sol.

    Использование:
        client = FlashContractClient(w3)
        result = client.simulate(opp)
        if result.profitable_after_aave:
            client.execute(opp, result)
    """

    def __init__(self, w3: Web3):
        self.w3 = w3
        self._contract = None

        if not FLASH_CONTRACT_ADDRESS:
            log.warning(
                "FLASH_CONTRACT_ADDRESS не задан. "
                "Задеплой FlashArbitrage: npx hardhat run scripts/deployFlash.js"
            )

    def _get_contract(self):
        if self._contract is None:
            if not FLASH_CONTRACT_ADDRESS:
                raise ValueError("FLASH_CONTRACT_ADDRESS не задан в .env")
            self._contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(FLASH_CONTRACT_ADDRESS),
                abi=FLASH_ABI,
            )
        return self._contract

    # ── Симуляция ────────────────────────────────────────────────────────────

    def simulate(self, opp: ArbOpportunity) -> Optional[FlashSimResult]:
        """
        Вызывает simulateTwoWay() на контракте — view, gas = 0.
        Возвращает точные цифры прибыли и рекомендацию провайдера.
        """
        c         = self._get_contract()
        token_a   = TOKENS.get(opp.token_a)
        token_b   = TOKENS.get(opp.token_b)
        amount_in = int(opp.trade_amount * 1e18)

        if not token_a or not token_b:
            log.error(f"Токены не найдены: {opp.token_a}, {opp.token_b}")
            return None

        try:
            result = c.functions.simulateTwoWay(
                Web3.to_checksum_address(token_a),
                Web3.to_checksum_address(token_b),
                amount_in,
                opp.buy_dex,
                opp.sell_dex,
            ).call()

            amount_out, profit_wei, pancake_debt, aave_premium, prof_pan, prof_aave = result
            profit_bnb = profit_wei / 1e18

            if prof_aave:
                rec = "AAVE"
            elif prof_pan:
                rec = "PANCAKE"
            else:
                rec = "NONE"

            sim = FlashSimResult(
                amount_out               = amount_out,
                profit_wei               = profit_wei,
                profit_bnb               = profit_bnb,
                pancake_debt_wei         = pancake_debt,
                aave_premium_wei         = aave_premium,
                profitable_after_pancake = prof_pan,
                profitable_after_aave    = prof_aave,
                recommended_provider     = rec,
            )
            log.debug(str(sim))
            return sim

        except Exception as e:
            log.error(f"simulate() ошибка: {e}")
            return None

    def simulate_triangle(
        self,
        token_a: str, token_b: str, token_c: str,
        amount_bnb: float,
        dex1: str, dex2: str, dex3: str,
    ) -> Optional[Tuple[float, bool]]:
        """
        Симулирует треугольный арбитраж A→B→C→A.
        Возвращает (profit_bnb, is_profitable_after_aave) или None.
        """
        c = self._get_contract()
        a = TOKENS.get(token_a)
        b = TOKENS.get(token_b)
        cc = TOKENS.get(token_c)
        if not all([a, b, cc]):
            return None
        try:
            final, profit_wei, profitable = c.functions.simulateTriangle(
                Web3.to_checksum_address(a),
                Web3.to_checksum_address(b),
                Web3.to_checksum_address(cc),
                int(amount_bnb * 1e18),
                dex1, dex2, dex3,
            ).call()
            return profit_wei / 1e18, profitable
        except Exception as e:
            log.debug(f"simulate_triangle(): {e}")
            return None

    def compare_providers(self, token_sym: str, amount_bnb: float) -> Optional[Dict]:
        """
        Спрашивает контракт: что дешевле для данного размера займа?
        Возвращает {'pancake_cost_bnb': float, 'aave_cost_bnb': float, 'cheaper': str}
        """
        c = self._get_contract()
        token_addr = TOKENS.get(token_sym)
        if not token_addr:
            return None
        try:
            pan_cost, aave_cost, cheaper = c.functions.compareProviders(
                Web3.to_checksum_address(token_addr),
                int(amount_bnb * 1e18),
            ).call()
            return {
                "pancake_cost_bnb": pan_cost / 1e18,
                "aave_cost_bnb":    aave_cost / 1e18,
                "cheaper":          cheaper,
            }
        except Exception as e:
            log.debug(f"compare_providers(): {e}")
            return None

    # ── Исполнение ───────────────────────────────────────────────────────────

    def execute(self, opp: ArbOpportunity, sim: FlashSimResult) -> bool:
        """
        Выбирает оптимальный провайдер и исполняет флэшлоан-арбитраж.

        Логика выбора:
          1. AAVE доступен и прибылен → flashAave()
          2. PancakeSwap прибылен → flashPancake()
          3. Ни то ни другое → отказываемся (убыток после fee)
        """
        if not PRIVATE_KEY or not WALLET_ADDRESS:
            log.error("WALLET_ADDRESS / PRIVATE_KEY не настроены")
            return False

        provider = self._choose_provider(opp, sim)
        if provider is None:
            log.warning(
                f"Ни один провайдер не даёт прибыли для "
                f"{opp.token_a}/{opp.token_b} — пропуск"
            )
            return False

        if DRY_RUN:
            log.info(
                f"[DRY RUN] Flash {provider} | "
                f"{opp.token_a}/{opp.token_b} | "
                f"{opp.buy_dex}→{opp.sell_dex} | "
                f"amount={opp.trade_amount} BNB | "
                f"sim_profit={sim.profit_bnb:+.6f} BNB"
            )
            return True

        if provider == "AAVE":
            return self._exec_flash_aave(opp, sim)
        else:
            return self._exec_flash_pancake(opp, sim)

    def _choose_provider(
        self, opp: ArbOpportunity, sim: FlashSimResult
    ) -> Optional[str]:
        """
        Выбирает провайдера флэшлоана.
        AAVE предпочтителен (fee в 6 раз ниже).
        """
        token_a_sym = opp.token_a

        # Если явно задан в конфиге
        if FLASH_PROVIDER == "pancake":
            return "PANCAKE" if sim.profitable_after_pancake else None
        if FLASH_PROVIDER == "aave":
            if token_a_sym in AAVE_SUPPORTED_TOKENS and sim.profitable_after_aave:
                return "AAVE"
            if sim.profitable_after_pancake:
                log.info(f"{token_a_sym} нет в AAVE пуле → используем PancakeSwap")
                return "PANCAKE"
            return None

        # Авто: выбираем дешевле и прибыльнее
        if sim.recommended_provider == "AAVE" and token_a_sym in AAVE_SUPPORTED_TOKENS:
            return "AAVE"
        if sim.profitable_after_pancake:
            return "PANCAKE"
        return None

    def _exec_flash_aave(self, opp: ArbOpportunity, sim: FlashSimResult) -> bool:
        """Исполняет flashAave() на контракте."""
        c         = self._get_contract()
        token_a   = Web3.to_checksum_address(TOKENS[opp.token_a])
        token_b   = Web3.to_checksum_address(TOKENS[opp.token_b])
        amount    = int(opp.trade_amount * 1e18)

        # Правильный расчёт min_profit:
        #   sim.profit_bnb   = amountOut - amountIn  (до вычета flash-fee)
        #   sim.aave_premium = amountIn + amountIn*5/10000  (общий долг)
        #   aave_fee         = aave_premium - amountIn  (только комиссия 0.05%)
        #   real_profit      = profit_wei - aave_fee  (прибыль ПОСЛЕ возврата долга)
        #   min_profit       = 80% от real_profit  (запас на колебания цены)
        aave_fee_wei  = sim.aave_premium_wei - amount            # только комиссия
        real_profit   = int(sim.profit_bnb * 1e18) - aave_fee_wei
        min_profit = max(
            int(real_profit * 0.8),
            int(FLASH_MIN_PROFIT_BNB * 1e18),
        )
        min_profit = max(min_profit, 0)

        log.info(
            f"flashAave | {opp.token_a}/{opp.token_b} | "
            f"{opp.buy_dex}→{opp.sell_dex} | "
            f"amount={opp.trade_amount} BNB | "
            f"min_profit={min_profit/1e18:.6f} BNB"
        )
        return self._send(
            c.functions.flashAave(
                token_a, token_b, amount,
                opp.buy_dex, opp.sell_dex,
                min_profit,
            )
        )

    def _exec_flash_pancake(self, opp: ArbOpportunity, sim: FlashSimResult) -> bool:
        """Исполняет flashPancake() на контракте. Ищет подходящий пул для займа."""
        pair_addr = self._find_pancake_pair(opp.token_a, opp.token_b)
        if not pair_addr:
            log.error(f"Пул PancakeSwap для {opp.token_a}/{opp.token_b} не найден")
            return False

        c         = self._get_contract()
        token_a   = Web3.to_checksum_address(TOKENS[opp.token_a])
        token_b   = Web3.to_checksum_address(TOKENS[opp.token_b])
        amount    = int(opp.trade_amount * 1e18)
        # Правильный расчёт min_profit:
        #   sim.pancake_debt = amountIn * 1000/997  (общий долг с fee ~0.3009%)
        #   pcs_fee          = pancake_debt - amountIn  (только комиссия)
        #   real_profit      = profit_wei - pcs_fee  (прибыль ПОСЛЕ возврата долга)
        #   min_profit       = 80% от real_profit
        pcs_fee_wei   = sim.pancake_debt_wei - amount            # только комиссия
        real_profit   = int(sim.profit_bnb * 1e18) - pcs_fee_wei
        min_profit = max(
            int(real_profit * 0.8),
            int(FLASH_MIN_PROFIT_BNB * 1e18),
        )
        min_profit = max(min_profit, 0)

        log.info(
            f"flashPancake | pair={pair_addr[:10]}... | "
            f"{opp.token_a}/{opp.token_b} | "
            f"{opp.buy_dex}→{opp.sell_dex} | "
            f"amount={opp.trade_amount} BNB | "
            f"min_profit={min_profit/1e18:.6f} BNB"
        )
        return self._send(
            c.functions.flashPancake(
                Web3.to_checksum_address(pair_addr),
                token_a, token_b, amount,
                opp.buy_dex, opp.sell_dex,
                min_profit,
            )
        )

    def execute_triangle(
        self,
        token_a: str, token_b: str, token_c: str,
        amount_bnb: float,
        dex1: str, dex2: str, dex3: str,
        min_profit_bnb: float = 0.0,
        known_profit_bnb: float = None,
    ) -> bool:
        """
        Исполняет трёхсторонний арбитраж через AAVE: A→B→C→A.
        Если known_profit_bnb передан (локальная оценка) — пропускает on-chain симуляцию.
        """
        if known_profit_bnb is not None:
            profit_bnb = known_profit_bnb
            log.info(
                f"Треугольный {token_a}→{token_b}→{token_c}: "
                f"profit={profit_bnb:+.6f} BNB (локальная оценка) → исполняем"
            )
        else:
            sim = self.simulate_triangle(token_a, token_b, token_c, amount_bnb, dex1, dex2, dex3)
            if not sim:
                log.error("Ошибка симуляции треугольного арбитража")
                return False

            profit_bnb, is_profitable = sim
            if not is_profitable:
                log.info(
                    f"Треугольный {token_a}→{token_b}→{token_c}: "
                    f"profit={profit_bnb:+.6f} BNB — убыточен"
                )
                return False

            log.info(
                f"Треугольный {token_a}→{token_b}→{token_c}: "
                f"profit={profit_bnb:+.6f} BNB → исполняем"
            )

        if DRY_RUN:
            log.info(f"[DRY RUN] flashAaveTriangle — отправка пропущена")
            return True

        c  = self._get_contract()
        ta = Web3.to_checksum_address(TOKENS[token_a])
        tb = Web3.to_checksum_address(TOKENS[token_b])
        tc = Web3.to_checksum_address(TOKENS[token_c])
        min_p = int(max(profit_bnb * 0.8, min_profit_bnb) * 1e18)

        return self._send(
            c.functions.flashAaveTriangle(
                ta, tb, tc,
                int(amount_bnb * 1e18),
                dex1, dex2, dex3,
                min_p,
            )
        )

    # ── Управление контрактом ────────────────────────────────────────────────

    def get_balances(self) -> Dict[str, float]:
        """Балансы всех токенов на контракте."""
        c = self._get_contract()
        balances = {}
        for sym, addr in TOKENS.items():
            try:
                wei = c.functions.balance(Web3.to_checksum_address(addr)).call()
                balances[sym] = wei / 1e18
            except Exception:
                balances[sym] = 0.0
        return balances

    def withdraw(self, token_sym: str, amount_bnb: float = 0.0) -> bool:
        """Выводит прибыль на кошелёк owner-а."""
        token_addr = TOKENS.get(token_sym)
        if not token_addr:
            log.error(f"Токен не найден: {token_sym}")
            return False

        if DRY_RUN:
            log.info(f"[DRY RUN] withdraw({token_sym}, {amount_bnb or 'ALL'})")
            return True

        c = self._get_contract()
        return self._send(
            c.functions.withdraw(
                Web3.to_checksum_address(token_addr),
                int(amount_bnb * 1e18),
            )
        )

    def emergency_sweep(self) -> bool:
        """Выводит ВСЕ токены с контракта (аварийный вывод)."""
        if DRY_RUN:
            log.info("[DRY RUN] emergencySweep")
            return True
        c     = self._get_contract()
        addrs = [Web3.to_checksum_address(a) for a in TOKENS.values()]
        return self._send(c.functions.emergencySweep(addrs))

    def set_paused(self, paused: bool) -> bool:
        """Ставит/снимает контракт с паузы."""
        if DRY_RUN:
            log.info(f"[DRY RUN] setPaused({paused})")
            return True
        c = self._get_contract()
        return self._send(c.functions.setPaused(paused))

    # ── Внутренние хелперы ───────────────────────────────────────────────────

    def _send(self, fn) -> bool:
        """Строит, подписывает и отправляет транзакцию. Ждёт receipt."""
        from nonce_manager import nonce_mgr
        try:
            tx_params = build_tx_params(self.w3)
            tx        = fn.build_transaction(tx_params)
            signed    = self.w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            raw_tx    = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash   = self.w3.eth.send_raw_transaction(raw_tx)
            log.info(f"TX отправлен: 0x{tx_hash.hex()}")

            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)

            if receipt["status"] == 1:
                self._parse_event(receipt)
                return True
            else:
                log.error(f"TX revert: 0x{tx_hash.hex()}")
                return False
        except Exception as e:
            err_str = str(e).lower()
            # Nonce ошибки — ресинхронизируем
            if "nonce" in err_str or "already known" in err_str or "replacement" in err_str:
                log.warning(f"Nonce ошибка: {e}. Ресинхронизация...")
                nonce_mgr.sync(self.w3)
            else:
                # Откатываем nonce если TX не ушла
                nonce_mgr.rollback()
            log.error(f"_send() ошибка: {e}")
            return False

    def _parse_event(self, receipt) -> None:
        """Парсит ArbitrageExecuted из логов и логирует прибыль."""
        try:
            c      = self._get_contract()
            events = c.events.ArbitrageExecuted().process_receipt(receipt)
            if events:
                ev       = events[0]["args"]
                provider = "AAVE" if ev["provider"] == 1 else "PANCAKE"
                profit   = ev["profit"] / 1e18
                log.profit(
                    f"FLASH АРБИТРАЖ | провайдер={provider} | "
                    f"прибыль={profit:.6f} BNB | "
                    f"TX=0x{receipt['transactionHash'].hex()[:16]}..."
                )
        except Exception:
            log.success(f"TX успешен (событие не распарсено)")

    def _find_pancake_pair(self, token_a_sym: str, token_b_sym: str) -> Optional[str]:
        """
        Находит адрес пула PancakeSwap для займа.
        Пробует сначала PancakeSwap V2, потом BiSwap.
        """
        token_a = TOKENS.get(token_a_sym)
        token_b = TOKENS.get(token_b_sym)
        if not token_a or not token_b:
            return None

        for dex_name, dex_info in DEX.items():
            pair = get_pair_address(self.w3, dex_info["factory"], token_a, token_b)
            if pair:
                log.debug(f"Пул для flashPancake: {dex_name} {pair}")
                return pair
        return None


# =============================================================================
# СКАНЕР ТРЕУГОЛЬНЫХ ВОЗМОЖНОСТЕЙ
# =============================================================================

# Все возможные треугольники из WATCH_PAIRS
# Генерируются автоматически из конфига
def _build_triangles() -> list:
    """Строит список треугольников из WATCH_PAIRS."""
    pairs_set = set()
    for a, b in WATCH_PAIRS:
        pairs_set.add((a, b))
        pairs_set.add((b, a))

    triangles = []
    symbols   = list(TOKENS.keys())

    for a in symbols:
        for b in symbols:
            if a == b: continue
            if (a, b) not in pairs_set: continue
            for c in symbols:
                if c in (a, b): continue
                if (b, c) in pairs_set and (c, a) in pairs_set:
                    triangles.append((a, b, c))

    return triangles

TRIANGLES = _build_triangles()


def scan_triangle_opportunities(
    client: FlashContractClient,
    amount_bnb: float,
    dex_names: list,
) -> list:
    """
    Сканирует треугольные арбитражные возможности.
    Возвращает список (token_a, token_b, token_c, dex1, dex2, dex3, profit_bnb).
    Сортирует по прибыли.
    """
    if not TRIANGLES:
        return []

    results = []
    for token_a, token_b, token_c in TRIANGLES:
        for d1 in dex_names:
            for d2 in dex_names:
                for d3 in dex_names:
                    sim = client.simulate_triangle(
                        token_a, token_b, token_c,
                        amount_bnb, d1, d2, d3
                    )
                    if sim and sim[1]:   # is_profitable
                        results.append((token_a, token_b, token_c, d1, d2, d3, sim[0]))

    results.sort(key=lambda x: x[6], reverse=True)
    return results
