# =============================================================================
# async_rpc.py — Параллельные запросы к нескольким BSC RPC нодам
# =============================================================================
#
# ПРОБЛЕМА:
#   Бесплатные RPC непредсказуемы: один запрос 50мс, следующий 500мс,
#   третий вообще timeout. Мы зависим от одной ноды.
#
# РЕШЕНИЕ:
#   Отправляем ОДИН И ТОТ ЖЕ запрос сразу на 3 ноды.
#   Берём первый ответ, остальные отбрасываем.
#   Результат: задержка = min(node1, node2, node3) вместо random(node1).
#
# ПРИМЕР:
#   Ноды ответили за: 180мс, 50мс, 320мс
#   Без racing: 180мс (случайная нода)
#   С racing:   50мс  (самая быстрая)
#
# ИСПОЛЬЗОВАНИЕ:
#   rpc = RpcRacer()
#   result = rpc.call("eth_call", [tx_data, "latest"])
#   # или для multicall:
#   result = rpc.eth_call(to, data)
# =============================================================================

import asyncio
import aiohttp
import json
import time
import threading
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import Future
from config import BSC_RPC_HTTP, BSC_RPC_HTTP_FALLBACKS
from logger import get_logger

log = get_logger()

# Сколько нод опрашивать параллельно (больше = надёжнее, но больше нагрузка)
MAX_PARALLEL = 3

# Timeout на один запрос (секунды)
# Бесплатные ноды могут отвечать 3-8 секунд на тяжёлый multicall
REQUEST_TIMEOUT = 3

# =============================================================================
# ПОСТОЯННАЯ aiohttp СЕССИЯ — TCP keep-alive между запросами
# =============================================================================
# Создаётся один раз при первом запросе в event loop RpcRacer.
# Повторно использует уже открытые TCP-соединения → экономит 100-500мс per call.
_persistent_session: Optional[aiohttp.ClientSession] = None


async def _get_or_create_session() -> aiohttp.ClientSession:
    global _persistent_session
    if _persistent_session is None or _persistent_session.closed:
        connector = aiohttp.TCPConnector(
            limit=30,
            limit_per_host=10,
            keepalive_timeout=60,
            enable_cleanup_closed=True,
            force_close=False,
        )
        _persistent_session = aiohttp.ClientSession(connector=connector)
    return _persistent_session

# Статистика: отслеживаем скорость каждой ноды
_node_stats: Dict[str, Dict] = {}


def _get_top_nodes(n: int = MAX_PARALLEL) -> List[str]:
    """
    Возвращает N лучших нод по средней задержке.
    Если статистики нет — возвращает первые N из списка.
    """
    all_nodes = [BSC_RPC_HTTP] + BSC_RPC_HTTP_FALLBACKS

    if not _node_stats:
        return all_nodes[:n]

    # Сортируем по средней задержке (меньше = лучше)
    scored = []
    for url in all_nodes:
        stats = _node_stats.get(url)
        if stats and stats["count"] > 0:
            avg_ms = stats["total_ms"] / stats["count"]
            # Штраф за ошибки: +500мс за каждую ошибку
            penalty = stats.get("errors", 0) * 500
            scored.append((url, avg_ms + penalty))
        else:
            scored.append((url, 999))  # Неизвестные — в конец

    scored.sort(key=lambda x: x[1])
    return [url for url, _ in scored[:n]]


def _update_stats(url: str, latency_ms: float, success: bool):
    """Обновляет статистику ноды."""
    if url not in _node_stats:
        _node_stats[url] = {"count": 0, "total_ms": 0.0, "errors": 0}

    stats = _node_stats[url]
    if success:
        stats["count"] += 1
        stats["total_ms"] += latency_ms
    else:
        stats["errors"] = stats.get("errors", 0) + 1


# =============================================================================
# ASYNC RACING
# =============================================================================

async def _send_one(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
    timeout: float,
) -> Tuple[Optional[Any], str, float]:
    """
    Отправляет один JSON-RPC запрос к одной ноде.
    Возвращает (result, url, latency_ms) или (None, url, latency_ms) при ошибке.
    """
    t0 = time.perf_counter()
    try:
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            data = await resp.json()
            latency = (time.perf_counter() - t0) * 1000

            if "error" in data:
                _update_stats(url, latency, False)
                return None, url, latency

            _update_stats(url, latency, True)
            return data.get("result"), url, latency

    except Exception:
        latency = (time.perf_counter() - t0) * 1000
        _update_stats(url, latency, False)
        return None, url, latency


async def _race_request(
    payload: dict,
    nodes: List[str] = None,
    timeout: float = REQUEST_TIMEOUT,
) -> Optional[Any]:
    """
    Отправляет один запрос параллельно на несколько нод.
    Возвращает первый успешный результат, отменяет остальные.
    """
    target_nodes = nodes or _get_top_nodes()
    session = await _get_or_create_session()

    tasks = [
        asyncio.create_task(_send_one(session, url, payload, timeout))
        for url in target_nodes
    ]

    # Ждём первый успешный результат
    for coro in asyncio.as_completed(tasks):
        result, url, latency = await coro
        if result is not None:
            # Отменяем оставшиеся задачи
            for t in tasks:
                if not t.done():
                    t.cancel()
            log.debug(f"RPC race: {url[:35]}... ответил за {latency:.0f}мс")
            return result

    log.warning("RPC race: все ноды не ответили")
    return None


async def _race_batch(
    payloads: List[dict],
    nodes: List[str] = None,
    timeout: float = REQUEST_TIMEOUT,
) -> List[Optional[Any]]:
    """
    Отправляет БАТЧ запросов как JSON-RPC batch на несколько нод.
    Возвращает список результатов в порядке payloads.
    """
    target_nodes = nodes or _get_top_nodes()
    session = await _get_or_create_session()

    async def _send_batch(url):
        t0 = time.perf_counter()
        try:
            async with session.post(
                url,
                json=payloads,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                data = await resp.json()
                latency = (time.perf_counter() - t0) * 1000
                _update_stats(url, latency, True)

                if isinstance(data, list):
                    return [item.get("result") for item in data], url, latency
                return None, url, latency
        except Exception:
            latency = (time.perf_counter() - t0) * 1000
            _update_stats(url, latency, False)
            return None, url, latency

    tasks = [
        asyncio.create_task(_send_batch(url))
        for url in target_nodes
    ]

    for coro in asyncio.as_completed(tasks):
        results, url, latency = await coro
        if results is not None:
            for t in tasks:
                if not t.done():
                    t.cancel()
            log.debug(f"RPC batch race: {url[:35]}... {len(payloads)} calls за {latency:.0f}мс")
            return results

    return [None] * len(payloads)


# =============================================================================
# СИНХРОННАЯ ОБЁРТКА (для использования из sync-кода бота)
# =============================================================================

class RpcRacer:
    """
    Синхронная обёртка над async racing.
    Запускает event loop в фоновом потоке.

    Использование:
        racer = RpcRacer()
        block = racer.call("eth_blockNumber", [])
        result = racer.eth_call(to_addr, calldata_hex)
        racer.shutdown()
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._req_id = 0

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def call(self, method: str, params: list, timeout: float = REQUEST_TIMEOUT) -> Optional[Any]:
        """Синхронный вызов JSON-RPC метода через racing."""
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }
        future = asyncio.run_coroutine_threadsafe(
            _race_request(payload, timeout=timeout),
            self._loop,
        )
        try:
            return future.result(timeout=timeout + 1)
        except Exception as e:
            log.error(f"RpcRacer.call({method}): {e}")
            return None

    def call_batch(self, calls: List[Tuple[str, list]], timeout: float = REQUEST_TIMEOUT) -> List[Optional[Any]]:
        """Синхронный батч JSON-RPC вызовов через racing."""
        payloads = [
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": method,
                "params": params,
            }
            for method, params in calls
        ]
        future = asyncio.run_coroutine_threadsafe(
            _race_batch(payloads, timeout=timeout),
            self._loop,
        )
        try:
            return future.result(timeout=timeout + 2)
        except Exception as e:
            log.error(f"RpcRacer.call_batch(): {e}")
            return [None] * len(calls)

    def eth_call(self, to: str, data: str, block: str = "latest") -> Optional[str]:
        """Удобный хелпер для eth_call."""
        return self.call("eth_call", [{"to": to, "data": data}, block])

    def get_stats(self) -> Dict[str, Dict]:
        """Возвращает статистику по нодам."""
        result = {}
        for url, stats in _node_stats.items():
            count = stats["count"]
            avg = stats["total_ms"] / count if count > 0 else 0
            result[url[:40]] = {
                "avg_ms": round(avg, 1),
                "requests": count,
                "errors": stats.get("errors", 0),
            }
        return result

    def shutdown(self):
        self._loop.call_soon_threadsafe(self._loop.stop)


# Глобальный экземпляр (ленивая инициализация)
_global_racer: Optional[RpcRacer] = None


def get_racer() -> RpcRacer:
    """Возвращает глобальный RpcRacer (создаёт при первом вызове)."""
    global _global_racer
    if _global_racer is None:
        _global_racer = RpcRacer()
    return _global_racer
