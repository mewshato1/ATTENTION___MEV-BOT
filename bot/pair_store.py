# =============================================================================
# pair_store.py — Персистентный кэш адресов пулов
# =============================================================================
#
# ПРОБЛЕМА:
#   При каждом запуске бота multicall тратит 3-5 RPC на getPair()
#   для каждого DEX. Адреса пулов НЕ меняются (разве что DEX мигрирует).
#   На бесплатных нодах это 500-1500мс впустую при каждом старте.
#
# РЕШЕНИЕ:
#   Один раз загружаем адреса → сохраняем в JSON файл.
#   При следующем запуске читаем из файла (0мс).
#   Фоновое обновление раз в 24 часа.
#
# ФОРМАТ ФАЙЛА pair_addresses.json:
#   {
#     "updated_at": "2025-01-15T10:30:00",
#     "pairs": {
#       "PancakeSwap_V2": {
#         "WBNB/USDT": "0x16b9a82891338f9bA80E2D6970FddA79D1eB0daE",
#         ...
#       },
#       ...
#     }
#   }
# =============================================================================

import json
import os
import time
from datetime import datetime
from typing import Dict, Optional, Tuple
from web3 import Web3
from config import TOKENS, DEX, DEX_V3, WATCH_PAIRS
from logger import get_logger

log = get_logger()

CACHE_FILE = os.getenv("PAIR_STORE_FILE", os.path.join(os.path.dirname(__file__), "pair_addresses.json"))
REFRESH_INTERVAL = 86400  # 24 часа


class PairStore:
    """
    Персистентный кэш адресов пулов.

    Использование:
        store = PairStore()
        store.load_or_fetch(w3)   # Загружает из файла или с блокчейна
        addr = store.get("PancakeSwap_V2", "WBNB", "USDT")
    """

    def __init__(self):
        # { dex_name: { "tokenA/tokenB": pair_address } }
        self._pairs: Dict[str, Dict[str, str]] = {}
        self._updated_at: float = 0
        self._loaded = False

    def load_or_fetch(self, w3: Web3) -> int:
        """
        Загружает адреса из файла. Если файл устарел или отсутствует —
        запрашивает блокчейн и сохраняет.

        Возвращает количество найденных пулов.
        """
        loaded_from_file = self._load_from_file()

        if loaded_from_file:
            age_hours = (time.time() - self._updated_at) / 3600
            total = sum(len(d) for d in self._pairs.values())
            log.info(
                f"PairStore: загружено {total} пулов из файла "
                f"(возраст: {age_hours:.1f}ч)"
            )

            # Переобновляем если V3 пулы отсутствуют или кэш старше 24ч
            v3_missing = bool(DEX_V3) and not any(d in self._pairs for d in DEX_V3)
            if v3_missing:
                log.info("PairStore: V3 пулы не найдены в кэше — обновляю...")
                self._fetch_all(w3)
                self._save_to_file()
            elif time.time() - self._updated_at > REFRESH_INTERVAL:
                log.info("PairStore: кэш старше 24ч — обновляю...")
                self._fetch_all(w3)
                self._save_to_file()
        else:
            log.info("PairStore: файл не найден — загружаю с блокчейна...")
            self._fetch_all(w3)
            self._save_to_file()

        self._loaded = True
        total = sum(len(d) for d in self._pairs.values())
        return total

    def get(self, dex_name: str, sym_a: str, sym_b: str) -> Optional[str]:
        """Возвращает адрес пула или None."""
        dex_pairs = self._pairs.get(dex_name, {})
        # Пробуем оба направления
        return dex_pairs.get(f"{sym_a}/{sym_b}") or dex_pairs.get(f"{sym_b}/{sym_a}")

    def get_all_for_pair(self, sym_a: str, sym_b: str) -> Dict[str, str]:
        """Возвращает {dex_name: pair_address} для пары."""
        result = {}
        for dex_name in DEX:
            addr = self.get(dex_name, sym_a, sym_b)
            if addr:
                result[dex_name] = addr
        return result

    def get_all(self) -> Dict[str, Dict[str, str]]:
        """Возвращает весь кэш."""
        return self._pairs

    def inject_into_dex_cache(self):
        """
        Заполняет pair_cache в dex.py из нашего хранилища.
        Это позволяет dex.py/multicall.py НЕ делать getPair запросы.
        """
        from dex import pair_cache, _pair_timestamps
        import time as t

        injected = 0
        now = t.time()

        for dex_name, dex_pairs in self._pairs.items():
            dex_info = DEX.get(dex_name)
            if not dex_info:
                continue
            factory = dex_info["factory"]

            for pair_key, pair_addr in dex_pairs.items():
                parts = pair_key.split("/")
                if len(parts) != 2:
                    continue
                sym_a, sym_b = parts
                addr_a = TOKENS.get(sym_a)
                addr_b = TOKENS.get(sym_b)
                if not addr_a or not addr_b:
                    continue

                cache_key = f"{factory}:{addr_a}:{addr_b}"
                pair_cache[cache_key] = pair_addr
                _pair_timestamps[cache_key] = now
                injected += 1

        log.debug(f"PairStore: внедрено {injected} записей в dex.pair_cache")

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    # ── Приватные методы ──────────────────────────────────────────────────

    def _load_from_file(self) -> bool:
        """Загружает из JSON файла."""
        if not os.path.exists(CACHE_FILE):
            return False

        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)

            self._pairs = data.get("pairs", {})
            updated_str = data.get("updated_at", "")
            if updated_str:
                dt = datetime.fromisoformat(updated_str)
                self._updated_at = dt.timestamp()
            else:
                self._updated_at = os.path.getmtime(CACHE_FILE)

            return bool(self._pairs)

        except Exception as e:
            log.warning(f"PairStore: ошибка чтения файла: {e}")
            return False

    def _save_to_file(self):
        """Сохраняет в JSON файл."""
        try:
            data = {
                "updated_at": datetime.now().isoformat(),
                "pairs": self._pairs,
            }
            with open(CACHE_FILE, "w") as f:
                json.dump(data, f, indent=2)

            total = sum(len(d) for d in self._pairs.values())
            log.info(f"PairStore: сохранено {total} пулов в {CACHE_FILE}")

        except Exception as e:
            log.error(f"PairStore: ошибка записи: {e}")

    def _fetch_all(self, w3: Web3):
        """Загружает адреса всех пулов с блокчейна (V2 + V3)."""
        from abi import FACTORY_ABI, V3_FACTORY_ABI

        self._pairs = {}
        zero = "0x" + "0" * 40

        # ── V2-совместимые DEX ────────────────────────────────────────────
        for dex_name, dex_info in DEX.items():
            factory_addr = dex_info["factory"]
            self._pairs[dex_name] = {}

            try:
                factory = w3.eth.contract(
                    address=Web3.to_checksum_address(factory_addr),
                    abi=FACTORY_ABI,
                )
            except Exception as e:
                log.warning(f"PairStore: не удалось подключить фабрику {dex_name}: {e}")
                continue

            for sym_a, sym_b in WATCH_PAIRS:
                addr_a = TOKENS.get(sym_a)
                addr_b = TOKENS.get(sym_b)
                if not addr_a or not addr_b:
                    continue

                try:
                    pair_addr = factory.functions.getPair(
                        Web3.to_checksum_address(addr_a),
                        Web3.to_checksum_address(addr_b),
                    ).call()

                    if pair_addr and pair_addr != zero:
                        self._pairs[dex_name][f"{sym_a}/{sym_b}"] = pair_addr
                    else:
                        log.debug(f"PairStore: NO POOL  {dex_name:16s} {sym_a}/{sym_b}")

                except Exception as e:
                    log.debug(f"getPair {dex_name} {sym_a}/{sym_b}: {e}")

            found = len(self._pairs[dex_name])
            log.info(f"PairStore: {dex_name} — {found}/{len(WATCH_PAIRS)} пулов")

        # ── PancakeSwap V3 (концентрированная ликвидность) ────────────────
        for dex_name, v3_info in DEX_V3.items():
            factory_addr = v3_info["factory"]
            fee_pips     = v3_info["fee_pips"]
            self._pairs[dex_name] = {}

            try:
                factory = w3.eth.contract(
                    address=Web3.to_checksum_address(factory_addr),
                    abi=V3_FACTORY_ABI,
                )
            except Exception as e:
                log.warning(f"PairStore: не удалось подключить фабрику {dex_name}: {e}")
                continue

            for sym_a, sym_b in WATCH_PAIRS:
                addr_a = TOKENS.get(sym_a)
                addr_b = TOKENS.get(sym_b)
                if not addr_a or not addr_b:
                    continue

                try:
                    pool_addr = factory.functions.getPool(
                        Web3.to_checksum_address(addr_a),
                        Web3.to_checksum_address(addr_b),
                        fee_pips,
                    ).call()

                    if pool_addr and pool_addr != zero:
                        self._pairs[dex_name][f"{sym_a}/{sym_b}"] = pool_addr
                    else:
                        log.debug(f"PairStore: NO POOL  {dex_name:20s} {sym_a}/{sym_b}")

                except Exception as e:
                    log.debug(f"getPool {dex_name} {sym_a}/{sym_b}: {e}")

            found = len(self._pairs[dex_name])
            log.info(f"PairStore: {dex_name} — {found}/{len(WATCH_PAIRS)} пулов")

        self._updated_at = time.time()


# Глобальный экземпляр
pair_store = PairStore()
