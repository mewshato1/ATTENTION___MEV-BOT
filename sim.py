from web3 import Web3

w3 = Web3(Web3.HTTPProvider("https://purple-alien-slug.bsc.quiknode.pro/e0f4774951af73bfd8feda805fcf2a648bed9175/"))

ABI = [
    {
        "name": "simulateTriangle",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "tokenC", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "dex1", "type": "string"},
            {"name": "dex2", "type": "string"},
            {"name": "dex3", "type": "string"},
        ],
        "outputs": [
            {"name": "finalAmount", "type": "uint256"},
            {"name": "profit", "type": "int256"},
            {"name": "isProfitableAfterAave", "type": "bool"},
        ],
    }
]

c = w3.eth.contract(address="0x30bDAa5553b06607f0278C066fc89D2f7A053334", abi=ABI)

USDT = "0x55d398326f99059fF775485246999027B3197955"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
DOT  = "0x7083609fCE4d1d8Dc0C979AAb8c869Ea2C873402"

amount = 10**18  # 1 USDT

print("=== BiSwap / BiSwap / MDEX ===")
try:
    r = c.functions.simulateTriangle(USDT, WBNB, DOT, amount, "BiSwap", "BiSwap", "MDEX").call()
    print(f"  finalAmount : {r[0] / 1e18:.8f} USDT")
    print(f"  profit      : {r[1] / 1e18:.8f} USDT")
    print(f"  profitable  : {r[2]}")
except Exception as e:
    print(f"  ERROR: {e}")

print("=== BiSwap / PancakeSwap_V2 / MDEX ===")
try:
    r = c.functions.simulateTriangle(USDT, WBNB, DOT, amount, "BiSwap", "PancakeSwap_V2", "MDEX").call()
    print(f"  finalAmount : {r[0] / 1e18:.8f} USDT")
    print(f"  profit      : {r[1] / 1e18:.8f} USDT")
    print(f"  profitable  : {r[2]}")
except Exception as e:
    print(f"  ERROR: {e}")
