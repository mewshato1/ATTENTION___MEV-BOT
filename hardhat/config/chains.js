// =============================================================================
// config/chains.js — Конфиги всех поддерживаемых чейнов
// =============================================================================
// Добавить новый чейн: скопировать блок, поменять адреса.
// Деплой: npx hardhat run scripts/deployMultiChain.js --network polygon
// =============================================================================

module.exports = {

  // ── BNB Chain ───────────────────────────────────────────────────────────────
  bsc_mainnet: {
    chainId:     56,
    nativeToken: "BNB",
    aavePool:    "0x6807dc923806fE8Fd134338EABCA509979a7e0cB",
    routers: [
      { name: "PancakeSwap_V2", address: "0x10ED43C718714eb63d5aA57B78B54704E256024E", feeBps: 25  },
      { name: "BiSwap",         address: "0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8", feeBps: 10  },
      { name: "ApeSwap",        address: "0xcF0feBd3f17CEf5b47b0cD257aCf6025c5BFf3b7", feeBps: 20  },
      { name: "BabySwap",       address: "0x325E343f1dE602396E256B67eFd1F61C3A6B38Bd", feeBps: 30  },
      { name: "MDEX",           address: "0x7DAe51BD3E3376B8c7c4900E9107f12Be3AF1bA8", feeBps: 30  },
    ],
    tokens: {
      WBNB: "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
      USDT: "0x55d398326f99059fF775485246999027B3197955",
      USDC: "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
      BUSD: "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56",
      CAKE: "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82",
      WETH: "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
    },
    // Основные пары для сканирования
    pairs: [
      ["WBNB", "USDT"],
      ["WBNB", "USDC"],
      ["WBNB", "BUSD"],
      ["CAKE", "WBNB"],
      ["WETH", "WBNB"],
      ["USDT", "USDC"],
    ],
  },

  // ── Polygon ─────────────────────────────────────────────────────────────────
  polygon: {
    chainId:     137,
    nativeToken: "MATIC",
    // AAVE V3 Pool — одинаков на Polygon, Arbitrum, Avalanche, Optimism
    aavePool:    "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    routers: [
      { name: "QuickSwap",  address: "0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff", feeBps: 30 },
      { name: "SushiSwap",  address: "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506", feeBps: 30 },
      { name: "ApeSwap",    address: "0xC0788A3aD43d79aa53B09c2EaCc313A787d1d607", feeBps: 20 },
      { name: "Dfyn",       address: "0xA102072A4C07F06EC3B4900FDC4C7B80b6c57429", feeBps: 30 },
    ],
    tokens: {
      WMATIC: "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
      USDT:   "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
      USDC:   "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
      WETH:   "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
      WBTC:   "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6",
      DAI:    "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063",
    },
    pairs: [
      ["WMATIC", "USDT"],
      ["WMATIC", "USDC"],
      ["WETH",   "USDT"],
      ["WETH",   "USDC"],
      ["USDT",   "USDC"],
      ["WBTC",   "WETH"],
    ],
  },

  // ── Arbitrum One ─────────────────────────────────────────────────────────────
  arbitrum: {
    chainId:     42161,
    nativeToken: "ETH",
    aavePool:    "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    routers: [
      // Camelot — основной V2-совместимый DEX на Arbitrum
      { name: "Camelot",    address: "0xc873fEcbd354f5A56E00E710B90EF4201db2448d", feeBps: 30 },
      // SushiSwap V2 на Arbitrum
      { name: "SushiSwap",  address: "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506", feeBps: 30 },
      // Uniswap V2 (через универсальный роутер развёрнут на Arbitrum)
      { name: "UniswapV2",  address: "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24", feeBps: 30 },
    ],
    tokens: {
      WETH:  "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
      USDT:  "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
      USDC:  "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", // native USDC
      USDCe: "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8", // bridged USDC.e
      WBTC:  "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
      ARB:   "0x912CE59144191C1204E64559FE8253a0e49E6548",
    },
    pairs: [
      ["WETH",  "USDT"],
      ["WETH",  "USDC"],
      ["WETH",  "USDCe"],
      ["USDT",  "USDC"],
      ["WBTC",  "WETH"],
      ["ARB",   "WETH"],
    ],
  },

  // ── Avalanche C-Chain ────────────────────────────────────────────────────────
  avalanche: {
    chainId:     43114,
    nativeToken: "AVAX",
    aavePool:    "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    routers: [
      // Trader Joe V1 (UniswapV2-совместимый)
      { name: "TraderJoe",  address: "0x60aE616a2155Ee3d9A68541Ba4544862310933d4", feeBps: 30 },
      // Pangolin
      { name: "Pangolin",   address: "0xE54Ca86531e17Ef3616d22Ca28b0D458b6C89106", feeBps: 30 },
      // SushiSwap
      { name: "SushiSwap",  address: "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506", feeBps: 30 },
    ],
    tokens: {
      WAVAX: "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
      USDT:  "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
      USDC:  "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6",
      WETH:  "0x49D5c2BdFfac6CE2BFdB6640F4F80f226bc10bAB",
      WBTC:  "0x50b7545627a5162F82A992c33b87aDc75187B218",
    },
    pairs: [
      ["WAVAX", "USDT"],
      ["WAVAX", "USDC"],
      ["WAVAX", "WETH"],
      ["WETH",  "USDT"],
      ["USDT",  "USDC"],
      ["WBTC",  "WETH"],
    ],
  },

};
