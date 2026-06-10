# =============================================================================
# executor.py — Исполнение арбитражных транзакций
# =============================================================================
# ВАЖНО: Этот модуль реально отправляет транзакции в блокчейн.
#        В режиме DRY_RUN=true — только симулирует, деньги не тратятся.
# =============================================================================

import time
from web3 import Web3
from config import (
    WALLET_ADDRESS, PRIVATE_KEY, DRY_RUN,
    SLIPPAGE_PCT, DEX, TOKENS, CHAIN_ID
)
from abi import ROUTER_ABI
from arbitrage import ArbOpportunity
from chain import build_tx_params, estimate_gas_cost_bnb
from logger import get_logger

log = get_logger()

# Дедлайн транзакции: сколько секунд с текущего момента транзакция действительна
TX_DEADLINE_SEC = 60


def calc_min_amount_out(
    amount_out: int,
    slippage_pct: float = None,
    amount_in: int = 0,
    reserve_in: int = 0,
) -> int:
    """
    Считает минимальный выход с учётом проскальзывания.
    
    Если amount_in и reserve_in переданы — используем динамический slippage.
    Иначе — фиксированный из конфига.
    """
    if amount_in > 0 and reserve_in > 0:
        try:
            from dynamic_slippage import calc_dynamic_slippage
            slippage_pct = calc_dynamic_slippage(amount_in, reserve_in)
        except ImportError:
            slippage_pct = slippage_pct or SLIPPAGE_PCT
    else:
        slippage_pct = slippage_pct or SLIPPAGE_PCT

    return int(amount_out * (1 - slippage_pct / 100))


def _calc_path_amount_out(
    w3: Web3,
    dex_name: str,
    path_syms: list,
    amount_in_wei: int,
) -> int:
    """Рассчитывает amountOut через цепочку пулов по пути из символов токенов."""
    from dex import get_amount_out_for_trade
    current = amount_in_wei
    for i in range(len(path_syms) - 1):
        a_addr = TOKENS[path_syms[i]]
        b_addr = TOKENS[path_syms[i + 1]]
        current = get_amount_out_for_trade(w3, dex_name, a_addr, b_addr, current)
        if not current:
            return 0
    return current


def execute_swap(
    w3: Web3,
    dex_name: str,
    path_addrs: list,
    amount_in_wei: int,
    amount_out_min_wei: int,
) -> dict:
    """
    Исполняет свап через роутер DEX.
    path_addrs — список адресов токенов [token_in, ..., token_out].
    Поддерживает и прямые (2 токена), и multihop (3+ токена) маршруты.
    Возвращает receipt транзакции (или заглушку в DRY_RUN).
    """
    dex = DEX[dex_name]
    router = w3.eth.contract(
        address=Web3.to_checksum_address(dex["router"]),
        abi=ROUTER_ABI
    )

    path      = [Web3.to_checksum_address(addr) for addr in path_addrs]
    deadline  = int(time.time()) + TX_DEADLINE_SEC
    tx_params = build_tx_params(w3)

    # Строим транзакцию свапа
    tx = router.functions.swapExactTokensForTokens(
        amount_in_wei,
        amount_out_min_wei,
        path,
        Web3.to_checksum_address(WALLET_ADDRESS),
        deadline
    ).build_transaction(tx_params)

    if DRY_RUN:
        route = "→".join(p[:8] + "..." for p in path)
        log.info(f"[DRY RUN] Свап {dex_name} [{route}]: "
                 f"in={amount_in_wei/1e18:.6f} → out_min={amount_out_min_wei/1e18:.6f} "
                 f"(реальная отправка отключена)")
        # Возвращаем фейковый receipt
        return {"status": 1, "transactionHash": b"\x00" * 32, "dry_run": True}

    # Подписываем и отправляем
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    # web3.py v6+: raw_transaction; v5: rawTransaction
    raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
    tx_hash = w3.eth.send_raw_transaction(raw_tx)
    log.info(f"Транзакция отправлена: 0x{tx_hash.hex()}")

    # Ждём подтверждения (макс 60 сек)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    return receipt


def execute_arbitrage(w3: Web3, opp: ArbOpportunity) -> bool:
    """
    Исполняет арбитраж: два последовательных свапа.
    Шаг 1: token_a → token_b на buy_dex (покупаем дёшево)
    Шаг 2: token_b → token_a на sell_dex (продаём дорого)

    Возвращает True если оба свапа прошли успешно.

    ОГРАНИЧЕНИЕ: Два отдельных свапа НЕ атомарны — есть риск что
    между ними цена изменится. Для полностью безопасного арбитража
    нужен смарт-контракт (flashloan + атомарный обмен).
    Это следующий этап разработки.
    """
    if not PRIVATE_KEY or not WALLET_ADDRESS:
        log.error("WALLET_ADDRESS или PRIVATE_KEY не настроены в .env")
        return False

    token_a_addr = TOKENS[opp.token_a]
    token_b_addr = TOKENS[opp.token_b]
    amount_in_wei = int(opp.trade_amount * 1e18)

    # Пути из opportunity (поддерживают multihop)
    buy_path_syms  = opp.buy_path  if opp.buy_path  else [opp.token_a, opp.token_b]
    sell_path_syms = opp.sell_path if opp.sell_path else [opp.token_b, opp.token_a]
    buy_path_addrs  = [TOKENS[s] for s in buy_path_syms]
    sell_path_addrs = [TOKENS[s] for s in sell_path_syms]

    buy_route  = "→".join(buy_path_syms)
    sell_route = "→".join(sell_path_syms)
    log.info(f"Исполнение арбитража: [{buy_route}] {opp.buy_dex} / [{sell_route}] {opp.sell_dex} "
             f"| net: {opp.net_profit:+.6f} BNB")

    # --- Свап 1: buy_path на buy_dex ---
    expected_b = _calc_path_amount_out(w3, opp.buy_dex, buy_path_syms, amount_in_wei)
    if not expected_b:
        log.error("Не удалось рассчитать выход для свапа 1")
        return False

    min_b = calc_min_amount_out(expected_b, SLIPPAGE_PCT)

    try:
        receipt1 = execute_swap(
            w3, opp.buy_dex, buy_path_addrs, amount_in_wei, min_b
        )
        if receipt1.get("status") != 1:
            log.error(f"Свап 1 упал! receipt: {receipt1}")
            return False
        log.success(f"Свап 1 ОК: {opp.token_a} → {opp.token_b} на {opp.buy_dex}")
    except Exception as e:
        log.error(f"Свап 1 исключение: {e}")
        return False

    # --- Свап 2: token_b → token_a на sell_dex ---
    # V2: Читаем РЕАЛЬНЫЙ баланс token_b после свапа 1 (вместо expected_b)
    # Реальный баланс может отличаться из-за проскальзывания
    from abi import PAIR_ABI  # нужен для ERC20 balanceOf
    try:
        token_b_contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_b_addr),
            abi=[{
                "name": "balanceOf", "type": "function",
                "stateMutability": "view",
                "inputs": [{"name": "account", "type": "address"}],
                "outputs": [{"name": "", "type": "uint256"}],
            }]
        )
        actual_b_balance = token_b_contract.functions.balanceOf(
            Web3.to_checksum_address(WALLET_ADDRESS)
        ).call()
        # Используем реальный баланс если он > 0
        swap2_input = actual_b_balance if actual_b_balance > 0 else expected_b
    except Exception:
        # Fallback на expected_b
        swap2_input = expected_b

    # Для sell_path используем реальный баланс как вход (уже в swap2_input)
    # Пересчитываем expected_a через весь sell путь
    expected_a = _calc_path_amount_out(w3, opp.sell_dex, sell_path_syms, swap2_input)
    if not expected_a:
        log.error("Не удалось рассчитать выход для свапа 2")
        return False

    min_a = calc_min_amount_out(expected_a, SLIPPAGE_PCT)

    try:
        receipt2 = execute_swap(
            w3, opp.sell_dex, sell_path_addrs, swap2_input, min_a
        )
        if receipt2.get("status") != 1:
            log.error(f"Свап 2 упал! receipt: {receipt2}")
            return False

        actual_profit = (expected_a - amount_in_wei) / 1e18 - opp.gas_cost
        log.profit(
            f"АРБИТРАЖ ВЫПОЛНЕН! {opp.token_a}/{opp.token_b} "
            f"net: {actual_profit:+.6f} BNB "
            f"({'DRY RUN' if DRY_RUN else 'REAL'})"
        )
        return True

    except Exception as e:
        log.error(f"Свап 2 исключение: {e}")
        return False
