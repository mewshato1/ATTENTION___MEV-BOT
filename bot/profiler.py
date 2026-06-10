# =============================================================================
# profiler.py — Профилирование цикла бота
# =============================================================================
#
# Замеряет время каждого этапа:
#   1. multicall scan (получение резервов)
#   2. arbitrage calc (расчёт возможностей)
#   3. triangle scan (треугольный арбитраж)
#   4. simulation (симуляция прибыли)
#   5. execution (отправка TX)
#
# Показывает где реально тратится время — часто узкое место
# неочевидно без замеров.
# =============================================================================

import time
from typing import Dict, List, Optional
from collections import defaultdict
from logger import get_logger

log = get_logger()


class CycleProfiler:
    """
    Профайлер одного цикла сканирования.

    Использование:
        prof = CycleProfiler()

        with prof.measure("multicall"):
            prices = scan_all_prices_multicall(w3)

        with prof.measure("arbitrage"):
            opps = find_opportunities(prices)

        prof.log_summary()     # Выводит разбивку по этапам
        prof.log_summary_avg() # Средние за все циклы
    """

    def __init__(self):
        self._timings: Dict[str, float] = {}
        self._history: List[Dict[str, float]] = []
        self._cycle_start: float = 0

    def start_cycle(self):
        """Начинает замер нового цикла."""
        self._timings = {}
        self._cycle_start = time.perf_counter()

    def measure(self, stage_name: str):
        """Контекстный менеджер для замера этапа."""
        return _StageTimer(self, stage_name)

    def record(self, stage_name: str, elapsed_ms: float):
        """Записывает время этапа вручную."""
        self._timings[stage_name] = elapsed_ms

    def end_cycle(self):
        """Завершает цикл, сохраняет в историю."""
        total = (time.perf_counter() - self._cycle_start) * 1000
        self._timings["_total"] = total
        self._history.append(self._timings.copy())

        # Храним максимум 100 циклов
        if len(self._history) > 100:
            self._history = self._history[-100:]

    def log_summary(self):
        """Выводит разбивку текущего цикла."""
        if not self._timings:
            return

        total = self._timings.get("_total", 0)
        parts = []
        for name, ms in sorted(self._timings.items()):
            if name.startswith("_"):
                continue
            pct = (ms / total * 100) if total > 0 else 0
            parts.append(f"{name}={ms:.0f}ms({pct:.0f}%)")

        log.info(f"⏱ Цикл {total:.0f}ms: {' | '.join(parts)}")

    def log_summary_avg(self):
        """Выводит средние по всем циклам."""
        if len(self._history) < 2:
            return

        n = len(self._history)
        aggregated: Dict[str, float] = defaultdict(float)

        for cycle in self._history:
            for name, ms in cycle.items():
                aggregated[name] += ms

        parts = []
        total_avg = aggregated.get("_total", 0) / n
        for name in sorted(aggregated.keys()):
            if name.startswith("_"):
                continue
            avg = aggregated[name] / n
            parts.append(f"{name}={avg:.0f}ms")

        log.info(
            f"⏱ AVG ({n} циклов): total={total_avg:.0f}ms | {' | '.join(parts)}"
        )

    @property
    def cycle_count(self) -> int:
        return len(self._history)

    @property
    def last_total_ms(self) -> float:
        if self._timings:
            return self._timings.get("_total", 0)
        return 0


class _StageTimer:
    """Контекстный менеджер для замера одного этапа."""

    def __init__(self, profiler: CycleProfiler, name: str):
        self._profiler = profiler
        self._name = name
        self._start = 0

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args):
        elapsed = (time.perf_counter() - self._start) * 1000
        self._profiler.record(self._name, elapsed)


# Глобальный экземпляр
profiler = CycleProfiler()
