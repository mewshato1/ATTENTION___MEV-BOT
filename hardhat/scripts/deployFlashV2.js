// =============================================================================
// deployFlashV2.js — Деплой FlashArbitrageV2 + автоматический approveMax
// =============================================================================
// Запуск: npx hardhat run scripts/deployFlashV2.js --network bsc_mainnet
// =============================================================================

const hre = require("hardhat");
const fs  = require("fs");

async function main() {
    const [deployer] = await hre.ethers.getSigners();
    console.log("Deploying with:", deployer.address);
    console.log("Balance:", hre.ethers.formatEther(await hre.ethers.provider.getBalance(deployer.address)), "BNB");

    // ── Параметры ──────────────────────────────────────────────────────────
    const AAVE_POOL = "0x6807dc923806fE8Fd134338EABCA509979a7e0cB"; // BSC Mainnet

    const routers = [
        "0x10ED43C718714eb63d5aA57B78B54704E256024E", // PancakeSwap V2
        "0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8", // BiSwap
        "0xcF0feBd3f17CEf5b47b0cD257aCf6025c5BFf3b7", // ApeSwap
        "0x325E343f1dE602396E256B67eFd1F61C3A6B38Bd", // BabySwap
        "0x7DAe51BD3E3376B8c7c4900E9107f12Be3AF1bA8", // MDEX
    ];
    const names = [
        "PancakeSwap_V2",
        "BiSwap",
        "ApeSwap",
        "BabySwap",
        "MDEX",
    ];

    // Токены для approveMax
    const tokens = [
        "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", // WBNB
        "0x55d398326f99059fF775485246999027B3197955", // USDT
        "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", // USDC
        "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56", // BUSD
        "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", // CAKE
        "0x2170Ed0880ac9A755fd29B2688956BD959F933F8", // ETH
    ];

    // ── Деплой ─────────────────────────────────────────────────────────────
    console.log("\nDeploying FlashArbitrageV2...");
    const Factory = await hre.ethers.getContractFactory("FlashArbitrageV2");
    const contract = await Factory.deploy(AAVE_POOL, routers, names);
    await contract.waitForDeployment();

    const addr = await contract.getAddress();
    console.log(`✅ FlashArbitrageV2 deployed: ${addr}`);

    // ── Массовый approveMax ────────────────────────────────────────────────
    // Экономит ~46000 gas × 2 свапа × каждая сделка = ~92000 gas/сделка
    console.log("\nApproving all token/router combinations...");
    console.log(`  ${tokens.length} tokens × ${routers.length} routers = ${tokens.length * routers.length} approvals`);

    const tx = await contract.approveMaxBatch(tokens, routers);
    const receipt = await tx.wait();
    console.log(`✅ approveMaxBatch done! Gas used: ${receipt.gasUsed.toString()}`);

    // Также approve для AAVE Pool (для возврата долга)
    console.log("\nApproving tokens for AAVE Pool...");
    for (const token of tokens) {
        const tx2 = await contract.approveMax(token, AAVE_POOL);
        await tx2.wait();
    }
    console.log("✅ AAVE approvals done");

    // ── Сохраняем адрес ────────────────────────────────────────────────────
    const result = {
        contract:  addr,
        deployer:  deployer.address,
        aavePool:  AAVE_POOL,
        dexRouters: Object.fromEntries(names.map((n, i) => [n, routers[i]])),
        deployedAt: new Date().toISOString(),
        network:    hre.network.name,
        version:    "V2",
    };

    fs.writeFileSync("deployed_flash_v2.json", JSON.stringify(result, null, 2));
    console.log("\n📄 Сохранено в deployed_flash_v2.json");
    console.log("\nДобавь в .env:");
    console.log(`FLASH_CONTRACT_ADDRESS=${addr}`);
}

main()
    .then(() => process.exit(0))
    .catch((error) => {
        console.error(error);
        process.exit(1);
    });
