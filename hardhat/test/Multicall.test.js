// =============================================================================
// test/Multicall.test.js — Тест multicall батчинга
// =============================================================================

const { expect } = require("chai");
const { ethers } = require("hardhat");

// Адрес Multicall3 — задеплоим локально для тестов
const MULTICALL3_BYTECODE =
    "0x608060405234801561001057600080fd5b50610ee0806100206000396000" +
    "f3fe60806040526004361061009c5760003560e01c80630f28c97d14610" +
    "0a1578063174dea7114610119578063252dba4214610139578063373d3" +
    "9211461015957806382ad56cb1461017957806386d516e81461019957" +
    "8063a8b0574e146101b95780638f4bea5d146101d9578063bce38bd7" +
    "146101f9578063c3077fa914610219578063ee82ac5e1461023957600" +
    "080fd5b005b00"; // упрощённая заглушка — в реальных тестах используй forking

describe("Multicall Integration", function () {

    let owner, tokenA, tokenB, factory1, factory2, router1, router2;

    before(async function () {
        [owner] = await ethers.getSigners();

        const ERC20 = await ethers.getContractFactory("MockERC20");
        tokenA = await ERC20.deploy("Token A", "TKA", 18);
        tokenB = await ERC20.deploy("Token B", "TKB", 18);

        const Router = await ethers.getContractFactory("MockDEXRouter");
        router1 = await Router.deploy("DEX1", 25);
        router2 = await Router.deploy("DEX2", 10);

        // Разные курсы для создания спреда
        await router1.setRate(
            await tokenA.getAddress(),
            await tokenB.getAddress(),
            ethers.parseEther("100")   // 1 TKA = 100 TKB
        );
        await router2.setRate(
            await tokenA.getAddress(),
            await tokenB.getAddress(),
            ethers.parseEther("101")   // 1 TKA = 101 TKB (спред 1%)
        );
    });

    // ── 1. MockDEXRouter корректно возвращает курсы ───────────────────────────
    describe("MockDEXRouter rates", function () {

        it("getAmountsOut возвращает правильный выход", async function () {
            const path = [await tokenA.getAddress(), await tokenB.getAddress()];
            const amountIn = ethers.parseEther("1");

            const out1 = await router1.getAmountsOut(amountIn, path);
            const out2 = await router2.getAmountsOut(amountIn, path);

            // DEX1: 100 TKB × (1 - 0.25%) ≈ 99.75 TKB
            expect(out1[1]).to.be.closeTo(
                ethers.parseEther("99.75"),
                ethers.parseEther("0.1")
            );

            // DEX2: 101 TKB × (1 - 0.10%) ≈ 100.899 TKB
            expect(out2[1]).to.be.closeTo(
                ethers.parseEther("100.9"),
                ethers.parseEther("0.1")
            );

            // DEX2 даёт больше токенов — туда продаём
            expect(out2[1]).to.be.gt(out1[1]);

            console.log(`    DEX1 out: ${ethers.formatEther(out1[1])} TKB`);
            console.log(`    DEX2 out: ${ethers.formatEther(out2[1])} TKB`);
            console.log(`    Спред: ${((Number(out2[1]) - Number(out1[1])) / Number(out1[1]) * 100).toFixed(3)}%`);
        });

        it("setRate обновляет курс", async function () {
            const Router = await ethers.getContractFactory("MockDEXRouter");
            const dex = await Router.deploy("TestDex", 25);

            const path = [await tokenA.getAddress(), await tokenB.getAddress()];
            await dex.setRate(
                await tokenA.getAddress(),
                await tokenB.getAddress(),
                ethers.parseEther("200")
            );

            const out = await dex.getAmountsOut(ethers.parseEther("1"), path);
            expect(out[1]).to.be.closeTo(ethers.parseEther("199.5"), ethers.parseEther("0.1"));
        });

        it("swap revert если курс не задан", async function () {
            const Router = await ethers.getContractFactory("MockDEXRouter");
            const emptyDex = await Router.deploy("Empty", 25);
            const path = [await tokenA.getAddress(), await tokenB.getAddress()];

            await expect(
                emptyDex.getAmountsOut(ethers.parseEther("1"), path)
            ).to.be.revertedWith("MockDEX: no rate set");
        });

        it("обратный курс вычисляется автоматически", async function () {
            const pathReverse = [await tokenB.getAddress(), await tokenA.getAddress()];
            const amountIn = ethers.parseEther("100");

            // Обратный курс: 100 TKB → ≈ 1 TKA
            const out = await router1.getAmountsOut(amountIn, pathReverse);
            expect(out[1]).to.be.closeTo(
                ethers.parseEther("0.9975"),  // с учётом 0.25% fee
                ethers.parseEther("0.01")
            );
        });
    });

    // ── 2. Арбитраж через контракт на mock DEX ────────────────────────────────
    describe("MevArbitrage с mock DEX", function () {

        let mev;

        before(async function () {
            const MevFactory = await ethers.getContractFactory("MevArbitrage");
            mev = await MevFactory.deploy(
                [await router1.getAddress(), await router2.getAddress()],
                ["DEX1", "DEX2"]
            );
        });

        it("арбитраж TKA→TKB→TKA прибылен при спреде 1%", async function () {
            const mevAddr = await mev.getAddress();
            const amount  = ethers.parseEther("10");

            // Фандируем контракт
            await tokenA.mint(mevAddr, amount);
            const balBefore = await tokenA.balanceOf(mevAddr);

            // DEX2 даёт 101 TKB/TKA (дороже) → продаём TKA здесь
            // DEX1 даёт 100 TKB/TKA (дешевле) → выкупаем TKA здесь
            await mev.connect(owner).executeArbitrage(
                await tokenA.getAddress(),
                await tokenB.getAddress(),
                amount,
                "DEX2",    // buy: продаём TKA дорого (101) — больше TKB
                "DEX1",    // sell: выкупаем TKA дёшево (100/TKA = 1/100 TKA/TKB)
                0n,
            );

            const balAfter = await tokenA.balanceOf(mevAddr);
            const profit   = balAfter - balBefore;

            expect(profit).to.be.gt(0n);
            console.log(`    💰 Прибыль: ${ethers.formatEther(profit)} TKA (от ${ethers.formatEther(amount)} TKA вклада)`);
            console.log(`    📊 ROI: ${(Number(profit) / Number(amount) * 100).toFixed(3)}%`);
        });

        it("simulateArbitrage корректно предсказывает прибыль", async function () {
            const [expectedOut, expectedProfit, isProfitable] =
                await mev.simulateArbitrage(
                    await tokenA.getAddress(),
                    await tokenB.getAddress(),
                    ethers.parseEther("10"),
                    "DEX2",
                    "DEX1",
                );

            expect(isProfitable).to.be.true;

            // Реальная сделка должна давать результат близкий к симуляции
            const mevAddr = await mev.getAddress();
            await tokenA.mint(mevAddr, ethers.parseEther("10"));
            const balBefore = await tokenA.balanceOf(mevAddr);

            await mev.connect(owner).executeArbitrage(
                await tokenA.getAddress(),
                await tokenB.getAddress(),
                ethers.parseEther("10"),
                "DEX2",
                "DEX1",
                0n,
            );

            const balAfter    = await tokenA.balanceOf(mevAddr);
            const realProfit  = balAfter - balBefore;
            const simProfit   = expectedProfit;

            // Реальная прибыль должна совпадать с симуляцией (±5%)
            const diff = Number(realProfit > simProfit
                ? realProfit - simProfit
                : simProfit - realProfit);
            const tolerance = Number(simProfit) * 0.05;

            expect(diff).to.be.lte(tolerance);

            console.log(`    📊 Симуляция: ${ethers.formatEther(simProfit)} TKA`);
            console.log(`    💰 Реально:   ${ethers.formatEther(realProfit)} TKA`);
        });

        it("арбитраж невыгоден при одинаковых курсах", async function () {
            const Router = await ethers.getContractFactory("MockDEXRouter");
            const sameDex = await Router.deploy("SameDex", 25);
            await sameDex.setRate(
                await tokenA.getAddress(),
                await tokenB.getAddress(),
                ethers.parseEther("100")
            );
            await mev.addDex("SameDex", await sameDex.getAddress());

            const mevAddr = await mev.getAddress();
            await tokenA.mint(mevAddr, ethers.parseEther("10"));

            // Требуем хоть какую-то прибыль — её нет → revert
            await expect(
                mev.connect(owner).executeArbitrage(
                    await tokenA.getAddress(),
                    await tokenB.getAddress(),
                    ethers.parseEther("10"),
                    "DEX1",
                    "SameDex",
                    ethers.parseEther("0.001"),
                )
            ).to.be.revertedWithCustomError(mev, "InsufficientProfit");
        });

        it("масштабируемость: прибыль растёт с размером сделки", async function () {
            const mevAddr  = await mev.getAddress();
            const amounts  = ["1", "5", "10"].map(ethers.parseEther);
            const profits  = [];

            for (const amount of amounts) {
                await tokenA.mint(mevAddr, amount);
                const before = await tokenA.balanceOf(mevAddr);

                await mev.connect(owner).executeArbitrage(
                    await tokenA.getAddress(),
                    await tokenB.getAddress(),
                    amount,
                    "DEX2",
                    "DEX1",
                    0n,
                );

                const after = await tokenA.balanceOf(mevAddr);
                profits.push(after - before);
            }

            // Бо́льший вклад = бо́льший абсолютный профит
            expect(profits[1]).to.be.gt(profits[0]);
            expect(profits[2]).to.be.gt(profits[1]);

            console.log("    Масштабирование прибыли:");
            ["1", "5", "10"].forEach((a, i) => {
                const roi = (Number(profits[i]) / Number(amounts[i]) * 100).toFixed(3);
                console.log(`      ${a} TKA вклад → ${ethers.formatEther(profits[i])} TKA прибыль (${roi}% ROI)`);
            });
        });
    });
});
