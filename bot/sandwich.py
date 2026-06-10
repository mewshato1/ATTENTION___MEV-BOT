# =============================================================================
# sandwich.py — Исполнение sandwich-атак
# =============================================================================
# Получает SandwichOpportunity из mempool.py и исполняет:
#   1. Front-run: свап с повышенным газом (встаём ПЕРЕД жертвой)
#   2. Back-run:  обратный свап СРАЗУ после подтверждения жертвы
# =============================================================================

import asyncio
import time
from web3 import Web3
from config import (
    WALLET_ADDRESS, PRIVATE_KEY, DRY_RUN,
    TOKENS, DEX, CHAIN_ID, GAS_LIMIT, SLIPPAGE_PCT
)
from abi import ROUTER_ABI
from mempool import SandwichOpportunity
from chain import get_gas_price
from logger import get_logger

log = get_logger()

# Максимальное время ожидания попадания жертвы в блок (секунды)
VICTIM_WAIT_TIMEOUT = 30


def _build_swap_tx(
    w3:          Web3,
    router_addr: str,
    token_in:    str,
    token_out:   str,
    amount_in:   int,
    gas_price:   int,
    slippage:    float = 1.0,  # % проскальзывания для sandwich чуть шире
) -> dict:
    """Строит транзакцию свапа через роутер DEX."""
    router = w3.eth.contract(
        address=Web3.to_checksum_address(router_addr),
        abi=ROUTER_ABI
    )

    path = [
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
    ]

    # Получаем ожидаемый выход
    expected = router.functions.getAmountsOut(amount_in, path).call()
    amount_out_min = int(expected[1] * (1 - slippage / 100))
    deadline = int(time.time()) + 60

    # Апрувим токен если нужно
    from abi import ERC20_ABI
    token_contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_in),
        abi=ERC20_ABI
    )
    allowance = token_contract.functions.allowance(
        Web3.to_checksum_address(WALLET_ADDRESS),
        Web3.to_checksum_address(router_addr)
    ).call()
    if allowance < amount_in:
        log.debug(f"Апрув токена {token_in[:10]} для {router_addr[:10]}")
        # Апрув отправляем отдельно (здесь не реализовано для краткости)

    nonce = w3.eth.get_transaction_count(
        Web3.to_checksum_address(WALLET_ADDRESS), "pending"
    )

    tx = router.functions.swapExactTokensForTokens(
        amount_in,
        amount_out_min,
        path,
        Web3.to_checksum_address(WALLET_ADDRESS),
        deadline,
    ).build_transaction({
        "from":     Web3.to_checksum_address(WALLET_ADDRESS),
        "gas":      GAS_LIMIT,
        "gasPrice": gas_price,
        "nonce":    nonce,
        "chainId":  CHAIN_ID,
    })

    return tx


def _send_tx(w3: Web3, tx: dict) -> str:
    """Подписывает и отправляет транзакцию. Возвращает tx_hash."""
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    raw_tx = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw_tx)
    return tx_hash.hex()


async def execute_sandwich(w3: Web3, opp: SandwichOpportunity) -> bool:
    """
    Исполняет sandwich-атаку.

    Шаг 1 (front-run):
      - Отправляем свап с газом ВЫШЕ жертвы
      - Это гарантирует что наша транзакция попадёт в блок ПЕРЕД жертвой

    Шаг 2 (ждём жертву):
      - Ждём пока жертва подтвердится (она сдвигает цену в нашу пользу)

    Шаг 3 (back-run):
      - Продаём купленное по новой (выгодной) цене
    """
    if not PRIVATE_KEY or not WALLET_ADDRESS:
        log.error("Кошелёк не настроен — sandwich невозможен")
        return False

    dex_info   = DEX.get(opp.dex_name)
    router_addr = dex_info["router"]

    log.info(
        f"Начинаем sandwich: {opp.dex_name} | "
        f"victim={opp.victim_tx.tx_hash[:16]}... | "
        f"наш_вклад={opp.frontrun_amount/1e18:.4f} BNB | "
        f"est_profit={opp.expected_profit:.6f} BNB"
    )

    # ─────────────────────────────────────────────
    # ШАГ 1: FRONT-RUN
    # ─────────────────────────────────────────────

    if DRY_RUN:
        log.info(f"[DRY RUN] Front-run: {opp.token_in[:10]} → {opp.token_out[:10]} "
                 f"| amount={opp.frontrun_amount/1e18:.4f} "
                 f"| gas={opp.frontrun_gas/1e9:.1f} Gwei")
        frontrun_hash = "0xDRYRUN_FRONT_" + opp.victim_tx.tx_hash[2:16]
    else:
        try:
            tx = _build_swap_tx(
                w3,
                router_addr,
                opp.token_in,
                opp.token_out,
                opp.frontrun_amount,
                opp.frontrun_gas,
                slippage=1.0,
            )
            frontrun_hash = _send_tx(w3, tx)
            log.success(f"Front-run отправлен: {frontrun_hash}")
        except Exception as e:
            log.error(f"Front-run провалился: {e}")
            return False

    # ─────────────────────────────────────────────
    # ШАГ 2: ЖДЁМ ПОДТВЕРЖДЕНИЯ ЖЕРТВЫ
    # ─────────────────────────────────────────────

    log.info(f"Ждём жертву: {opp.victim_tx.tx_hash}")

    if DRY_RUN:
        await asyncio.sleep(2)  # Симулируем ожидание
        victim_confirmed = True
    else:
        start = time.time()
        victim_confirmed = False
        while time.time() - start < VICTIM_WAIT_TIMEOUT:
            try:
                receipt = w3.eth.get_transaction_receipt(opp.victim_tx.tx_hash)
                if receipt and receipt.get("status") == 1:
                    victim_confirmed = True
                    elapsed = time.time() - start
                    log.success(f"Жертва подтверждена за {elapsed:.1f}s")
                    break
            except Exception:
                pass
            await asyncio.sleep(0.3)

        if not victim_confirmed:
            log.warning(f"Жертва не подтверждена за {VICTIM_WAIT_TIMEOUT}s — back-run всё равно")

    # ─────────────────────────────────────────────
    # ШАГ 3: BACK-RUN
    # ─────────────────────────────────────────────

    # Продаём то что купили на front-run
    # amount = то что мы получили на шаге 1 (приблизительно)
    from dex import get_amount_out_for_trade
    received_b = get_amount_out_for_trade(
        w3, opp.dex_name, opp.token_in, opp.token_out, opp.frontrun_amount
    )

    if not received_b:
        log.error("Не удалось рассчитать выход для back-run")
        return False

    # Газ для back-run немного ниже front-run
    # (должен попасть в блок ПОСЛЕ жертвы, но в тот же или следующий)
    backrun_gas = int(opp.frontrun_gas * 0.95)

    if DRY_RUN:
        log.info(f"[DRY RUN] Back-run: {opp.token_out[:10]} → {opp.token_in[:10]} "
                 f"| amount={received_b/1e18:.4f} "
                 f"| gas={backrun_gas/1e9:.1f} Gwei")
        log.profit(
            f"SANDWICH ЗАВЕРШЁН (DRY RUN) | "
            f"Ожидаемая прибыль: {opp.expected_profit:+.6f} BNB"
        )
        return True
    else:
        try:
            tx = _build_swap_tx(
                w3,
                router_addr,
                opp.token_out,
                opp.token_in,
                received_b,
                backrun_gas,
                slippage=2.0,  # Шире — цена уже сдвинулась
            )
            backrun_hash = _send_tx(w3, tx)
            log.success(f"Back-run отправлен: {backrun_hash}")

            # Ждём подтверждения back-run
            receipt = w3.eth.wait_for_transaction_receipt(backrun_hash, timeout=30)
            if receipt["status"] == 1:
                log.profit(
                    f"SANDWICH ВЫПОЛНЕН! | "
                    f"front={frontrun_hash[:16]} | "
                    f"back={backrun_hash[:16]} | "
                    f"est_profit={opp.expected_profit:+.6f} BNB"
                )
                return True
            else:
                log.error(f"Back-run revert: {backrun_hash}")
                return False

        except Exception as e:
            log.error(f"Back-run провалился: {e}")
            return False
