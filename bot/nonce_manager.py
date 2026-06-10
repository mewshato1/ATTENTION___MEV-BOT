# =============================================================================
# nonce_manager.py — Локальное управление nonce
# =============================================================================
#
# ПРОБЛЕМА:
#   build_tx_params() вызывает w3.eth.get_transaction_count() — это RPC запрос.
#   При отправке нескольких TX подряд (sandwich: front-run + back-run)
#   это ещё и риск: вторая TX может получить тот же nonce что и первая.
#
# РЕШЕНИЕ:
#   Храним nonce локально. Инкрементируем после отправки.
#   Синхронизируем с блокчейном при старте и при ошибках.
#
# ЭКОНОМИЯ: -1 RPC на каждую транзакцию
# =============================================================================

import threading
from web3 import Web3
from config import WALLET_ADDRESS
from logger import get_logger

log = get_logger()


class NonceManager:
    """
    Потокобезопасный менеджер nonce.

    Использование:
        nm = NonceManager()
        nm.sync(w3)                    # При старте
        nonce = nm.get_and_increment() # Для каждой TX
        nm.sync(w3)                    # При ошибке (nonce mismatch)
    """

    def __init__(self):
        self._nonce = -1
        self._lock = threading.Lock()
        self._synced = False

    def sync(self, w3: Web3) -> int:
        """
        Синхронизирует nonce с блокчейном.
        Вызывать при старте и при ошибках "nonce too low/already known".
        """
        if not WALLET_ADDRESS:
            return 0

        with self._lock:
            try:
                on_chain = w3.eth.get_transaction_count(
                    Web3.to_checksum_address(WALLET_ADDRESS), "pending"
                )
                self._nonce = on_chain
                self._synced = True
                log.debug(f"Nonce синхронизирован: {on_chain}")
                return on_chain
            except Exception as e:
                log.warning(f"Nonce sync failed: {e}")
                return self._nonce

    def get_and_increment(self) -> int:
        """Возвращает текущий nonce и увеличивает на 1."""
        with self._lock:
            current = self._nonce
            self._nonce += 1
            return current

    def get_current(self) -> int:
        """Возвращает текущий nonce без инкремента."""
        with self._lock:
            return self._nonce

    def rollback(self):
        """Откатывает nonce на 1 назад (TX не прошла)."""
        with self._lock:
            self._nonce = max(0, self._nonce - 1)
            log.debug(f"Nonce rollback → {self._nonce}")

    @property
    def is_synced(self) -> bool:
        return self._synced


# Глобальный экземпляр
nonce_mgr = NonceManager()
