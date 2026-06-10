// =============================================================================
// deployMultiChain.js — Деплой FlashArbitrageV2 на любой поддерживаемый чейн
// =============================================================================
// Запуск:
//   npx hardhat run scripts/deployMultiChain.js --network polygon
//   npx hardhat run scripts/deployMultiChain.js --network arbitrum
//   npx hardhat run scripts/deployMultiChain.js --network avalanche
//   npx hardhat run scripts/deployMultiChain.js --network bsc_mainnet
// =============================================================================

const hre = require("hardhat");
const fs  = require("fs");
const path = require("path");
const CHAINS = require("../config/chains");

async function main() {
    const networkName = hre.network.name;
    const chainCfg    = CHAINS[networkName];

    if (!chainCfg) {
        console.error(`\nНет конфига для сети "${networkName}"`);
        console.error(`Доступные сети: ${Object.keys(CHAINS).join(", ")}`);
        process.exit(1);
    }

    const [deployer] = await hre.ethers.getSigners();
    const balance    = await hre.ethers.provider.getBalance(deployer.address);

    console.log("=".repeat(60));
    console.log(`Чейн:    ${networkName} (chainId ${chainCfg.chainId})`);
    console.log(`Деплоер: ${deployer.address}`);
    console.log(`Баланс:  ${hre.ethers.formatEther(balance)} ${chainCfg.nativeToken}`);
    console.log("=".repeat(60));

    // ── Деплой контракта ───────────────────────────────────────────────────
    const routerAddrs = chainCfg.routers.map(r => r.address);
    const routerNames = chainCfg.routers.map(r => r.name);

    console.log(`\nДеплоим FlashArbitrageV2...`);
    console.log(`  AAVE Pool: ${chainCfg.aavePool}`);
    console.log(`  DEX-ы (${routerNames.length}): ${routerNames.join(", ")}`);

    const Factory  = await hre.ethers.getContractFactory("FlashArbitrageV2");
    const contract = await Factory.deploy(chainCfg.aavePool, routerAddrs, routerNames);
    await contract.waitForDeployment();

    const addr = await contract.getAddress();
    console.log(`\nFlashArbitrageV2 задеплоен: ${addr}`);

    // ── approveMax для всех токен/роутер комбинаций ───────────────────────
    const tokenAddrs = Object.values(chainCfg.tokens);
    const tokenNames = Object.keys(chainCfg.tokens);

    console.log(`\nApprove: ${tokenNames.length} токенов × ${routerAddrs.length} роутеров...`);
    const txApprove = await contract.approveMaxBatch(tokenAddrs, routerAddrs);
    const rcptApprove = await txApprove.wait();
    console.log(`  approveMaxBatch gas: ${rcptApprove.gasUsed.toLocaleString()}`);

    // Approve для AAVE Pool (возврат долга)
    console.log(`\nApprove токенов для AAVE Pool...`);
    for (let i = 0; i < tokenAddrs.length; i++) {
        const tx = await contract.approveMax(tokenAddrs[i], chainCfg.aavePool);
        await tx.wait();
        process.stdout.write(`  [${i + 1}/${tokenAddrs.length}] ${tokenNames[i]}\r`);
    }
    console.log(`\n  Готово`);

    // ── Сохраняем результат ────────────────────────────────────────────────
    const outFile = `deployed_${networkName}.json`;
    const result = {
        network:    networkName,
        chainId:    chainCfg.chainId,
        contract:   addr,
        deployer:   deployer.address,
        aavePool:   chainCfg.aavePool,
        dexRouters: Object.fromEntries(routerNames.map((n, i) => [n, routerAddrs[i]])),
        tokens:     chainCfg.tokens,
        deployedAt: new Date().toISOString(),
        version:    "V2",
    };

    fs.writeFileSync(outFile, JSON.stringify(result, null, 2));

    console.log(`\nСохранено в ${outFile}`);
    console.log(`\nДобавь в .env:`);
    console.log(`${networkName.toUpperCase().replace(/_/g, "_")}_CONTRACT=${addr}`);
    console.log("=".repeat(60));
}

main()
    .then(() => process.exit(0))
    .catch(err => {
        console.error(err);
        process.exit(1);
    });
