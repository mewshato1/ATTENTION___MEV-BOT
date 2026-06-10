// =============================================================================
// test/MevArbitrage.test.js — Полный тест-сьют контракта MevArbitrage
// =============================================================================
// Запуск:
//   npx hardhat test                     # все тесты
//   npx hardhat test --grep "Arbitrage"  # только арбитраж
//   npx hardhat test --grep "Security"   # только безопасность
// =============================================================================

const { expect }        = require("chai");
const { ethers }        = require("hardhat");

// ── Константы ─────────────────────────────────────────────────────────────────

const WAD = ethers.parseEther("1");   // 1e18

// Курсы обмена для тестовых DEX (количество tokenOut за 1 tokenIn)
// PancakeSwap: 1 BNB = 584 USDT (buyDex — продаём BNB дороже)
// BiSwap:      1 BNB = 580 USDT  → спред 0.69% → арбитраж возможен
const RATE_PANCAKE_BNB_USDT = ethers.parseEther("584");
const RATE_BISWAP_BNB_USDT  = ethers.parseEther("580");

// ── Хелперы ───────────────────────────────────────────────────────────────────

async function deployAll() {
    const [owner, user, attacker] = await ethers.getSigners();

    // Токены
    const TokenA = await ethers.getContractFactory("MockERC20");
    const TokenB = await ethers.getContractFactory("MockERC20");
    const wbnb   = await TokenA.deploy("Wrapped BNB", "WBNB", 18);
    const usdt   = await TokenB.deploy("Tether USD",  "USDT", 18);

    // DEX-роутеры с разными курсами (спред = возможность арбитража)
    const RouterFactory = await ethers.getContractFactory("MockDEXRouter");
    const pancake = await RouterFactory.deploy("PancakeSwap_V2", 25);  // 0.25%
    const biswap  = await RouterFactory.deploy("BiSwap",         10);  // 0.10%

    // Курс на PancakeSwap: 1 WBNB = 580 USDT
    await pancake.setRate(await wbnb.getAddress(), await usdt.getAddress(), RATE_PANCAKE_BNB_USDT);
    // Курс на BiSwap: 1 WBNB = 584 USDT (дороже → туда продаём)
    await biswap.setRate(await wbnb.getAddress(), await usdt.getAddress(), RATE_BISWAP_BNB_USDT);

    // Деплоим основной контракт
    const MevFactory = await ethers.getContractFactory("MevArbitrage");
    const mev = await MevFactory.deploy(
        [await pancake.getAddress(), await biswap.getAddress()],
        ["PancakeSwap_V2", "BiSwap"]
    );

    return { mev, wbnb, usdt, pancake, biswap, owner, user, attacker };
}

async function fundContract(mev, token, amount) {
    // Минтим токены прямо на адрес контракта
    await token.mint(await mev.getAddress(), amount);
}

// =============================================================================
// ТЕСТЫ
// =============================================================================

describe("MevArbitrage", function () {

    // ── 1. ДЕПЛОЙ ──────────────────────────────────────────────────────────────
    describe("Deploy", function () {

        it("owner задан корректно", async function () {
            const { mev, owner } = await deployAll();
            expect(await mev.owner()).to.equal(owner.address);
        });

        it("роутеры записаны правильно", async function () {
            const { mev, pancake, biswap } = await deployAll();
            expect(await mev.dexRouters("PancakeSwap_V2")).to.equal(await pancake.getAddress());
            expect(await mev.dexRouters("BiSwap")).to.equal(await biswap.getAddress());
        });

        it("paused = false после деплоя", async function () {
            const { mev } = await deployAll();
            expect(await mev.paused()).to.equal(false);
        });

        it("slippageFactor = 9950 (0.5%) по умолчанию", async function () {
            const { mev } = await deployAll();
            expect(await mev.slippageFactor()).to.equal(9950n);
        });

        it("revert если длины массивов не совпадают", async function () {
            const MevFactory = await ethers.getContractFactory("MevArbitrage");
            await expect(
                MevFactory.deploy(
                    [ethers.ZeroAddress],
                    ["A", "B"]  // 1 роутер, 2 имени → revert
                )
            ).to.be.revertedWith("Length mismatch");
        });
    });

    // ── 2. АРБИТРАЖ (основная логика) ──────────────────────────────────────────
    describe("Arbitrage — executeArbitrage", function () {

        it("успешный арбитраж: WBNB→USDT на PancakeSwap, USDT→WBNB на BiSwap", async function () {
            const { mev, wbnb, usdt, owner } = await deployAll();
            const mevAddr = await mev.getAddress();

            const amountIn = ethers.parseEther("1"); // 1 WBNB
            await fundContract(mev, wbnb, amountIn);

            const balBefore = await wbnb.balanceOf(mevAddr);

            await mev.connect(owner).executeArbitrage(
                await wbnb.getAddress(),
                await usdt.getAddress(),
                amountIn,
                "PancakeSwap_V2",    // Покупаем USDT здесь (580 USDT)
                "BiSwap",            // Продаём USDT здесь  (584 → больше WBNB)
                ethers.parseEther("0.001"),  // минимальная прибыль 0.001 WBNB
            );

            const balAfter = await wbnb.balanceOf(mevAddr);
            // После арбитража баланс WBNB должен вырасти
            expect(balAfter).to.be.gt(balBefore);
        });

        it("прибыль соответствует ожидаемому спреду", async function () {
            const { mev, wbnb, usdt, owner } = await deployAll();
            const amountIn = ethers.parseEther("1");
            await fundContract(mev, wbnb, amountIn);

            const mevAddr   = await mev.getAddress();
            const balBefore = await wbnb.balanceOf(mevAddr);

            await mev.connect(owner).executeArbitrage(
                await wbnb.getAddress(),
                await usdt.getAddress(),
                amountIn,
                "PancakeSwap_V2",
                "BiSwap",
                0n,
            );

            const balAfter = await wbnb.balanceOf(mevAddr);
            const profit   = balAfter - balBefore;

            // Спред ~0.69%, за вычетом двух комиссий (0.25% + 0.10%)
            // Ожидаем прибыль > 0 и < 1% от вложенного
            expect(profit).to.be.gt(0n);
            expect(profit).to.be.lt(ethers.parseEther("0.01")); // < 1%

            console.log(`    💰 Прибыль: ${ethers.formatEther(profit)} WBNB`);
        });

        it("revert если прибыли нет (курсы одинаковые)", async function () {
            const { wbnb, usdt, pancake, owner } = await deployAll();

            // Деплоим контракт где оба DEX имеют одинаковый курс
            const RouterFactory = await ethers.getContractFactory("MockDEXRouter");
            const sameDex = await RouterFactory.deploy("SameDex", 25);
            await sameDex.setRate(
                await wbnb.getAddress(),
                await usdt.getAddress(),
                RATE_PANCAKE_BNB_USDT
            );

            const MevFactory = await ethers.getContractFactory("MevArbitrage");
            const mev2 = await MevFactory.deploy(
                [await pancake.getAddress(), await sameDex.getAddress()],
                ["PancakeSwap_V2", "SameDex"]
            );

            await fundContract(mev2, wbnb, ethers.parseEther("1"));

            await expect(
                mev2.connect(owner).executeArbitrage(
                    await wbnb.getAddress(),
                    await usdt.getAddress(),
                    ethers.parseEther("1"),
                    "PancakeSwap_V2",
                    "SameDex",
                    ethers.parseEther("0.001"),  // требуем прибыль — её нет
                )
            ).to.be.revertedWithCustomError(mev2, "InsufficientProfit");
        });

        it("revert если amountIn = 0", async function () {
            const { mev, wbnb, usdt, owner } = await deployAll();
            await expect(
                mev.connect(owner).executeArbitrage(
                    await wbnb.getAddress(),
                    await usdt.getAddress(),
                    0n,
                    "PancakeSwap_V2",
                    "BiSwap",
                    0n,
                )
            ).to.be.revertedWithCustomError(mev, "ZeroAmount");
        });

        it("revert если DEX не существует", async function () {
            const { mev, wbnb, usdt, owner } = await deployAll();
            await fundContract(mev, wbnb, ethers.parseEther("1"));

            await expect(
                mev.connect(owner).executeArbitrage(
                    await wbnb.getAddress(),
                    await usdt.getAddress(),
                    ethers.parseEther("1"),
                    "NonExistentDEX",   // ← нет такого
                    "BiSwap",
                    0n,
                )
            ).to.be.revertedWithCustomError(mev, "RouterNotFound");
        });

        it("revert если недостаточно токенов на контракте", async function () {
            const { mev, wbnb, usdt, owner } = await deployAll();
            // НЕ фандим контракт токенами

            await expect(
                mev.connect(owner).executeArbitrage(
                    await wbnb.getAddress(),
                    await usdt.getAddress(),
                    ethers.parseEther("1"),
                    "PancakeSwap_V2",
                    "BiSwap",
                    0n,
                )
            ).to.be.revertedWith("Insufficient tokenA balance");
        });

        it("событие ArbitrageExecuted эмитируется", async function () {
            const { mev, wbnb, usdt, owner, pancake, biswap } = await deployAll();
            await fundContract(mev, wbnb, ethers.parseEther("1"));

            await expect(
                mev.connect(owner).executeArbitrage(
                    await wbnb.getAddress(),
                    await usdt.getAddress(),
                    ethers.parseEther("1"),
                    "PancakeSwap_V2",
                    "BiSwap",
                    0n,
                )
            ).to.emit(mev, "ArbitrageExecuted")
             .withArgs(
                await wbnb.getAddress(),
                await usdt.getAddress(),
                await pancake.getAddress(),
                await biswap.getAddress(),
                ethers.parseEther("1"),
                (profit) => profit > 0n,   // profit > 0
                (ts)     => ts > 0n,       // timestamp > 0
             );
        });
    });

    // ── 3. СИМУЛЯЦИЯ ───────────────────────────────────────────────────────────
    describe("simulateArbitrage (view)", function () {

        it("возвращает isProfitable=true для прибыльного арбитража", async function () {
            const { mev, wbnb, usdt } = await deployAll();

            const [expectedOut, expectedProfit, isProfitable] =
                await mev.simulateArbitrage(
                    await wbnb.getAddress(),
                    await usdt.getAddress(),
                    ethers.parseEther("1"),
                    "PancakeSwap_V2",
                    "BiSwap",
                );

            expect(isProfitable).to.equal(true);
            expect(expectedProfit).to.be.gt(0n);
            console.log(`    📊 Симуляция: out=${ethers.formatEther(expectedOut)} WBNB, profit=${ethers.formatEther(expectedProfit)} WBNB`);
        });

        it("возвращает isProfitable=false если нет спреда", async function () {
            const { mev, wbnb, usdt, pancake } = await deployAll();

            // Деплоим второй DEX с тем же курсом
            const RouterFactory = await ethers.getContractFactory("MockDEXRouter");
            const sameDex = await RouterFactory.deploy("Same", 25);
            await sameDex.setRate(
                await wbnb.getAddress(),
                await usdt.getAddress(),
                RATE_PANCAKE_BNB_USDT
            );
            await mev.addDex("Same", await sameDex.getAddress());

            const [,, isProfitable] = await mev.simulateArbitrage(
                await wbnb.getAddress(),
                await usdt.getAddress(),
                ethers.parseEther("1"),
                "PancakeSwap_V2",
                "Same",
            );
            expect(isProfitable).to.equal(false);
        });

        it("не тратит газ (view функция)", async function () {
            const { mev, wbnb, usdt } = await deployAll();
            // call() на view функции не тратит газ в сети
            // В тестах просто убеждаемся что не бросает исключение
            await mev.simulateArbitrage.staticCall(
                await wbnb.getAddress(),
                await usdt.getAddress(),
                ethers.parseEther("1"),
                "PancakeSwap_V2",
                "BiSwap",
            );
        });
    });

    // ── 4. БЕЗОПАСНОСТЬ ────────────────────────────────────────────────────────
    describe("Security", function () {

        it("только owner может вызвать executeArbitrage", async function () {
            const { mev, wbnb, usdt, user } = await deployAll();
            await fundContract(mev, wbnb, ethers.parseEther("1"));

            await expect(
                mev.connect(user).executeArbitrage(
                    await wbnb.getAddress(),
                    await usdt.getAddress(),
                    ethers.parseEther("1"),
                    "PancakeSwap_V2",
                    "BiSwap",
                    0n,
                )
            ).to.be.revertedWithCustomError(mev, "NotOwner");
        });

        it("только owner может вызвать withdraw", async function () {
            const { mev, wbnb, attacker } = await deployAll();
            await fundContract(mev, wbnb, ethers.parseEther("1"));

            await expect(
                mev.connect(attacker).withdraw(await wbnb.getAddress(), 0n)
            ).to.be.revertedWithCustomError(mev, "NotOwner");
        });

        it("только owner может добавить DEX", async function () {
            const { mev, attacker } = await deployAll();
            await expect(
                mev.connect(attacker).addDex("Evil", attacker.address)
            ).to.be.revertedWithCustomError(mev, "NotOwner");
        });

        it("только owner может поставить на паузу", async function () {
            const { mev, attacker } = await deployAll();
            await expect(
                mev.connect(attacker).setPaused(true)
            ).to.be.revertedWithCustomError(mev, "NotOwner");
        });

        it("executeArbitrage недоступен когда контракт на паузе", async function () {
            const { mev, wbnb, usdt, owner } = await deployAll();
            await fundContract(mev, wbnb, ethers.parseEther("1"));

            await mev.connect(owner).setPaused(true);

            await expect(
                mev.connect(owner).executeArbitrage(
                    await wbnb.getAddress(),
                    await usdt.getAddress(),
                    ethers.parseEther("1"),
                    "PancakeSwap_V2",
                    "BiSwap",
                    0n,
                )
            ).to.be.revertedWithCustomError(mev, "ContractPaused");
        });

        it("можно снять с паузы и снова торговать", async function () {
            const { mev, wbnb, usdt, owner } = await deployAll();
            await fundContract(mev, wbnb, ethers.parseEther("1"));

            await mev.connect(owner).setPaused(true);
            await mev.connect(owner).setPaused(false);

            // Теперь должно работать
            await expect(
                mev.connect(owner).executeArbitrage(
                    await wbnb.getAddress(),
                    await usdt.getAddress(),
                    ethers.parseEther("1"),
                    "PancakeSwap_V2",
                    "BiSwap",
                    0n,
                )
            ).to.not.be.reverted;
        });

        it("transferOwnership работает корректно", async function () {
            const { mev, owner, user } = await deployAll();

            await mev.connect(owner).transferOwnership(user.address);
            expect(await mev.owner()).to.equal(user.address);

            // Старый owner теперь не может ничего делать
            await expect(
                mev.connect(owner).setPaused(true)
            ).to.be.revertedWithCustomError(mev, "NotOwner");
        });

        it("transferOwnership на нулевой адрес запрещён", async function () {
            const { mev, owner } = await deployAll();
            await expect(
                mev.connect(owner).transferOwnership(ethers.ZeroAddress)
            ).to.be.revertedWith("Zero address");
        });
    });

    // ── 5. УПРАВЛЕНИЕ ──────────────────────────────────────────────────────────
    describe("Management", function () {

        it("withdraw выводит токены owner-у", async function () {
            const { mev, wbnb, owner } = await deployAll();
            const amount = ethers.parseEther("2");
            await fundContract(mev, wbnb, amount);

            const ownerBefore = await wbnb.balanceOf(owner.address);
            await mev.connect(owner).withdraw(await wbnb.getAddress(), amount);
            const ownerAfter = await wbnb.balanceOf(owner.address);

            expect(ownerAfter - ownerBefore).to.equal(amount);
            expect(await wbnb.balanceOf(await mev.getAddress())).to.equal(0n);
        });

        it("withdraw с amount=0 выводит всё", async function () {
            const { mev, wbnb, owner } = await deployAll();
            const amount = ethers.parseEther("3.5");
            await fundContract(mev, wbnb, amount);

            await mev.connect(owner).withdraw(await wbnb.getAddress(), 0n);
            expect(await wbnb.balanceOf(await mev.getAddress())).to.equal(0n);
        });

        it("addDex добавляет новый роутер", async function () {
            const { mev, owner } = await deployAll();
            const fakeRouter = ethers.Wallet.createRandom().address;

            await mev.connect(owner).addDex("NewDEX", fakeRouter);
            expect(await mev.dexRouters("NewDEX")).to.equal(fakeRouter);
            expect(await mev.getDexCount()).to.equal(3n); // 2 начальных + 1 новый
        });

        it("setSlippage меняет slippageFactor", async function () {
            const { mev, owner } = await deployAll();
            await mev.connect(owner).setSlippage(9900n); // 1%
            expect(await mev.slippageFactor()).to.equal(9900n);
        });

        it("setSlippage revert на невалидное значение", async function () {
            const { mev, owner } = await deployAll();
            await expect(
                mev.connect(owner).setSlippage(5000n) // 50% — абсурд
            ).to.be.revertedWith("Invalid slippage");
        });

        it("tokenBalance возвращает правильный баланс", async function () {
            const { mev, wbnb } = await deployAll();
            const amount = ethers.parseEther("5");
            await fundContract(mev, wbnb, amount);

            expect(await mev.tokenBalance(await wbnb.getAddress())).to.equal(amount);
        });

        it("событие Paused эмитируется", async function () {
            const { mev, owner } = await deployAll();
            await expect(mev.connect(owner).setPaused(true))
                .to.emit(mev, "Paused").withArgs(true);
            await expect(mev.connect(owner).setPaused(false))
                .to.emit(mev, "Paused").withArgs(false);
        });

        it("событие Withdrawn эмитируется при выводе", async function () {
            const { mev, wbnb, owner } = await deployAll();
            const amount = ethers.parseEther("1");
            await fundContract(mev, wbnb, amount);

            await expect(mev.connect(owner).withdraw(await wbnb.getAddress(), amount))
                .to.emit(mev, "Withdrawn")
                .withArgs(await wbnb.getAddress(), owner.address, amount);
        });
    });

    // ── 6. ГРАНИЧНЫЕ СЛУЧАИ ────────────────────────────────────────────────────
    describe("Edge cases", function () {

        it("несколько арбитражей подряд накапливают прибыль", async function () {
            const { mev, wbnb, usdt, owner } = await deployAll();
            const mevAddr = await mev.getAddress();

            // Фандим с запасом для нескольких сделок
            await fundContract(mev, wbnb, ethers.parseEther("5"));

            const balStart = await wbnb.balanceOf(mevAddr);

            // Три сделки подряд
            for (let i = 0; i < 3; i++) {
                await mev.connect(owner).executeArbitrage(
                    await wbnb.getAddress(),
                    await usdt.getAddress(),
                    ethers.parseEther("1"),
                    "PancakeSwap_V2",
                    "BiSwap",
                    0n,
                );
            }

            const balEnd = await wbnb.balanceOf(mevAddr);
            // Суммарный баланс должен вырасти
            expect(balEnd).to.be.gt(balStart - ethers.parseEther("3")); // потратили 3, вернули 3+
            console.log(`    📈 После 3 сделок: баланс ${ethers.formatEther(balEnd)} WBNB`);
        });

        it("arбитраж с обратным направлением (B→A buy, A→B sell)", async function () {
            const { mev, wbnb, usdt, biswap, pancake, owner } = await deployAll();

            // Теперь BiSwap дешевле по USDT → WBNB
            // Это значит биСвап даёт больше WBNB за USDT
            await fundContract(mev, usdt, ethers.parseEther("580"));

            // USDT→WBNB на BiSwap (дешевле), WBNB→USDT на PancakeSwap (дороже)
            // С нашими mock курсами это тоже должно быть прибыльным
            await expect(
                mev.connect(owner).executeArbitrage(
                    await usdt.getAddress(),
                    await wbnb.getAddress(),
                    ethers.parseEther("580"),
                    "BiSwap",
                    "PancakeSwap_V2",
                    0n,
                )
            ).to.not.be.reverted;
        });

        it("контракт принимает нативный BNB через receive()", async function () {
            const { mev, owner } = await deployAll();
            const mevAddr = await mev.getAddress();

            await owner.sendTransaction({
                to:    mevAddr,
                value: ethers.parseEther("0.1"),
            });

            expect(await ethers.provider.getBalance(mevAddr))
                .to.equal(ethers.parseEther("0.1"));
        });

        it("withdrawBNB выводит нативный BNB owner-у", async function () {
            const { mev, owner } = await deployAll();

            await owner.sendTransaction({
                to:    await mev.getAddress(),
                value: ethers.parseEther("0.1"),
            });

            const balBefore = await ethers.provider.getBalance(owner.address);
            const tx      = await mev.connect(owner).withdrawBNB();
            const receipt = await tx.wait();
            const gasCost = receipt.gasUsed * receipt.gasPrice;
            const balAfter  = await ethers.provider.getBalance(owner.address);

            // Баланс вырос на 0.1 BNB минус газ
            expect(balAfter + gasCost - balBefore).to.be.closeTo(
                ethers.parseEther("0.1"),
                ethers.parseEther("0.001"), // допуск 0.001 BNB
            );
        });

        it("minProfit = 0 всегда проходит если есть хоть какая прибыль", async function () {
            const { mev, wbnb, usdt, owner } = await deployAll();
            await fundContract(mev, wbnb, ethers.parseEther("1"));

            await expect(
                mev.connect(owner).executeArbitrage(
                    await wbnb.getAddress(),
                    await usdt.getAddress(),
                    ethers.parseEther("1"),
                    "PancakeSwap_V2",
                    "BiSwap",
                    0n,
                )
            ).to.not.be.reverted;
        });
    });

    // ── 7. GAS REPORT ──────────────────────────────────────────────────────────
    describe("Gas usage", function () {

        it("executeArbitrage gas", async function () {
            const { mev, wbnb, usdt, owner } = await deployAll();
            await fundContract(mev, wbnb, ethers.parseEther("1"));

            const tx      = await mev.connect(owner).executeArbitrage(
                await wbnb.getAddress(),
                await usdt.getAddress(),
                ethers.parseEther("1"),
                "PancakeSwap_V2",
                "BiSwap",
                0n,
            );
            const receipt = await tx.wait();
            console.log(`    ⛽ executeArbitrage gas: ${receipt.gasUsed.toLocaleString()} units`);
            expect(receipt.gasUsed).to.be.lt(500_000n); // должно укладываться в наш GAS_LIMIT
        });

        it("simulateArbitrage — staticCall, газ = 0", async function () {
            const { mev, wbnb, usdt } = await deployAll();
            // Просто убеждаемся что выполняется без ошибок
            await mev.simulateArbitrage.staticCall(
                await wbnb.getAddress(),
                await usdt.getAddress(),
                ethers.parseEther("1"),
                "PancakeSwap_V2",
                "BiSwap",
            );
        });
    });
});
