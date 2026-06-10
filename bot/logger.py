# =============================================================================
# logger.py — Цветной логгер для терминала + запись в файл
# =============================================================================

import logging
import sys
from datetime import datetime
from config import LOG_LEVEL, LOG_FILE


# ANSI-цвета для терминала
class Colors:
    RESET   = "\033[0m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    BLUE    = "\033[94m"
    CYAN    = "\033[96m"
    GREY    = "\033[90m"
    BOLD    = "\033[1m"
    ORANGE  = "\033[38;5;208m"


class ColorFormatter(logging.Formatter):
    """Форматтер с цветами для терминала."""

    LEVEL_COLORS = {
        "DEBUG":    Colors.GREY,
        "INFO":     Colors.BLUE,
        "WARNING":  Colors.YELLOW,
        "ERROR":    Colors.RED,
        "CRITICAL": Colors.RED + Colors.BOLD,
        "SUCCESS":  Colors.GREEN,
        "PROFIT":   Colors.ORANGE + Colors.BOLD,
    }

    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelname, Colors.RESET)
        time_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        level = f"{color}{record.levelname:<8}{Colors.RESET}"
        msg = record.getMessage()

        # Подсветка ключевых слов в сообщениях
        if "PROFIT" in msg or "ARB FOUND" in msg:
            msg = f"{Colors.GREEN}{Colors.BOLD}{msg}{Colors.RESET}"
        elif "ERROR" in msg.upper() or "FAILED" in msg.upper():
            msg = f"{Colors.RED}{msg}{Colors.RESET}"
        elif "WARN" in msg.upper() or "SKIP" in msg.upper():
            msg = f"{Colors.YELLOW}{msg}{Colors.RESET}"

        return f"{Colors.GREY}{time_str}{Colors.RESET}  {level}  {msg}"


# Добавляем кастомные уровни
SUCCESS_LEVEL = 25
PROFIT_LEVEL  = 26
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")
logging.addLevelName(PROFIT_LEVEL,  "PROFIT")


def success(self, msg, *args, **kwargs):
    if self.isEnabledFor(SUCCESS_LEVEL):
        self._log(SUCCESS_LEVEL, msg, args, **kwargs)

def profit(self, msg, *args, **kwargs):
    if self.isEnabledFor(PROFIT_LEVEL):
        self._log(PROFIT_LEVEL, msg, args, **kwargs)

logging.Logger.success = success
logging.Logger.profit  = profit


def get_logger(name: str = "mev_bot") -> logging.Logger:
    """
    Создаёт и возвращает логгер.
    Пишет в терминал (с цветами) и в файл (без цветов).
    """
    logger = logging.getLogger(name)

    # Не добавлять handlers повторно
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # --- Терминал (с цветами) ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColorFormatter())
    logger.addHandler(console_handler)

    # --- Файл (без цветов) ---
    try:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        plain_fmt = logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler.setFormatter(plain_fmt)
        logger.addHandler(file_handler)
    except Exception as e:
        logger.warning(f"Не удалось открыть лог-файл {LOG_FILE}: {e}")

    return logger
