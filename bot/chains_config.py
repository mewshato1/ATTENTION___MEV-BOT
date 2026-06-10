# =============================================================================
# chains_config.py — Конфиги поддерживаемых чейнов для Python-бота
# =============================================================================
# Используется config.py при CHAIN != "bsc_mainnet".
# =============================================================================

CHAINS = {

    # ── BNB Chain ───────────────────────────────────────────────────────────────
    "bsc_mainnet": {
        "chain_id":    56,
        "native_token": "BNB",
        "rpc_http": [
            "https://bsc-rpc.publicnode.com",
            "https://bsc-dataseed.bnbchain.org/",
            "https://bsc-dataseed1.binance.org/",
            "https://bsc-dataseed2.binance.org/",
        ],
        "rpc_ws": [
            "wss://bsc-rpc.publicnode.com",
            "wss://bsc.drpc.org",
        ],
        "aave_pool":   "0x6807dc923806fE8Fd134338EABCA509979a7e0cB",
        "aave_tokens": {"WBNB", "USDT", "USDC", "BUSD", "ETH"},
        "multicall3":  "0xcA11bde05977b3631167028862bE2a173976CA11",
        "contract_env": "FLASH_CONTRACT_ADDRESS",
        "tokens": {
            "WBNB":  "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
            "USDT":  "0x55d398326f99059fF775485246999027B3197955",
            "USDC":  "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
            "BUSD":  "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56",
            "CAKE":  "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82",
            "ETH":   "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
            "BTCB":  "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c",
        },
        "dex": {
            "PancakeSwap_V2": {
                "factory": "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",
                "router":  "0x10ED43C718714eb63d5aA57B78B54704E256024E",
                "fee_bps": 25,
            },
            "BiSwap": {
                "factory": "0x858E3312ed3A876947EA49d572A7C42DE08af7EE",
                "router":  "0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8",
                "fee_bps": 10,
            },
            "ApeSwap": {
                "factory": "0x0841BD0B734E4F5853f0dD8d7Ea041c241fb0Da6",
                "router":  "0xcF0feBd3f17CEf5b47b0cD257aCf6025c5BFf3b7",
                "fee_bps": 20,
            },
        },
        "pairs": [
            ("WBNB", "USDT"), ("WBNB", "BUSD"), ("WBNB", "USDC"),
            ("CAKE", "WBNB"), ("ETH", "WBNB"), ("ETH", "USDT"),
            ("USDT", "BUSD"), ("USDT", "USDC"), ("BTCB", "WBNB"),
        ],
    },
}
