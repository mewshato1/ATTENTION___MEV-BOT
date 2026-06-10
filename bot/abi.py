# =============================================================================
# abi.py — ABI контрактов PancakeSwap V2 / UniswapV2-совместимых DEX
# =============================================================================
# ABI — это интерфейс контракта: описывает какие функции можно вызывать
# и какие данные они возвращают. Без ABI web3 не знает как общаться с контрактом.
# =============================================================================

# Фабрика DEX: создаёт пары и хранит их адреса
FACTORY_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"}
        ],
        "name": "getPair",
        "outputs": [{"name": "pair", "type": "address"}],
        "type": "function"
    }
]

# Пара (Pool): хранит резервы двух токенов и отдаёт цену
PAIR_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "reserve0", "type": "uint112"},
            {"name": "reserve1", "type": "uint112"},
            {"name": "blockTimestampLast", "type": "uint32"}
        ],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token1",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function"
    }
]

# Роутер: исполняет свапы
ROUTER_ABI = [
    {
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactTokensForTokens",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactETHForTokens",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path", "type": "address[]"}
        ],
        "name": "getAmountsOut",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Multicall3 — батчинг множества view-вызовов в один RPC-запрос
# Адрес на BSC: 0xcA11bde05977b3631167028862bE2a173976CA11
MULTICALL3_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "target",   "type": "address"},
                    {"name": "callData", "type": "bytes"},
                ],
                "name":       "calls",
                "type":       "tuple[]",
            }
        ],
        "name":             "aggregate",
        "outputs": [
            {"name": "blockNumber", "type": "uint256"},
            {"name": "returnData",  "type": "bytes[]"},
        ],
        "stateMutability": "view",
        "type":            "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"name": "target",       "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData",     "type": "bytes"},
                ],
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name":    "aggregate3",
        "outputs": [
            {
                "components": [
                    {"name": "success",    "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
                "name": "returnData",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "view",
        "type":            "function",
    },
]

# V3 QuoterV2: точные котировки с учётом реального состояния пула
# Адрес на BSC: 0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25aC
# quoteExactInputSingle — симулирует своп и возвращает точный amountOut.
# Вызывается через eth_call (stateMutability=nonpayable, но работает как view в eth_call).
V3_QUOTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn",           "type": "address"},
                    {"name": "tokenOut",          "type": "address"},
                    {"name": "amountIn",          "type": "uint256"},
                    {"name": "fee",               "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut",               "type": "uint256"},
            {"name": "sqrtPriceX96After",       "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate",             "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# V3 Factory: создаёт пулы с разными fee tier
V3_FACTORY_ABI = [
    {
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "fee",    "type": "uint24"}
        ],
        "name": "getPool",
        "outputs": [{"name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# ERC-20: стандартный интерфейс токена (нужен для approve и balanceOf)
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    }
]
