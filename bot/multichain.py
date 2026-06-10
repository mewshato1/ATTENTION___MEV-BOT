# =============================================================================
# multichain.py — Запуск MEV-бота на нескольких чейнах одновременно
# =============================================================================
#
# Каждый чейн = отдельный subprocess с CHAIN=<name> в env.
# Если процесс падает — перезапускается через 10 секунд.
# Ctrl+C останавливает все процессы.
#
# Запуск:
#   python multichain.py                              # все чейны из ACTIVE_CHAINS
#   python multichain.py --chains bsc_mainnet,polygon
#   python multichain.py --chains arbitrum --triangle  # + любые флаги main.py
#   python multichain.py --scan-only                   # dry run, все чейны
#
# Логи: mev_bot_bsc_mainnet.log, mev_bot_polygon.log, ...
# Пулы: pair_addresses_bsc_mainnet.json, pair_addresses_polygon.json, ...
# =============================================================================

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

from chains_config import CHAINS

ACTIVE_CHAINS = [
    "bsc_mainnet",
]

RESTART_DELAY_SEC = 10   # пауза перед перезапуском упавшего процесса
STATUS_INTERVAL   = 60   # вывод статуса каждые N секунд

_processes: dict[str, subprocess.Popen] = {}
_stop_event = threading.Event()


# =============================================================================
# ЗАПУСК ОДНОГО ЧЕЙНА
# =============================================================================

def chain_env(chain_name: str) -> dict:
    """Возвращает env для subprocess с перекрытием переменных для чейна."""
    env = os.environ.copy()
    env["CHAIN"]            = chain_name
    env["LOG_FILE"]         = f"mev_bot_{chain_name}.log"
    env["PAIR_STORE_FILE"]  = os.path.join(
        os.path.dirname(__file__),
        f"pair_addresses_{chain_name}.json",
    )
    # Подтягиваем RPC из .env если есть специфичные переменные
    rpc_map = {
        "bsc_mainnet": ("BSC_RPC_HTTP",      "BSC_RPC_WS"),
        "polygon":     ("POLYGON_RPC_HTTP",   "POLYGON_RPC_WS"),
        "arbitrum":    ("ARBITRUM_RPC_HTTP",  "ARBITRUM_RPC_WS"),
        "avalanche":   ("AVAX_RPC_HTTP",      "AVAX_RPC_WS"),
    }
    if chain_name in rpc_map:
        http_var, ws_var = rpc_map[chain_name]
        # Только если переменная задана в .env — пробрасываем как BSC_RPC_HTTP
        # (config.py читает BSC_RPC_HTTP как основной RPC, вне зависимости от чейна)
        if http_var in env and env[http_var]:
            env["BSC_RPC_HTTP"] = env[http_var]
        if ws_var in env and env[ws_var]:
            env["BSC_RPC_WS"] = env[ws_var]
    return env


def run_chain_loop(chain_name: str, main_args: list[str]) -> None:
    """
    Цикл для одного чейна: запускает subprocess, перезапускает при падении.
    Работает в отдельном потоке.
    """
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    cmd     = [sys.executable, os.path.join(bot_dir, "main.py")] + main_args
    env     = chain_env(chain_name)

    while not _stop_event.is_set():
        print(f"[{chain_name}] Запуск: {datetime.now().strftime('%H:%M:%S')}")
        try:
            proc = subprocess.Popen(cmd, env=env, cwd=bot_dir)
            _processes[chain_name] = proc
            ret = proc.wait()
            _processes.pop(chain_name, None)

            if _stop_event.is_set():
                break  # нормальная остановка

            if ret == 0:
                print(f"[{chain_name}] Вышел с кодом 0 (нормально)")
                break

            print(
                f"[{chain_name}] Упал с кодом {ret}. "
                f"Перезапуск через {RESTART_DELAY_SEC}с..."
            )
        except Exception as e:
            print(f"[{chain_name}] Ошибка запуска: {e}. Перезапуск через {RESTART_DELAY_SEC}с...")
            _processes.pop(chain_name, None)

        # Ждём перед перезапуском, но реагируем на stop_event
        _stop_event.wait(timeout=RESTART_DELAY_SEC)

    print(f"[{chain_name}] Поток завершён")


# =============================================================================
# СТАТУС
# =============================================================================

def status_loop(chains: list[str]) -> None:
    """Раз в STATUS_INTERVAL выводит какие чейны живы."""
    while not _stop_event.is_set():
        _stop_event.wait(timeout=STATUS_INTERVAL)
        if _stop_event.is_set():
            break
        alive = [c for c in chains if c in _processes and _processes[c].poll() is None]
        dead  = [c for c in chains if c not in alive]
        ts    = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] СТАТУС: живых={len(alive)} {alive} | мертвых={len(dead)} {dead}\n")


# =============================================================================
# ОСТАНОВКА
# =============================================================================

def stop_all(sig=None, frame=None) -> None:
    """Останавливает все дочерние процессы."""
    print("\nОстановка всех чейнов...")
    _stop_event.set()
    for chain_name, proc in list(_processes.items()):
        try:
            proc.terminate()
            print(f"  [{chain_name}] SIGTERM отправлен")
        except Exception:
            pass
    # Даём 5 секунд на завершение, потом kill
    time.sleep(5)
    for chain_name, proc in list(_processes.items()):
        if proc.poll() is None:
            proc.kill()
            print(f"  [{chain_name}] SIGKILL")
    _processes.clear()


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Запуск MEV-бота на нескольких чейнах одновременно",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Доступные чейны: {', '.join(CHAINS.keys())}",
    )
    parser.add_argument(
        "--chains",
        default=",".join(ACTIVE_CHAINS),
        help=f"Через запятую. По умолчанию: {','.join(ACTIVE_CHAINS)}",
    )
    # Пробрасываем любые флаги main.py напрямую
    parser.add_argument("--scan-only",  action="store_true")
    parser.add_argument("--triangle",   action="store_true")
    parser.add_argument("--poll",       action="store_true")
    parser.add_argument("--mempool",    action="store_true")

    args, unknown = parser.parse_known_args()

    chains = [c.strip() for c in args.chains.split(",") if c.strip()]

    # Проверяем что все чейны известны
    unknown_chains = [c for c in chains if c not in CHAINS]
    if unknown_chains:
        print(f"Неизвестные чейны: {unknown_chains}")
        print(f"Доступные: {', '.join(CHAINS.keys())}")
        sys.exit(1)

    # Флаги для main.py
    main_args = []
    if args.scan_only: main_args.append("--scan-only")
    if args.triangle:  main_args.append("--triangle")
    if args.poll:      main_args.append("--poll")
    if args.mempool:   main_args.append("--mempool")
    main_args += unknown

    # Сигналы остановки
    signal.signal(signal.SIGINT,  stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    print("=" * 60)
    print("  MEV//BOT — MultiChain")
    print(f"  Чейны: {', '.join(chains)}")
    if main_args:
        print(f"  Флаги: {' '.join(main_args)}")
    print("  Ctrl+C для остановки")
    print("=" * 60 + "\n")

    # Запускаем поток для каждого чейна
    threads = []
    for chain_name in chains:
        cfg = CHAINS[chain_name]
        contract_env_var = cfg.get("contract_env", "FLASH_CONTRACT_ADDRESS")
        contract_addr    = os.getenv(contract_env_var, os.getenv("FLASH_CONTRACT_ADDRESS", ""))
        if not contract_addr:
            print(
                f"  ПРЕДУПРЕЖДЕНИЕ [{chain_name}]: контракт не задан "
                f"({contract_env_var} в .env). Режим: DRY_RUN или только scan."
            )
        t = threading.Thread(
            target=run_chain_loop,
            args=(chain_name, main_args),
            daemon=True,
            name=f"chain-{chain_name}",
        )
        t.start()
        threads.append(t)
        time.sleep(1)  # небольшая задержка между стартами чтобы не перегружать RPC

    # Поток статуса
    st = threading.Thread(target=status_loop, args=(chains,), daemon=True)
    st.start()

    # Ждём пока все потоки не завершатся
    for t in threads:
        t.join()

    print("\nВсе чейны остановлены.")


if __name__ == "__main__":
    main()
