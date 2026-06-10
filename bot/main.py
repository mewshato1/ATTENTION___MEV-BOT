# =============================================================================
# main.py — Главный цикл MEV-бота
# =============================================================================
#
# Иерархия исполнения (выбирается автоматически):
#
#   FLASH_CONTRACT_ADDRESS задан →
#     1. simulateTwoWay() on-chain (view, gas=0)
#     2. AAVE flashloan (fee 0.05%)  если токен в пуле AAVE
#        или PancakeSwap flashswap (fee ~0.3%)
#
#   CONTRACT_ADDRESS задан →
#     3. MevArbitrage.executeArbitrage() — атомарно, свой капитал
#
#   Ничего не задано →
#     4. executor.py — два раздельных свапа (медленно, рискованно)
#
# Запуск:
#   python main.py                       # обычный режим
#   python main.py --scan-only           # только смотреть
#   python main.py --check               # проверить подключение
#   python main.py --once                # один цикл и выйти
#   python main.py --triangle            # + треугольный арбитраж
#   python main.py --mempool             # + WebSocket мемпул
#   python main.py --mempool --sandwich  # + sandwich атаки
#   python main.py --withdraw WBNB       # вывести прибыль
#   python main.py --sweep               # вывести всё (emergency)
# =============================================================================

import sys
import time
import signal
import argparse
import os
import atexit
from datetime import datetime
from typing import Dict

# =============================================================================
# ЗАЩИТА ОТ ДВОЙНОГО ЗАПУСКА (PID-файл)
# =============================================================================

_LOCK_FILE = os.path.join(os.path.dirname(__file__), ".mev_bot.pid")


def _acquire_lock() -> None:
    """Проверяет что бот ещё не запущен. Завершает процесс если запущен."""
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE) as f:
                old_pid = int(f.read().strip())
            # Проверяем жив ли старый процесс
            os.kill(old_pid, 0)
            print(f"[ОШИБКА] Бот уже запущен (PID {old_pid}). Выход.")
            print(f"         Если процесс завис — удали файл: {_LOCK_FILE}")
            sys.exit(1)
        except (ProcessLookupError, PermissionError):
            pass   # Старый процесс мёртв — перезаписываем PID
        except ValueError:
            pass   # Битый PID-файл

    with open(_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(_release_lock)


def _release_lock() -> None:
    try:
        os.remove(_LOCK_FILE)
    except FileNotFoundError:
        pass

# uvloop: быстрый event loop на C (libuv). Ускоряет aiohttp и WS в 2-4x.
# Работает только на Linux/macOS — на Windows тихо игнорируется.
try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

from chain import connect, get_bnb_balance, get_gas_price
from arbitrage import scan_all_pairs
from executor import execute_arbitrage
from config import (
    SCAN_INTERVAL_SEC, MIN_PROFIT_BNB, DRY_RUN,
    WATCH_PAIRS, MAX_GAS_GWEI, WALLET_ADDRESS,
    FLASH_CONTRACT_ADDRESS, FLASH_MIN_PROFIT_BNB,
    FLASH_PROVIDER, DEX, MAX_TRADE_BNB,
)
from logger import get_logger
import telegram_alert as tg

log = get_logger()

CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS", "")

# =============================================================================
# КУЛДАУН ПОСЛЕ РЕВЕРТА
# =============================================================================
# После TX revert один и тот же маршрут блокируется на REVERT_COOLDOWN_SEC секунд.
# Предотвращает повторные попытки пока reserves не обновятся.
REVERT_COOLDOWN_SEC = 30  # ~10 блоков BSC

_revert_cooldown: Dict[str, float] = {}  # route_key → timestamp последнего реверта


def _is_cooling_down(route_key: str) -> bool:
    ts = _revert_cooldown.get(route_key)
    if ts and time.time() - ts < REVERT_COOLDOWN_SEC:
        remaining = REVERT_COOLDOWN_SEC - (time.time() - ts)
        log.debug(f"Кулдаун {route_key}: ещё {remaining:.0f}с")
        return True
    return False


def _set_cooldown(route_key: str) -> None:
    _revert_cooldown[route_key] = time.time()

# =============================================================================
# СТАТИСТИКА
# =============================================================================

stats = {
    "start_time":     datetime.now(),
    "cycles":         0,
    "opps_found":     0,
    "trades_tried":   0,
    "trades_ok":      0,
    "total_profit":   0.0,
    "flash_aave":     0,
    "flash_pancake":  0,
    "flash_old":      0,
    "flash_seq":      0,
    "triangle_found": 0,
    "triangle_ok":    0,
}

_running = True


def handle_signal(sig, frame):
    global _running
    log.info("Получен сигнал остановки...")
    _running = False


# =============================================================================
# ВЫВОД
# =============================================================================

def print_banner():
    mode = "DRY RUN (симуляция)" if DRY_RUN else "БОЕВОЙ РЕЖИМ"

    if FLASH_CONTRACT_ADDRESS:
        c_line = f"  FlashArbitrage: {FLASH_CONTRACT_ADDRESS[:10]}... [{FLASH_PROVIDER.upper()}]"
    elif CONTRACT_ADDRESS:
        c_line = f"  MevArbitrage:   {CONTRACT_ADDRESS[:10]}... [свой капитал]"
    else:
        c_line = "  Контракт:       не задан [два раздельных свапа]"

    print("\n" + "═" * 60)
    print("  MEV//BOT — BNB Chain Arbitrage")
    print(f"  Режим:   {mode}")
    print(c_line)
    print(f"  Пары:    {', '.join(f'{a}/{b}' for a, b in WATCH_PAIRS)}")
    print(f"  DEX:     {', '.join(DEX.keys())}")
    print(f"  Мин. прибыль: {MIN_PROFIT_BNB} BNB  (flash: {FLASH_MIN_PROFIT_BNB} BNB)")
    print(f"  Макс. газ:    {MAX_GAS_GWEI} Gwei")
    print("═" * 60 + "\n")


def print_stats():
    e = datetime.now() - stats["start_time"]
    h, rem = divmod(int(e.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    log.info(
        f"━━ СТАТИСТИКА ━━ "
        f"{h:02d}:{m:02d}:{s:02d} | "
        f"циклы={stats['cycles']} | "
        f"возможности={stats['opps_found']} | "
        f"сделок={stats['trades_ok']}/{stats['trades_tried']} | "
        f"профит={stats['total_profit']:+.6f} BNB"
    )
    if stats["trades_ok"]:
        log.info(
            f"   AAVE={stats['flash_aave']} | "
            f"PancakeFlash={stats['flash_pancake']} | "
            f"MevContract={stats['flash_old']} | "
            f"Sequential={stats['flash_seq']} | "
            f"Triangle={stats['triangle_ok']}/{stats['triangle_found']}"
        )


# =============================================================================
# ИСПОЛНЕНИЕ
# =============================================================================

def _execute_best(w3, best, flash_client) -> None:
    """
    Исполняет лучшую возможность через оптимальный провайдер.
    Иерархия: FlashAave → FlashPancake → MevContract → Sequential
    """
    global stats
    stats["trades_tried"] += 1
    success  = False
    provider = "none"

    # Multihop маршруты (путь длиннее 2 токенов) не поддерживаются flash-контрактом,
    # который внутри строит path=[tokenA, tokenB]. Для них используем sequential.
    has_multihop = (
        len(getattr(best, "buy_path",  [])) > 2 or
        len(getattr(best, "sell_path", [])) > 2
    )
    if has_multihop:
        log.info(
            f"Multihop маршрут [{best.buy_path} / {best.sell_path}] → sequential executor"
        )

    # ── 1. FlashArbitrage (займ, без своего капитала) ────────────────────────
    if not has_multihop and flash_client and FLASH_CONTRACT_ADDRESS:
        sim = flash_client.simulate(best)
        if sim:
            log.info(f"Flash симуляция: {sim}")
            if sim.recommended_provider != "NONE":
                success  = flash_client.execute(best, sim)
                provider = sim.recommended_provider.lower()
                if provider == "aave":
                    stats["flash_aave"] += 1
                else:
                    stats["flash_pancake"] += 1
            else:
                log.warning("Flash провайдеры убыточны — пробуем MevArbitrage")

    # ── 2. MevArbitrage (атомарно, свой капитал) ─────────────────────────────
    if not success and not has_multihop and CONTRACT_ADDRESS:
        from contract import execute_via_contract, simulate_on_chain
        ok_sim, sim_profit = simulate_on_chain(w3, best)
        if ok_sim:
            log.info(f"MevArbitrage симуляция: +{sim_profit:.6f} BNB")
            success  = execute_via_contract(w3, best)
            provider = "mev_contract"
            stats["flash_old"] += 1
        else:
            log.warning("MevArbitrage симуляция убыточна")

    # ── 3. Sequential (multihop и last resort) ────────────────────────────────
    if not success and (has_multihop or (not FLASH_CONTRACT_ADDRESS and not CONTRACT_ADDRESS)):
        success  = execute_arbitrage(w3, best)
        provider = "sequential"
        stats["flash_seq"] += 1

    if success:
        stats["trades_ok"]    += 1
        stats["total_profit"] += best.net_profit
        log.profit(
            f"[{provider.upper()}] Сделка выполнена | "
            f"Суммарно: {stats['total_profit']:+.6f} BNB"
        )
        tg.alert_profit(
            provider    = provider.upper(),
            pair        = f"{best.token_a}/{best.token_b}",
            net_profit  = best.net_profit,
            total_profit= stats["total_profit"],
        )
    else:
        log.warning("Все провайдеры не смогли исполнить сделку")
        tg.alert_trade_failed(
            pair             = f"{best.token_a}/{best.token_b} | {best.buy_dex}→{best.sell_dex}",
            reason           = "Все провайдеры (AAVE/Pancake) вернули убыток или revert",
            potential_profit = best.net_profit,
        )


def _run_triangle(flash_client, scan_only: bool) -> None:
    """
    Сканирует треугольный арбитраж ЛОКАЛЬНО (0 RPC), 
    исполняет через flash_client только при обнаружении прибыли.
    
    Старый подход: N³ RPC вызовов simulateTriangle() на контракте.
    Новый подход:  N³ вычислений calc_amount_out() в памяти → 0 RPC.
    """
    global stats

    # Получаем резервы из последнего multicall скана
    from multicall import get_last_reserves
    from triangle import scan_triangles_local

    reserves = get_last_reserves()
    if not reserves:
        log.debug("Треугольники: нет резервов из multicall")
        return

    # Полностью локальный скан — 0 RPC
    results = scan_triangles_local(reserves, amount_bnb=MAX_TRADE_BNB)
    profitable = [r for r in results if r.profitable]

    if not profitable:
        log.debug("Нет прибыльных треугольников")
        return

    stats["triangle_found"] += len(profitable)

    # Фильтруем маршруты в кулдауне (недавний реверт)
    ready = [
        r for r in profitable
        if not _is_cooling_down(f"{r.token_a}→{r.token_b}→{r.token_c}|{r.dex1}/{r.dex2}/{r.dex3}")
    ]
    if not ready:
        log.debug("Треугольники: все маршруты в кулдауне после реверта")
        return

    best = ready[0]
    route_key = f"{best.token_a}→{best.token_b}→{best.token_c}|{best.dex1}/{best.dex2}/{best.dex3}"

    log.success(
        f"TRIANGLE: {best.token_a}→{best.token_b}→{best.token_c} | "
        f"{best.dex1}/{best.dex2}/{best.dex3} | "
        f"net={best.net_profit:+.6f} BNB"
    )

    if scan_only:
        return

    if not flash_client:
        log.warning("Треугольный: flash_client не инициализирован")
        return

    ok = flash_client.execute_triangle(
        best.token_a, best.token_b, best.token_c,
        best.amount_in,
        best.dex1, best.dex2, best.dex3,
        FLASH_MIN_PROFIT_BNB,
    )
    if ok:
        stats["triangle_ok"]  += 1
        stats["total_profit"] += best.net_profit
        log.profit(f"Треугольный выполнен! Профит: {best.net_profit:+.6f} BNB")
        tg.alert_triangle_profit(
            token_a      = best.token_a,
            token_b      = best.token_b,
            token_c      = best.token_c,
            dex1         = best.dex1,
            dex2         = best.dex2,
            dex3         = best.dex3,
            net_profit   = best.net_profit,
            total_profit = stats["total_profit"],
        )
    else:
        # Реверт или симуляция убыточна — ставим кулдаун на этот маршрут
        _set_cooldown(route_key)
        log.warning(f"Кулдаун {REVERT_COOLDOWN_SEC}с для маршрута: {route_key}")
        tg.alert_trade_failed(
            pair             = f"{best.token_a}→{best.token_b}→{best.token_c} | {best.dex1}/{best.dex2}/{best.dex3}",
            reason           = "Revert или симуляция убыточна",
            is_triangle      = True,
            potential_profit = best.net_profit,
        )


# =============================================================================
# ЦИКЛ
# =============================================================================

def run_cycle(w3, scan_only=False, do_triangle=False, flash_client=None):
    global stats
    stats["cycles"] += 1

    from profiler import profiler

    profiler.start_cycle()

    try:
        # Используем номер блока из BlockWatcher (0 RPC) если он синхронизирован,
        # иначе fallback на w3.eth.block_number (1 RPC)
        from chain import block_watcher as _bw
        block = _bw.latest_block if _bw.latest_block > 0 else w3.eth.block_number
        gas_gwei = get_gas_price(w3) / 1e9
    except ValueError as e:
        log.warning(str(e))
        return
    except Exception as e:
        log.error(f"Ошибка сети: {e}")
        return

    log.info(
        f"Блок #{block:,} | Газ {gas_gwei:.2f} Gwei | "
        f"{len(WATCH_PAIRS)} пар × {len(DEX)} DEX..."
    )

    # Двусторонний арбитраж (с профилированием)
    with profiler.measure("scan"):
        opportunities = scan_all_pairs(w3)
    profitable = [o for o in opportunities if o.profitable]

    if profitable:
        stats["opps_found"] += len(profitable)
        log.success(f"Найдено {len(profitable)} возможностей")

        if scan_only:
            # Используем ЛОКАЛЬНУЮ симуляцию вместо RPC к контракту
            with profiler.measure("local_sim"):
                from local_sim import simulate_opportunity_local
                from multicall import get_last_reserves
                reserves = get_last_reserves()
                for opp in profitable:
                    log.success(str(opp))
                    if reserves:
                        sim = simulate_opportunity_local(opp, reserves)
                        if sim:
                            log.info(f"   → {sim}")
        else:
            with profiler.measure("execute"):
                _execute_best(w3, profitable[0], flash_client)
    else:
        log.info(f"Нет прибыльных ({len(opportunities)} проверено)")

    # Треугольный арбитраж (локальный, 0 RPC)
    if do_triangle:
        with profiler.measure("triangle"):
            _run_triangle(flash_client, scan_only)

    # Multihop маршруты (логирование лучших, 0 RPC)
    if stats["cycles"] % 5 == 0:  # Каждые 5 циклов (не каждый — экономим CPU)
        with profiler.measure("multihop"):
            try:
                from multihop import scan_multihop_improvements
                from multicall import get_last_reserves
                mh_reserves = get_last_reserves()
                if mh_reserves:
                    scan_multihop_improvements(
                        mh_reserves, WATCH_PAIRS, int(MAX_TRADE_BNB * 1e18)
                    )
            except Exception as e:
                log.debug(f"Multihop scan: {e}")

    profiler.end_cycle()

    # Показываем профиль каждый цикл и средние каждые 10 циклов
    profiler.log_summary()
    if stats["cycles"] % 10 == 0:
        profiler.log_summary_avg()


# =============================================================================
# MAIN
# =============================================================================

def main():
    _acquire_lock()
    signal.signal(signal.SIGINT,  handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    parser = argparse.ArgumentParser(description="MEV Bot — BNB Chain")
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--check",     action="store_true")
    parser.add_argument("--once",      action="store_true")
    parser.add_argument("--triangle",  action="store_true",
                        help="Сканировать треугольный арбитраж")
    parser.add_argument("--mempool",   action="store_true")
    parser.add_argument("--sandwich",  action="store_true")
    parser.add_argument("--poll",      action="store_true",
                        help="Принудительный polling вместо WebSocket блоков")
    parser.add_argument("--withdraw",  metavar="TOKEN",
                        help="Вывести прибыль (пример: --withdraw WBNB)")
    parser.add_argument("--sweep",     action="store_true",
                        help="Вывести все токены с контракта")
    args = parser.parse_args()

    print_banner()

    try:
        w3 = connect()
    except ConnectionError as e:
        log.error(str(e))
        sys.exit(1)

    # ── Загрузка кэша адресов пулов ───────────────────────────────────────────
    from pair_store import pair_store
    try:
        total_pools = pair_store.load_or_fetch(w3)
        pair_store.inject_into_dex_cache()
        log.success(f"PairStore: {total_pools} пулов готово")
    except Exception as e:
        log.warning(f"PairStore: не удалось загрузить ({e}), будет fallback на multicall")

    if WALLET_ADDRESS:
        bal = get_bnb_balance(w3)
        log.info(f"Кошелёк: {WALLET_ADDRESS[:8]}...{WALLET_ADDRESS[-6:]}")
        log.info(f"Баланс:  {bal:.6f} BNB")
        tg.alert_started(WALLET_ADDRESS, bal, DRY_RUN)

        # Синхронизация nonce
        from nonce_manager import nonce_mgr
        nonce_mgr.sync(w3)
        log.info(f"Nonce: {nonce_mgr.get_current()}")
    else:
        if tg.is_configured():
            tg.send("START", "🤖 <b>MEV Bot запущен</b>\n⚠️ WALLET_ADDRESS не настроен")
        log.warning("WALLET_ADDRESS не настроен")

    # Инициализация FlashContractClient
    flash_client = None
    if FLASH_CONTRACT_ADDRESS:
        from flash_contract import FlashContractClient
        flash_client = FlashContractClient(w3)
        log.success(f"FlashArbitrage: {FLASH_CONTRACT_ADDRESS}")

        balances = flash_client.get_balances()
        nonzero  = {k: f"{v:.4f}" for k, v in balances.items() if v > 0}
        if nonzero:
            log.info(f"Баланс контракта: {nonzero}")

        cmp = flash_client.compare_providers("WBNB", 1.0)
        if cmp:
            log.info(
                f"Fee (1 WBNB): Pancake={cmp['pancake_cost_bnb']:.6f} | "
                f"AAVE={cmp['aave_cost_bnb']:.6f} | "
                f"Лучший={cmp['cheaper']}"
            )
    elif CONTRACT_ADDRESS:
        log.info(f"MevArbitrage: {CONTRACT_ADDRESS[:10]}...")

    # ── Telegram commander (приём команд /balance и др.) ──────────────────────
    tg.start_commander(w3, flash_client)

    # ── Утилиты ───────────────────────────────────────────────────────────────
    if args.withdraw:
        if flash_client:
            ok = flash_client.withdraw(args.withdraw)
        elif CONTRACT_ADDRESS:
            from contract import withdraw_profit
            ok = withdraw_profit(w3, args.withdraw)
        else:
            log.error("Нет контракта для вывода")
            sys.exit(1)
        sys.exit(0 if ok else 1)

    if args.sweep:
        if not flash_client:
            log.error("--sweep только для FlashArbitrage")
            sys.exit(1)
        ok = flash_client.emergency_sweep()
        sys.exit(0 if ok else 1)

    if args.check:
        log.success("Подключение ОК.")
        return

    # ── Мемпул монитор ────────────────────────────────────────────────────────
    if args.mempool:
        from mempool import MempoolMonitor
        from sandwich import execute_sandwich
        import asyncio
        monitor = MempoolMonitor(w3)

        async def on_sandwich(opp):
            if args.sandwich:
                await execute_sandwich(w3, opp)
            else:
                log.success(f"SANDWICH: {opp}")

        monitor.on_sandwich(on_sandwich)
        log.info("Мемпул монитор активен")

    # ── Режим ожидания блоков ─────────────────────────────────────────────────
    from chain import block_watcher, reconnect

    use_block_watcher = not args.poll
    if use_block_watcher:
        try:
            block_watcher.start()
            log.info("Режим: подписка на блоки (WebSocket)")
        except Exception as e:
            log.warning(f"BlockWatcher не запустился: {e}. Fallback на polling.")
            use_block_watcher = False

    if not use_block_watcher:
        log.info(f"Режим: polling каждые {SCAN_INTERVAL_SEC}с")

    # ── Основной цикл ─────────────────────────────────────────────────────────
    flags = []
    if args.scan_only: flags.append("scan-only")
    if args.triangle:  flags.append("triangle")
    if args.mempool:   flags.append("mempool")
    if use_block_watcher: flags.append("block-sub")
    log.info(
        f"Запуск {'(' + ', '.join(flags) + ') ' if flags else ''}"
        f"| Ctrl+C для остановки"
    )

    consecutive_errors = 0
    _last_heartbeat = time.time()

    while _running:
        try:
            # Heartbeat — раз в час
            if time.time() - _last_heartbeat >= 3600:
                try:
                    bnb_bal  = get_bnb_balance(w3)
                    gas_gwei = get_gas_price(w3) / 1e9
                except Exception:
                    bnb_bal, gas_gwei = 0.0, 0.0
                elapsed = int((datetime.now() - stats["start_time"]).total_seconds())
                h, m = divmod(elapsed // 60, 60)
                tg.alert_heartbeat(
                    uptime_str  = f"{h:02d}:{m:02d}",
                    cycles      = stats["cycles"],
                    profit      = stats["total_profit"],
                    bnb_balance = bnb_bal,
                    gas_gwei    = gas_gwei,
                )
                _last_heartbeat = time.time()

            # Пауза
            if tg.is_paused():
                time.sleep(1)
                continue

            # Ждём новый блок (или poll interval)
            if use_block_watcher and not args.once:
                got_block = block_watcher.wait_new_block(timeout=5.0)
                if not got_block and _running:
                    # Timeout — блоки не приходят, пробуем poll
                    log.debug("BlockWatcher timeout, пробуем запрос напрямую")

            run_cycle(
                w3,
                scan_only    = args.scan_only,
                do_triangle  = args.triangle,
                flash_client = flash_client,
            )
            consecutive_errors = 0  # Сброс при успехе

        except (ConnectionError, OSError, TimeoutError) as e:
            consecutive_errors += 1
            log.warning(f"Ошибка сети ({consecutive_errors}): {e}")
            if consecutive_errors >= 3:
                log.info("3+ ошибки подряд — переподключаюсь к другому RPC...")
                tg.alert_reconnect(consecutive_errors, str(e)[:60])
                try:
                    w3 = reconnect(w3)
                    # Ресинхронизируем nonce после переподключения
                    from nonce_manager import nonce_mgr
                    nonce_mgr.sync(w3)
                    consecutive_errors = 0
                except ConnectionError:
                    log.error("Все RPC недоступны. Жду 30с...")
                    tg.alert_error("Все RPC недоступны. Жду 30с...")
                    time.sleep(30)

        except Exception as e:
            if "429" in str(e) or "Too Many Requests" in str(e):
                log.warning(f"Rate limit (429) — переключаюсь на резервный RPC...")
                try:
                    w3 = reconnect(w3)
                    from nonce_manager import nonce_mgr
                    nonce_mgr.sync(w3)
                    consecutive_errors = 0
                except ConnectionError:
                    log.error("Все RPC недоступны. Жду 30с...")
                    time.sleep(30)
            else:
                log.error(f"Ошибка в цикле: {e}", exc_info=True)
                tg.alert_error(f"Ошибка в цикле: {e}")

        if stats["cycles"] % 10 == 0:
            print_stats()

        if args.once:
            break

        # Если polling — ждём интервал
        if not use_block_watcher and _running:
            time.sleep(SCAN_INTERVAL_SEC)

    block_watcher.stop()
    print("\n" + "═" * 60)
    log.info("Бот остановлен.")
    print_stats()
    print("═" * 60 + "\n")


if __name__ == "__main__":
    main()
