# =============================================================================
# config.py — Конфигурация MEV-бота для BNB Chain
# =============================================================================
# Перед запуском: создай файл .env в папке проекта и заполни значения.
# Пример .env файла смотри ниже.
# =============================================================================

import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# СЕТЬ
# =============================================================================

# Публичные RPC — бесплатны, но медленные. Для боевого режима — замени на QuickNode/Alchemy.
# Пул из нескольких бесплатных нод для отказоустойчивости (основной + резервные)
BSC_RPC_HTTP  = os.getenv("BSC_RPC_HTTP",  "https://bsc-rpc.publicnode.com")
BSC_RPC_HTTP_FALLBACKS = [
    "https://rpc.ankr.com/bsc",
    "https://bsc-dataseed.bnbchain.org/",
    "https://bsc-dataseed1.binance.org/",
    "https://bsc-dataseed2.binance.org/",
    "https://bsc.nodereal.io/",
    "https://bsc-dataseed-public.bnbchain.org/",
    "https://bnb.rpc.subquery.network/public",
    "https://bsc.drpc.org/",
]
BSC_RPC_WS    = os.getenv("BSC_RPC_WS",    "wss://bsc-rpc.publicnode.com")
BSC_RPC_WS_FALLBACKS = [
    "wss://bsc-rpc.publicnode.com",
    "wss://bsc.drpc.org",
]
CHAIN_ID      = 56   # BSC Mainnet

# Режим сканирования: "poll" (time.sleep) или "block" (подписка на блоки через WS)
SCAN_MODE = os.getenv("SCAN_MODE", "block")

# =============================================================================
# КОШЕЛЁК
# ВАЖНО: Используй только отдельный hot wallet с небольшой суммой!
# =============================================================================

WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
PRIVATE_KEY    = os.getenv("PRIVATE_KEY",    "")  # Никогда не коммить в git!

# =============================================================================
# РЕЖИМ РАБОТЫ
# =============================================================================

# DRY_RUN = True  → бот находит возможности и считает прибыль, но НЕ отправляет транзакции.
#                   Включи для тестирования логики без риска потерять деньги.
# DRY_RUN = False → реальные транзакции. Только когда убедишься что всё работает.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# =============================================================================
# ПАРАМЕТРЫ ГАЗА
# =============================================================================

GAS_LIMIT         = int(os.getenv("GAS_LIMIT", "300000"))     # Реальный расход flashloan ~200-300k
GAS_LIMIT_SEQ     = int(os.getenv("GAS_LIMIT_SEQ", "250000"))  # Один свап ~120-150k
USE_DYNAMIC_GAS   = os.getenv("USE_DYNAMIC_GAS", "true").lower() == "true"  # eth_estimateGas
GAS_PRICE_MULT    = float(os.getenv("GAS_PRICE_MULT", "1.2"))   # +20% к базовому
MAX_GAS_GWEI      = float(os.getenv("MAX_GAS_GWEI", "10.0"))    # Не торговать если газ выше

# =============================================================================
# ПАРАМЕТРЫ ТОРГОВЛИ
# =============================================================================

MIN_PROFIT_BNB    = float(os.getenv("MIN_PROFIT_BNB", "0.001"))  # Мин. прибыль в BNB
MAX_TRADE_BNB     = float(os.getenv("MAX_TRADE_BNB", "0.5"))     # Макс. размер сделки
SLIPPAGE_PCT      = float(os.getenv("SLIPPAGE_PCT", "0.5"))      # Допустимое проскальзывание %
USE_OPTIMAL_SIZE  = os.getenv("USE_OPTIMAL_SIZE", "true").lower() == "true"  # Авторасчёт размера

# =============================================================================
# ИНТЕРВАЛЫ СКАНИРОВАНИЯ
# =============================================================================

SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "0.5"))  # Пауза между циклами (fallback)
# При SCAN_MODE=block этот интервал игнорируется — скан запускается по новому блоку

# =============================================================================
# ТОКЕНЫ BSC (адреса в checksum-формате)
# =============================================================================

TOKENS = {
    "WBNB":  "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    "USDT":  "0x55d398326f99059fF775485246999027B3197955",
    "USDC":  "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    "BUSD":  "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56",
    "CAKE":  "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82",
    "ETH":   "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
    "BTCB":  "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c",
    "XVS":   "0xcF6BB5389c92Bdda8a3747Ddb454cB7a64626C63",
    "TWT":   "0x4B0F1812e5Df2A09796481Ff14017e6005508003",
    "ADA":   "0x3EE2200Efb3400fAbB9AacF31297cBdD1d435D47",
    "DOT":   "0x7083609fCE4d1d8Dc0C979AAb8c869Ea2C873402",
    "LINK":  "0xF8A0BF9cF54Bb92F17374d9e9A321E6a111a51bD",
}

# =============================================================================
# ТОРГОВЫЕ ПАРЫ ДЛЯ МОНИТОРИНГА
# Формат: (token_a, token_b)
# =============================================================================

WATCH_PAIRS = [
    # Основные пары — максимальная ликвидность на всех DEX
    ("WBNB", "USDT"),
    ("WBNB", "BUSD"),
    ("WBNB", "USDC"),
    ("ETH",  "WBNB"),
    ("ETH",  "USDT"),
    ("CAKE", "WBNB"),
    # Стейблкоины — небольшой но стабильный спред
    ("USDT", "BUSD"),
    ("USDT", "USDC"),
    # BTC
    ("BTCB", "WBNB"),
    ("BTCB", "USDT"),
    # Альт-пары — меньше MEV-конкурентов, выше шанс устаревших котировок
    ("XVS",  "WBNB"),
    ("XVS",  "USDT"),
    ("TWT",  "WBNB"),
    ("TWT",  "USDT"),
    ("ADA",  "WBNB"),
    ("ADA",  "USDT"),
    ("DOT",  "WBNB"),
    ("DOT",  "USDT"),
    ("LINK", "WBNB"),
    ("LINK", "USDT"),
]

# =============================================================================
# DEX — ФАБРИКИ И РОУТЕРЫ (V2-совместимые)
# =============================================================================

DEX = {
    "PancakeSwap_V2": {
        "factory": "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",
        "router":  "0x10ED43C718714eb63d5aA57B78B54704E256024E",
        "fee_bps": 25,   # 0.25%
    },
    "BiSwap": {
        "factory": "0x858E3312ed3A876947EA49d572A7C42DE08af7EE",
        "router":  "0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8",
        "fee_bps": 10,   # 0.10%
    },
    "ApeSwap": {
        "factory": "0x0841BD0B734E4F5853f0dD8d7Ea041c241fb0Da6",
        "router":  "0xcF0feBd3f17CEf5b47b0cD257aCf6025c5BFf3b7",
        "fee_bps": 20,   # 0.20%
    },
    # SushiSwap, MDEX, BabySwap оставлены закомментированными — TVL <$10k.
    # ApeSwap оставлен: имеет ~$100-300k для WBNB/USDT, MIN_RESERVE_WEI=50BNB отфильтрует мелкие пары.
}

# =============================================================================
# DEX V3 — PancakeSwap V3 (концентрированная ликвидность)
# fee_bps  = fee_pips / 100  (используется в calc_amount_out с виртуальными резервами)
# fee_pips = единицы PancakeSwap V3 (1e-6)
# Пример: 500 pips = 0.05% → fee_bps = 5
# =============================================================================

_PANCAKE_V3_FACTORY = "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"

DEX_V3 = {
    "PancakeSwap_V3_100":  {"factory": _PANCAKE_V3_FACTORY, "fee_bps":  1, "fee_pips":  100},
    "PancakeSwap_V3_500":  {"factory": _PANCAKE_V3_FACTORY, "fee_bps":  5, "fee_pips":  500},
    "PancakeSwap_V3_2500": {"factory": _PANCAKE_V3_FACTORY, "fee_bps": 25, "fee_pips": 2500},
}

# QuoterV2 — точные котировки V3 без виртуальных резервов.
# Вызывается через Multicall3 батчем (0 дополнительных RPC).
PANCAKE_V3_QUOTER = "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"

# =============================================================================
# ЛОГИРОВАНИЕ
# =============================================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")   # DEBUG / INFO / WARNING / ERROR
LOG_FILE  = os.getenv("LOG_FILE",  "mev_bot.log")


# =============================================================================
# ПРИМЕР ФАЙЛА .env (скопируй в .env и заполни)
# =============================================================================
# BSC_RPC_HTTP=https://your-quicknode-endpoint.bsc.quiknode.pro/YOUR_KEY/
# BSC_RPC_WS=wss://your-quicknode-endpoint.bsc.quiknode.pro/YOUR_KEY/
# WALLET_ADDRESS=0xYourWalletAddress
# PRIVATE_KEY=your_private_key_here
# DRY_RUN=true
# MIN_PROFIT_BNB=0.003
# MAX_TRADE_BNB=0.5
# GAS_PRICE_MULT=1.2
# MAX_GAS_GWEI=10.0

# =============================================================================
# MULTICALL3
# =============================================================================
# Контракт развёрнут на BSC по стандартному адресу
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Максимальное число вызовов в одном батче
# Слишком большой батч → нода может вернуть ошибку
MULTICALL_BATCH_SIZE = int(os.getenv("MULTICALL_BATCH_SIZE", "300"))  # 300 = все пары в 1 батч (276 вызовов), платные: 500+

# Время жизни кэша адресов пулов (секунды). По истечении — перезапрос.
PAIR_CACHE_TTL = int(os.getenv("PAIR_CACHE_TTL", "3600"))  # 1 час

# =============================================================================
# FLASHARBITRAGE КОНТРАКТ
# =============================================================================

# Адрес задеплоенного FlashArbitrage.sol
# Заполни после: npx hardhat run scripts/deployFlash.js --network bsc_mainnet
FLASH_CONTRACT_ADDRESS = os.getenv("FLASH_CONTRACT_ADDRESS", "")

# Провайдер флэшлоана: "aave" (дешевле, 0.05%) или "pancake" (0.3%, нужен pair address)
# AAVE доступен если токен есть в пуле (WBNB, USDT, USDC, ETH)
# PancakeSwap всегда доступен для любого пула
FLASH_PROVIDER = os.getenv("FLASH_PROVIDER", "aave").lower()

# Адрес AAVE V3 Pool на BSC Mainnet
AAVE_POOL_ADDRESS = os.getenv(
    "AAVE_POOL_ADDRESS",
    "0x6807dc923806fE8Fd134338EABCA509979a7e0cB"
)

# Токены доступные в AAVE V3 пуле на BSC
# Только для них можно использовать flashAave()
# Для остальных — автоматически fallback на flashPancake()
AAVE_SUPPORTED_TOKENS = {
    "WBNB", "USDT", "USDC", "BUSD", "ETH",
}

# Порог спреда для автовыбора треугольного арбитража (%)
# Если двусторонний спред меньше этого — пробуем треугольный
TRIANGLE_ARB_MIN_SPREAD = float(os.getenv("TRIANGLE_ARB_MIN_SPREAD", "0.5"))

# Минимальная прибыль после вычета AAVE/Pancake fee (в BNB)
# ВАЖНО: должна быть чуть выше чем MIN_PROFIT_BNB чтобы покрыть fee
FLASH_MIN_PROFIT_BNB = float(os.getenv("FLASH_MIN_PROFIT_BNB", "0.0005"))

# =============================================================================
# MULTI-CHAIN ПОДДЕРЖКА
# =============================================================================
# По умолчанию CHAIN=bsc_mainnet.
# Для добавления другой сети: добавь блок в chains_config.py и задай CHAIN= в .env
# =============================================================================

CHAIN = os.getenv("CHAIN", "bsc_mainnet")

if CHAIN != "bsc_mainnet":
    try:
        from chains_config import CHAINS as _ALL_CHAINS
        _cfg = _ALL_CHAINS[CHAIN]
    except KeyError:
        from chains_config import CHAINS as _ALL_CHAINS
        raise ValueError(
            f"Неизвестный чейн: '{CHAIN}'. "
            f"Доступные: {', '.join(_ALL_CHAINS.keys())}"
        )

    # Перезаписываем сетевые параметры
    BSC_RPC_HTTP           = _cfg["rpc_http"][0]
    BSC_RPC_HTTP_FALLBACKS = _cfg["rpc_http"][1:]
    BSC_RPC_WS             = _cfg["rpc_ws"][0]
    BSC_RPC_WS_FALLBACKS   = _cfg["rpc_ws"][1:]
    CHAIN_ID               = _cfg["chain_id"]

    # Перезаписываем токены и DEX
    TOKENS      = _cfg["tokens"]
    DEX         = _cfg["dex"]
    DEX_V3      = {}   # V3 поддерживается только на BSC пока
    WATCH_PAIRS = _cfg["pairs"]

    # AAVE
    AAVE_POOL_ADDRESS     = _cfg["aave_pool"]
    AAVE_SUPPORTED_TOKENS = _cfg["aave_tokens"]

    # Контракт — сначала ищем {CHAIN}_CONTRACT, затем общий FLASH_CONTRACT_ADDRESS
    FLASH_CONTRACT_ADDRESS = os.getenv(
        _cfg.get("contract_env", ""),
        os.getenv("FLASH_CONTRACT_ADDRESS", ""),
    )

    # На не-BSC чейнах нет PancakeSwap → только AAVE flashloan
    FLASH_PROVIDER = "aave"

    # Multicall3 (тот же адрес на всех EVM чейнах)
    MULTICALL3_ADDRESS = _cfg["multicall3"]
