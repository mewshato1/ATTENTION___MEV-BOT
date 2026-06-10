# =============================================================================
# contract.py — Python-обёртка для взаимодействия с MevArbitrage контрактом
# =============================================================================
# После деплоя контракта добавь в .env:
#   CONTRACT_ADDRESS=0x...адрес_контракта...
# =============================================================================

import os
import json
from web3 import Web3
from config import TOKENS, DEX, WALLET_ADDRESS, PRIVATE_KEY, DRY_RUN, CHAIN_ID
from chain import build_tx_params, estimate_gas_cost_bnb
from arbitrage import ArbOpportunity
from logger import get_logger

log = get_logger()

CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS", "")

# ABI только тех функций которые мы вызываем из Python
CONTRACT_ABI = [
    {
        "name": "executeArbitrage",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "tokenA",      "type": "address"},
            {"name": "tokenB",      "type": "address"},
            {"name": "amountIn",    "type": "uint256"},
            {"name": "buyDexName",  "type": "string"},
            {"name": "sellDexName", "type": "string"},
            {"name": "minProfit",   "type": "uint256"},
        ],
        "outputs": [{"name": "profit", "type": "uint256"}],
    },
    {
        "name": "executeFlashloanArbitrage",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "pairAddress",  "type": "address"},
            {"name": "tokenA",       "type": "address"},
            {"name": "tokenB",       "type": "address"},
            {"name": "flashAmount",  "type": "uint256"},
            {"name": "buyDexName",   "type": "string"},
            {"name": "sellDexName",  "type": "string"},
            {"name": "minProfit",    "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "simulateArbitrage",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA",       "type": "address"},
            {"name": "tokenB",       "type": "address"},
            {"name": "amountIn",     "type": "uint256"},
            {"name": "buyDexName",   "type": "string"},
            {"name": "sellDexName",  "type": "string"},
        ],
        "outputs": [
            {"name": "expectedOut",    "type": "uint256"},
            {"name": "expectedProfit", "type": "int256"},
            {"name": "isProfitable",   "type": "bool"},
        ],
    },
    {
        "name": "tokenBalance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "token", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
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
        "name": "setPaused",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_paused", "type": "bool"}],
        "outputs": [],
    },
    # События для парсинга логов
    {
        "name": "ArbitrageExecuted",
        "type": "event",
        "inputs": [
            {"name": "tokenA",     "type": "address", "indexed": True},
            {"name": "tokenB",     "type": "address", "indexed": True},
            {"name": "buyDex",     "type": "address", "indexed": False},
            {"name": "sellDex",    "type": "address", "indexed": False},
            {"name": "amountIn",   "type": "uint256", "indexed": False},
            {"name": "profit",     "type": "uint256", "indexed": False},
            {"name": "timestamp",  "type": "uint256", "indexed": False},
        ],
    },
]


def get_contract(w3: Web3):
    """Возвращает объект контракта для вызова функций."""
    if not CONTRACT_ADDRESS:
        raise ValueError(
            "CONTRACT_ADDRESS не задан в .env. "
            "Сначала задеплой контракт: node contracts/deploy.js"
        )
    return w3.eth.contract(
        address=Web3.to_checksum_address(CONTRACT_ADDRESS),
        abi=CONTRACT_ABI
    )


def simulate_on_chain(
    w3: Web3,
    opp: ArbOpportunity
) -> tuple[bool, float]:
    """
    Симулирует арбитраж через view-функцию контракта.
    Не тратит газ, не изменяет состояние.
    Возвращает (is_profitable, expected_profit_bnb).
    """
    contract = get_contract(w3)
    token_a  = TOKENS[opp.token_a]
    token_b  = TOKENS[opp.token_b]
    amount_in = int(opp.trade_amount * 1e18)

    try:
        result = contract.functions.simulateArbitrage(
            Web3.to_checksum_address(token_a),
            Web3.to_checksum_address(token_b),
            amount_in,
            opp.buy_dex,
            opp.sell_dex,
        ).call()

        expected_out, expected_profit, is_profitable = result
        profit_bnb = expected_profit / 1e18

        log.debug(
            f"On-chain симуляция {opp.token_a}/{opp.token_b}: "
            f"out={expected_out/1e18:.6f} profit={profit_bnb:+.6f} BNB"
        )
        return is_profitable, profit_bnb

    except Exception as e:
        log.error(f"Ошибка симуляции на контракте: {e}")
        return False, 0.0


def execute_via_contract(w3: Web3, opp: ArbOpportunity) -> bool:
    """
    Исполняет арбитраж через смарт-контракт.
    Один вызов = оба свапа атомарно = один набор газа.

    Если прибыли нет — контракт делает revert, деньги не тратятся.
    """
    if not PRIVATE_KEY or not WALLET_ADDRESS:
        log.error("PRIVATE_KEY или WALLET_ADDRESS не настроены")
        return False

    contract  = get_contract(w3)
    token_a   = TOKENS[opp.token_a]
    token_b   = TOKENS[opp.token_b]
    amount_in = int(opp.trade_amount * 1e18)

    # Минимальная прибыль которую принимаем (в Wei)
    # Ставим чуть ниже расчётной чтобы не получить revert из-за колебаний
    min_profit_wei = int(opp.net_profit * 0.8 * 1e18)

    log.info(
        f"Вызов контракта: {opp.token_a}/{opp.token_b} "
        f"| {opp.buy_dex} → {opp.sell_dex} "
        f"| in={opp.trade_amount} BNB "
        f"| min_profit={opp.net_profit * 0.8:.6f} BNB"
    )

    if DRY_RUN:
        log.info("[DRY RUN] Вызов executeArbitrage пропущен")
        return True

    try:
        tx_params = build_tx_params(w3)
        tx = contract.functions.executeArbitrage(
            Web3.to_checksum_address(token_a),
            Web3.to_checksum_address(token_b),
            amount_in,
            opp.buy_dex,
            opp.sell_dex,
            min_profit_wei,
        ).build_transaction(tx_params)

        signed  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        raw_tx  = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
        log.info(f"TX отправлен: 0x{tx_hash.hex()}")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] == 1:
            # Парсим событие ArbitrageExecuted из логов
            try:
                events = contract.events.ArbitrageExecuted().process_receipt(receipt)
                if events:
                    ev = events[0]["args"]
                    actual_profit = ev["profit"] / 1e18
                    log.profit(
                        f"КОНТРАКТ: Арбитраж выполнен! "
                        f"Прибыль: {actual_profit:.6f} BNB | "
                        f"TX: 0x{tx_hash.hex()[:16]}..."
                    )
            except Exception:
                log.success(f"TX успешен: 0x{tx_hash.hex()}")
            return True
        else:
            log.error(f"TX revert: 0x{tx_hash.hex()}")
            return False

    except Exception as e:
        log.error(f"Ошибка вызова контракта: {e}")
        return False


def get_contract_balances(w3: Web3) -> dict:
    """Возвращает балансы всех токенов на контракте."""
    contract = get_contract(w3)
    balances = {}
    for symbol, addr in TOKENS.items():
        try:
            bal_wei = contract.functions.tokenBalance(
                Web3.to_checksum_address(addr)
            ).call()
            balances[symbol] = bal_wei / 1e18
        except Exception:
            balances[symbol] = 0.0
    return balances


def withdraw_profit(w3: Web3, token_symbol: str, amount: float = 0) -> bool:
    """Выводит прибыль с контракта на кошелёк owner-а."""
    contract  = get_contract(w3)
    token_addr = TOKENS.get(token_symbol)
    if not token_addr:
        log.error(f"Токен не найден: {token_symbol}")
        return False

    amount_wei = int(amount * 1e18)  # 0 = вывести всё

    if DRY_RUN:
        log.info(f"[DRY RUN] withdraw({token_symbol}, {amount or 'ALL'})")
        return True

    try:
        tx_params = build_tx_params(w3)
        tx = contract.functions.withdraw(
            Web3.to_checksum_address(token_addr),
            amount_wei
        ).build_transaction(tx_params)

        signed  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        raw_tx  = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] == 1:
            log.success(f"Вывод {token_symbol} успешен")
            return True
        else:
            log.error("Вывод не удался")
            return False

    except Exception as e:
        log.error(f"Ошибка вывода: {e}")
        return False
