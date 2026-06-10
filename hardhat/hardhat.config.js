require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config({ path: "../.env" });

const PRIVATE_KEY = process.env.PRIVATE_KEY || "0x" + "0".repeat(64);

// RPC с fallback на публичные ноды
const rpc = (envKey, pub) => process.env[envKey] || pub;

module.exports = {
  solidity: {
    version: "0.8.19",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      viaIR: true,
    },
  },
  networks: {
    hardhat: {
      chainId: 56,
    },

    // ── BNB Chain ─────────────────────────────────────────────────────────
    bsc_testnet: {
      url:      "https://data-seed-prebsc-1-s1.binance.org:8545/",
      chainId:  97,
      accounts: [PRIVATE_KEY],
    },
    bsc_mainnet: {
      url:      rpc("BSC_RPC_HTTP", "https://bsc-dataseed1.binance.org/"),
      chainId:  56,
      accounts: [PRIVATE_KEY],
    },

    // ── Polygon ───────────────────────────────────────────────────────────
    polygon: {
      url:      rpc("POLYGON_RPC_HTTP", "https://polygon-rpc.com"),
      chainId:  137,
      accounts: [PRIVATE_KEY],
    },
    polygon_testnet: {
      url:      rpc("MUMBAI_RPC_HTTP", "https://rpc-mumbai.maticvigil.com"),
      chainId:  80001,
      accounts: [PRIVATE_KEY],
    },

    // ── Arbitrum One ──────────────────────────────────────────────────────
    arbitrum: {
      url:      rpc("ARBITRUM_RPC_HTTP", "https://arb1.arbitrum.io/rpc"),
      chainId:  42161,
      accounts: [PRIVATE_KEY],
    },
    arbitrum_testnet: {
      url:      rpc("ARBITRUM_GOERLI_RPC_HTTP", "https://goerli-rollup.arbitrum.io/rpc"),
      chainId:  421613,
      accounts: [PRIVATE_KEY],
    },

    // ── Avalanche C-Chain ─────────────────────────────────────────────────
    avalanche: {
      url:      rpc("AVAX_RPC_HTTP", "https://api.avax.network/ext/bc/C/rpc"),
      chainId:  43114,
      accounts: [PRIVATE_KEY],
    },
    avalanche_testnet: {
      url:      rpc("AVAX_FUJI_RPC_HTTP", "https://api.avax-test.network/ext/bc/C/rpc"),
      chainId:  43113,
      accounts: [PRIVATE_KEY],
    },
  },

  etherscan: {
    apiKey: {
      // BNB Chain
      bsc:                process.env.BSCSCAN_API_KEY    || "",
      bscTestnet:         process.env.BSCSCAN_API_KEY    || "",
      // Polygon
      polygon:            process.env.POLYGONSCAN_API_KEY || "",
      polygonMumbai:      process.env.POLYGONSCAN_API_KEY || "",
      // Arbitrum
      arbitrumOne:        process.env.ARBISCAN_API_KEY    || "",
      arbitrumGoerli:     process.env.ARBISCAN_API_KEY    || "",
      // Avalanche (через snowtrace)
      avalanche:          process.env.SNOWTRACE_API_KEY   || "",
      avalancheFujiTestnet: process.env.SNOWTRACE_API_KEY || "",
    },
  },

  gasReporter: {
    enabled:  process.env.REPORT_GAS === "true",
    currency: "USD",
    token:    "BNB",
  },
};
