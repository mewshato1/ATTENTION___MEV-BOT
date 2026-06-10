# =============================================================================
# pool_filter.py — Фильтрация мёртвых и опасных пулов
# =============================================================================
#
# ПРОБЛЕМА:
#   Бот тратит время на анализ пулов где:
#   - Ликвидность $50 — любая сделка вызовет 90% price impact
#   - Токен с transfer-fee (дефляционный) — amountOut всегда меньше ожидаемого
#   - Пул постоянно revert'ит — битый контракт или паузa
#
# РЕШЕНИЕ:
#   Фильтруем пулы ДО расчёта арбитража:
#   1. Минимальные резервы (эквивалент ~$1000)
#   2. Чёрный список пулов с повторяющимися revert'ами
#   3. Запоминаем "мёртвые" пулы и проверяем их реже
# =============================================================================

import time
from typing import Dict, Set, Optional, Tuple
from logger import get_logger

log = get_logger()

# Минимальный размер резерва tokenA (в wei) для рассмотрения пула
# Логика: trade_size = MAX_TRADE_BNB * bnb_rate токенов.
# Для price impact < 1%: reserve > 100 × trade_size.
# При MAX_TRADE_BNB=0.5 BNB: нужно минимум 50 BNB в резерве.
# При MAX_TRADE_BNB=3 BNB: нужно минимум 300 BNB — только PancakeSwap_V2/BiSwap пройдут.
# Значение 50 BNB — разумный минимум для торговли с <1% price impact на 0.5 BNB сделке.
MIN_RESERVE_WEI = int(50 * 1e18)  # 50 BNB ≈ $15 000

# Сколько revert'ов до блокировки пула
MAX_REVERTS = 3

# Время блокировки (секунды) — пул не проверяется N секунд после блокировки
BLOCK_DURATION = 600  # 10 минут


class PoolFilter:
    """
    Фильтрует неликвидные и проблемные пулы.

    Использование:
        pf = PoolFilter()

        # В цикле сканирования:
        if not pf.is_viable(pair_key, dex_name, reserve_a, reserve_b):
            continue  # пропускаем этот пул

        # После revert'а:
        pf.record_revert(pair_key, dex_name)
    """

    def __init__(self, min_reserve_wei: int = MIN_RESERVE_WEI):
        self.min_reserve = min_reserve_wei
        # {"WBNB/USDT:PancakeSwap_V2": {"reverts": 3, "blocked_until": timestamp}}
        self._revert_log: Dict[str, Dict] = {}
        self._stats = {"filtered_low_liq": 0, "filtered_blocked": 0, "passed": 0}

    def is_viable(
        self,
        pair_key: str,
        dex_name: str,
        reserve_a: int,
        reserve_b: int,
    ) -> bool:
        """
        Проверяет пригоден ли пул для торговли.
        Возвращает False если пул неликвидный или заблокирован.
        """
        pool_id = f"{pair_key}:{dex_name}"

        # Проверка блокировки
        entry = self._revert_log.get(pool_id)
        if entry:
            blocked_until = entry.get("blocked_until", 0)
            if time.time() < blocked_until:
                self._stats["filtered_blocked"] += 1
                return False
            # Блокировка истекла — сбрасываем
            if blocked_until > 0 and time.time() >= blocked_until:
                entry["reverts"] = 0
                entry["blocked_until"] = 0

        # Проверка ликвидности
        if reserve_a < self.min_reserve or reserve_b < self.min_reserve:
            self._stats["filtered_low_liq"] += 1
            return False

        self._stats["passed"] += 1
        return True

    def record_revert(self, pair_key: str, dex_name: str):
        """Записывает revert. После MAX_REVERTS — блокирует пул."""
        pool_id = f"{pair_key}:{dex_name}"

        if pool_id not in self._revert_log:
            self._revert_log[pool_id] = {"reverts": 0, "blocked_until": 0}

        entry = self._revert_log[pool_id]
        entry["reverts"] += 1

        if entry["reverts"] >= MAX_REVERTS:
            entry["blocked_until"] = time.time() + BLOCK_DURATION
            log.warning(
                f"Пул {pool_id} заблокирован на {BLOCK_DURATION}с "
                f"({entry['reverts']} revert'ов)"
            )

    def record_success(self, pair_key: str, dex_name: str):
        """Сбрасывает счётчик revert'ов после успешной сделки."""
        pool_id = f"{pair_key}:{dex_name}"
        if pool_id in self._revert_log:
            self._revert_log[pool_id]["reverts"] = 0

    def get_blocked_pools(self) -> list:
        """Список заблокированных пулов."""
        now = time.time()
        return [
            pool_id for pool_id, entry in self._revert_log.items()
            if entry.get("blocked_until", 0) > now
        ]

    def get_stats(self) -> Dict:
        return {
            **self._stats,
            "blocked_pools": len(self.get_blocked_pools()),
        }

    def reset(self):
        """Полный сброс фильтра."""
        self._revert_log.clear()
        self._stats = {"filtered_low_liq": 0, "filtered_blocked": 0, "passed": 0}


# Глобальный экземпляр
pool_filter = PoolFilter()
