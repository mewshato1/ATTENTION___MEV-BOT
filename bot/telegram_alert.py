# =============================================================================
# telegram_alert.py — Telegram-уведомления для MEV-бота
# =============================================================================
#
# Настройка:
#   1. Создай бота через @BotFather в Telegram → получи TELEGRAM_BOT_TOKEN
#   2. Напиши боту любое сообщение, затем узнай свой CHAT_ID:
#      https://api.telegram.org/bot<TOKEN>/getUpdates
#   3. Добавь в .env:
#      TELEGRAM_BOT_TOKEN=123456:ABC-...
#      TELEGRAM_CHAT_ID=123456789
#      TELEGRAM_ALERTS=true
#
# Категории алертов (можно отключить каждую через .env):
#   START/STOP  — бот запущен/остановлен
#   PROFIT      — успешная сделка (всегда отправляется)
#   ERROR       — критическая ошибка
#   WARN        — предупреждение (reconnect и т.п.)
#   STATS       — периодическая статистика (каждые N циклов)
# =============================================================================

import os
import time
import asyncio
import threading
from typing import Optional
from logger import get_logger

log = get_logger()

# =============================================================================
# КОНФИГ (из .env)
# =============================================================================

_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_ENABLED = os.getenv("TELEGRAM_ALERTS", "true").lower() == "true"

# Какие категории отправлять
_SEND_PROFIT = os.getenv("TG_ALERT_PROFIT", "true").lower() == "true"
_SEND_ERROR  = os.getenv("TG_ALERT_ERROR",  "true").lower() == "true"
_SEND_WARN   = os.getenv("TG_ALERT_WARN",   "true").lower() == "true"
_SEND_STATS  = os.getenv("TG_ALERT_STATS",  "true").lower() == "true"
_SEND_START  = os.getenv("TG_ALERT_START",  "true").lower() == "true"
_SEND_FAIL   = os.getenv("TG_ALERT_FAIL",   "true").lower() == "true"

# =============================================================================
# RATE LIMITER — не спамим одинаковыми алертами
# =============================================================================

# Минимальный интервал между сообщениями одной категории (секунды)
_RATE_LIMITS = {
    "PROFIT":    0,      # Каждую прибыльную сделку — без лимита
    "ERROR":     30,     # Ошибки — не чаще раз в 30с
    "WARN":      60,     # Предупреждения — раз в 60с
    "STATS":     300,    # Статистика — раз в 5 минут
    "START":     0,
    "STOP":      0,
    "FAIL":      15,     # Неудачные сделки — не чаще раз в 15с
    "HEARTBEAT": 3600,   # Пинг — раз в час
}

_last_sent: dict = {}  # {category: timestamp}
_lock = threading.Lock()

# =============================================================================
# ПАУЗА
# =============================================================================

_bot_paused = False

def is_paused() -> bool:
    return _bot_paused

def set_paused(paused: bool):
    global _bot_paused
    _bot_paused = paused


def _is_rate_limited(category: str) -> bool:
    limit = _RATE_LIMITS.get(category, 60)
    if limit == 0:
        return False
    with _lock:
        last = _last_sent.get(category, 0)
        return (time.time() - last) < limit


def _update_rate_limit(category: str):
    with _lock:
        _last_sent[category] = time.time()


# =============================================================================
# ОТПРАВКА
# =============================================================================

def _send_raw(text: str) -> bool:
    """Синхронная отправка через urllib (без зависимостей)."""
    if not _TOKEN or not _CHAT_ID:
        return False
    try:
        import urllib.request
        import urllib.parse
        import json

        url  = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    _CHAT_ID,
            "text":       text,
            "parse_mode": "HTML",
        }).encode()

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception as e:
        log.debug(f"Telegram send failed: {e}")
        return False


def _send_in_thread(text: str):
    """Отправляет сообщение в фоновом потоке — не блокирует основной цикл."""
    t = threading.Thread(target=_send_raw, args=(text,), daemon=True)
    t.start()


# =============================================================================
# ПУБЛИЧНЫЙ API
# =============================================================================

def send(category: str, text: str):
    """
    Отправляет алерт в Telegram.

    category: PROFIT | ERROR | WARN | STATS | START | STOP
    text: текст сообщения (HTML разрешён: <b>, <code>)
    """
    if not _ENABLED or not _TOKEN or not _CHAT_ID:
        return

    # Проверяем настройку категории
    cat_flags = {
        "PROFIT": _SEND_PROFIT,
        "ERROR":  _SEND_ERROR,
        "WARN":   _SEND_WARN,
        "STATS":  _SEND_STATS,
        "START":  _SEND_START,
        "STOP":   _SEND_START,
        "FAIL":   _SEND_FAIL,
    }
    if not cat_flags.get(category, True):
        return

    if _is_rate_limited(category):
        log.debug(f"Telegram [{category}] rate-limited, пропуск")
        return

    _update_rate_limit(category)
    _send_in_thread(text)


async def send_async(category: str, text: str):
    """Асинхронная версия для использования в server.py (asyncio)."""
    if not _ENABLED or not _TOKEN or not _CHAT_ID:
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send, category, text)


# =============================================================================
# ГОТОВЫЕ ШАБЛОНЫ АЛЕРТОВ
# =============================================================================

def alert_started(wallet: str, balance: float, dry_run: bool):
    mode = "🔵 DRY RUN" if dry_run else "🟢 БОЕВОЙ РЕЖИМ"
    send("START", (
        f"🤖 <b>MEV Bot запущен</b>\n"
        f"Режим: {mode}\n"
        f"Кошелёк: <code>{wallet[:8]}...{wallet[-6:]}</code>\n"
        f"Баланс: <b>{balance:.4f} BNB</b>"
    ))


def alert_stopped(total_profit: float, trades_ok: int, trades_tried: int):
    send("STOP", (
        f"🛑 <b>MEV Bot остановлен</b>\n"
        f"Сделок: {trades_ok}/{trades_tried}\n"
        f"Суммарный профит: <b>{total_profit:+.6f} BNB</b>"
    ))


def alert_profit(provider: str, pair: str, net_profit: float, total_profit: float, tx_hash: str = ""):
    tx_line = f"\nTx: <code>{tx_hash[:16]}...</code>" if tx_hash else ""
    send("PROFIT", (
        f"💰 <b>Сделка выполнена!</b>\n"
        f"Провайдер: {provider}\n"
        f"Пара: <b>{pair}</b>\n"
        f"Профит: <b>{net_profit:+.6f} BNB</b>\n"
        f"Итого: {total_profit:+.6f} BNB"
        f"{tx_line}"
    ))


def alert_error(message: str):
    send("ERROR", f"🔴 <b>Ошибка бота</b>\n<code>{message[:300]}</code>")


def alert_reconnect(attempt: int, rpc: str):
    send("WARN", (
        f"⚠️ <b>Переподключение к RPC</b>\n"
        f"Попытка #{attempt}\n"
        f"Узел: <code>{rpc[:40]}</code>"
    ))


def alert_stats(cycles: int, opps: int, trades_ok: int, trades_tried: int, profit: float, uptime_str: str):
    send("STATS", (
        f"📊 <b>Статистика бота</b>\n"
        f"Аптайм: {uptime_str}\n"
        f"Циклов: {cycles} | Возможностей: {opps}\n"
        f"Сделок: {trades_ok}/{trades_tried}\n"
        f"Профит: <b>{profit:+.6f} BNB</b>"
    ))


def alert_heartbeat(uptime_str: str, cycles: int, profit: float, bnb_balance: float, gas_gwei: float):
    paused_line = "\n⏸ <b>БОТ НА ПАУЗЕ</b>" if _bot_paused else ""
    send("HEARTBEAT", (
        f"🟢 <b>Бот работает</b>{paused_line}\n"
        f"Аптайм: {uptime_str} | Циклов: {cycles:,}\n"
        f"Газ: {gas_gwei:.2f} Gwei | BNB: {bnb_balance:.4f}\n"
        f"Профит: <b>{profit:+.6f} BNB</b>"
    ))


def alert_triangle_profit(
    token_a: str, token_b: str, token_c: str,
    dex1: str, dex2: str, dex3: str,
    net_profit: float, total_profit: float,
):
    send("PROFIT", (
        f"💰 <b>Треугольный арбитраж выполнен!</b>\n"
        f"Маршрут: <b>{token_a}→{token_b}→{token_c}→{token_a}</b>\n"
        f"DEX: {dex1} / {dex2} / {dex3}\n"
        f"Профит: <b>{net_profit:+.6f} BNB</b>\n"
        f"Итого: {total_profit:+.6f} BNB"
    ))


def alert_trade_failed(pair: str, reason: str, is_triangle: bool = False, potential_profit: float = 0.0):
    kind = "Треугольник" if is_triangle else "Сделка"
    profit_line = f"\nПотенциал: <b>+{potential_profit:.6f} BNB</b>" if potential_profit > 0 else ""
    send("FAIL", (
        f"❌ <b>{kind} не выполнен</b>\n"
        f"Маршрут: <code>{pair}</code>\n"
        f"Причина: {reason}"
        f"{profit_line}"
    ))


def is_configured() -> bool:
    """Проверяет, настроен ли Telegram."""
    return bool(_ENABLED and _TOKEN and _CHAT_ID)


# =============================================================================
# ПРИЁМ КОМАНД (/balance и др.)
# =============================================================================

def _reply(chat_id, text: str):
    """Отвечает в конкретный чат (не обязательно совпадает с _CHAT_ID)."""
    if not _TOKEN:
        return
    try:
        import urllib.request, urllib.parse, json
        url  = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.debug(f"Telegram _reply failed: {e}")


def _handle_command(text: str, chat_id, w3, flash_client):
    """Разбирает команду и отвечает."""
    cmd = text.strip().split()[0].lower().split("@")[0]  # /balance@botname → /balance

    if cmd == "/balance":
        lines = ["💼 <b>Баланс</b>\n"]

        # Баланс кошелька (нативный токен)
        try:
            from chain import get_bnb_balance
            from config import WALLET_ADDRESS, CHAIN, TOKENS
            native = get_bnb_balance(w3)
            chain_label = CHAIN.replace("_mainnet", "").upper()
            native_sym  = {"bsc": "BNB", "polygon": "MATIC", "arbitrum": "ETH", "avalanche": "AVAX"}.get(chain_label.lower(), "BNB")
            lines.append(f"<b>Кошелёк ({native_sym}):</b> {native:.6f}")
        except Exception as e:
            lines.append(f"Кошелёк: ошибка ({e})")

        # Баланс контракта по каждому токену
        if flash_client:
            try:
                balances = flash_client.get_balances()
                nonzero  = {sym: val for sym, val in balances.items() if val > 0}
                if nonzero:
                    lines.append("\n<b>Контракт:</b>")
                    for sym, val in nonzero.items():
                        lines.append(f"  {sym}: {val:.6f}")
                else:
                    lines.append("\nКонтракт: пустой")
            except Exception as e:
                lines.append(f"\nКонтракт: ошибка ({e})")
        else:
            lines.append("\nКонтракт: не инициализирован")

        _reply(chat_id, "\n".join(lines))

    elif cmd == "/pause":
        set_paused(True)
        _reply(chat_id, "⏸ <b>Бот на паузе.</b> Сканирование остановлено.\nДля возобновления: /resume")

    elif cmd == "/resume":
        set_paused(False)
        _reply(chat_id, "▶️ <b>Бот возобновлён.</b> Сканирование запущено.")

    elif cmd == "/help":
        _reply(chat_id, (
            "🤖 <b>Доступные команды:</b>\n"
            "/balance — баланс кошелька и контракта\n"
            "/pause — поставить бота на паузу\n"
            "/resume — возобновить работу\n"
            "/help — эта справка"
        ))


def _poll_loop(w3, flash_client):
    """
    Фоновый поток: long-polling getUpdates.
    Отвечает только на сообщения от _CHAT_ID (защита от чужих).
    """
    import urllib.request, json

    offset = 0
    allowed_chat = str(_CHAT_ID).strip()

    while True:
        try:
            url = (
                f"https://api.telegram.org/bot{_TOKEN}/getUpdates"
                f"?offset={offset}&timeout=30&allowed_updates=[\"message\"]"
            )
            with urllib.request.urlopen(url, timeout=35) as resp:
                data = json.loads(resp.read())

            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if not text.startswith("/"):
                    continue
                if chat_id != allowed_chat:
                    log.debug(f"Telegram: команда от чужого chat_id {chat_id} — игнор")
                    continue

                log.info(f"Telegram команда: {text.strip()}")
                _handle_command(text, chat_id, w3, flash_client)

        except Exception as e:
            log.debug(f"Telegram poll error: {e}")
            time.sleep(10)


def start_commander(w3, flash_client):
    """
    Запускает фоновый поток приёма команд из Telegram.
    Вызывать из main.py после инициализации w3 и flash_client.
    """
    if not is_configured():
        return
    t = threading.Thread(
        target=_poll_loop,
        args=(w3, flash_client),
        daemon=True,
        name="tg-commander",
    )
    t.start()
    log.info("Telegram commander запущен (/balance, /help)")
