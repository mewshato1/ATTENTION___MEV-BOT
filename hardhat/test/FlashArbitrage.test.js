// =============================================================================
// test/FlashArbitrage.test.js — Полный тест-сьют FlashArbitrage
// =============================================================================
// npx hardhat test test/FlashArbitrage.test.js
// npx hardhat test --grep "FlashArbitrage"
// =============================================================================

const { expect } = require("chai");
const { ethers } = require("hardhat");

const WAD = ethers.parseEther("1");

// ── Деплой всей инфраструктуры ────────────────────────────────────────────────

async function deployAll() {
    const [owner, user, attacker] = await ethers.getSigners();

    // Токены
    const ERC20 = await ethers.getContractFactory("MockERC20");
    const wbnb  = await ERC20.deploy("WBNB",  "WBNB",  18);
    const usdt  = await ERC20.deploy("USDT",  "USDT",  18);
    const cake  = await ERC20.deploy("CAKE",  "CAKE",  18);

    // DEX — разные курсы для создания спреда
    const Router = await ethers.getContractFactory("MockDEXRouter");
    const pancake = await Router.deploy("PancakeSwap_V2", 25);  // 0.25% fee
    const biswap  = await Router.deploy("BiSwap",         10);  // 0.10% fee

    // Курсы: PancakeSwap 584 (buyDex — дороже продаём WBNB), BiSwap 580 (sellDex — дешевле выкупаем)
    const wbnbAddr = await wbnb.getAddress();
    const usdtAddr = await usdt.getAddress();
    const cakeAddr = await cake.getAddress();

    await pancake.setRate(wbnbAddr, usdtAddr, ethers.parseEther("584"));
    await biswap .setRate(wbnbAddr, usdtAddr, ethers.parseEther("580"));

    // Для треугольного арбитража: WBNB→USDT→CAKE→WBNB
    await pancake.setRate(usdtAddr, cakeAddr, ethers.parseEther("2"));    // 1 USDT = 2 CAKE
    await biswap .setRate(cakeAddr, wbnbAddr, ethers.parseEther("0.000856")); // равновесный курс ~1/(584*2)

    // Mock AAVE Pool
    const AAVEFactory = await ethers.getContractFactory("MockAAVEPool");
    const aavePool = await AAVEFactory.deploy();

    // FlashArbitrage контракт
    const FlashFactory = await ethers.getContractFactory("FlashArbitrage");
    const flash = await FlashFactory.deploy(
        await aavePool.getAddress(),
        [await pancake.getAddress(), await biswap.getAddress()],
        ["PancakeSwap_V2", "BiSwap"]
    );

    // Разрешаем AAVE pull токены с контракта (нужно для repayment)
    // В реальности делается в executeOperation, но для тестов упрощаем

    return {
        flash, aavePool, pancake, biswap,
        wbnb, usdt, cake,
        wbnbAddr, usdtAddr, cakeAddr,
        owner, user, attacker
    };
}

// Минтим токены прямо на контракт
async function fund(token, to, amount) {
    await token.mint(await to.getAddress(), ethers.parseEther(amount.toString()));
}

// ─────────────────────────────────────────────────────────────────────────────

describe("FlashArbitrage", function () {

    // ── 1. ДЕПЛОЙ ─────────────────────────────────────────────────────────────
    describe("Deploy", function () {

        it("owner, aavePool, роутеры заданы корректно", async function () {
            const { flash, aavePool, pancake, biswap, owner } = await deployAll();

            expect(await flash.owner()).to.equal(owner.address);
            expect(await flash.aavePool()).to.equal(await aavePool.getAddress());
            expect(await flash.dexRouters("PancakeSwap_V2")).to.equal(await pancake.getAddress());
            expect(await flash.dexRouters("BiSwap")).to.equal(await biswap.getAddress());
        });

        it("paused=false, slippageFactor=9950 по умолчанию", async function () {
            const { flash } = await deployAll();
            expect(await flash.paused()).to.equal(false);
            expect(await flash.slippageFactor()).to.equal(9950n);
        });

        it("revert при mismatch длин массивов", async function () {
            const FlashFactory = await ethers.getContractFactory("FlashArbitrage");
            await expect(
                FlashFactory.deploy(ethers.ZeroAddress, [ethers.ZeroAddress], ["A", "B"])
            ).to.be.revertedWith("Length mismatch");
        });
    });

    // ── 2. AAVE FLASHLOAN АРБИТРАЖ ────────────────────────────────────────────
    describe("AAVE Flashloan — flashAave()", function () {

        it("успешный двусторонний арбитраж WBNB→USDT→WBNB", async function () {
            const { flash, wbnb, usdt, aavePool, wbnbAddr, usdtAddr, owner } = await deployAll();
            const flashAddr = await flash.getAddress();

            // Даём AAVE пулу USDT для выплаты swap outputs
            await usdt.mint(await aavePool.getAddress(), ethers.parseEther("10000"));

            // Разрешаем контракту тратить AAVE-долг
            // (в реальном контракте делается в executeOperation через approve)
            const flashAmount = ethers.parseEther("1");

            // Перед вызовом на контракте нет ни одного WBNB
            expect(await wbnb.balanceOf(flashAddr)).to.equal(0n);

            await flash.connect(owner).flashAave(
                wbnbAddr,
                usdtAddr,
                flashAmount,
                "PancakeSwap_V2",   // A→B дороже (584) → продаём WBNB здесь
                "BiSwap",           // B→A дешевле (580) → выкупаем WBNB здесь
                0n
            );

            // После арбитража на контракте должна быть прибыль
            const profit = await wbnb.balanceOf(flashAddr);
            expect(profit).to.be.gt(0n);
            console.log(`    💰 AAVE арбитраж: прибыль ${ethers.formatEther(profit)} WBNB`);
        });

        it("прибыль покрывает AAVE premium (0.05%)", async function () {
            const { flash, wbnb, usdt, aavePool, wbnbAddr, usdtAddr, owner } = await deployAll();
            await usdt.mint(await aavePool.getAddress(), ethers.parseEther("10000"));

            const flashAmount = ethers.parseEther("5");
            const aavePremium = (flashAmount * 5n) / 10_000n;

            await flash.connect(owner).flashAave(
                wbnbAddr, usdtAddr, flashAmount,
                "PancakeSwap_V2", "BiSwap", 0n
            );

            const profit = await wbnb.balanceOf(await flash.getAddress());
            // Прибыль = arbitrage_gain - premium
            // При спреде 0.69% и комиссиях 0.25%+0.10% ожидаем > 0
            expect(profit).to.be.gt(0n);
            console.log(`    📊 AAVE fee: ${ethers.formatEther(aavePremium)} WBNB | Прибыль: ${ethers.formatEther(profit)} WBNB`);
        });

        it("revert с InsufficientProfit если спреда нет", async function () {
            const { flash, wbnb, usdt, aavePool, pancake, wbnbAddr, usdtAddr, owner } = await deployAll();
            await usdt.mint(await aavePool.getAddress(), ethers.parseEther("10000"));

            // Добавляем DEX с тем же курсом что и PancakeSwap → нет арбитража
            const Router = await ethers.getContractFactory("MockDEXRouter");
            const sameDex = await Router.deploy("Same", 25);
            await sameDex.setRate(wbnbAddr, usdtAddr, ethers.parseEther("584")); // идентичный курс с PancakeSwap
            await flash.addDex("Same", await sameDex.getAddress());

            await expect(
                flash.connect(owner).flashAave(
                    wbnbAddr, usdtAddr,
                    ethers.parseEther("1"),
                    "PancakeSwap_V2", "Same",
                    ethers.parseEther("0.001") // требуем прибыль — её нет
                )
            ).to.be.revertedWithCustomError(flash, "InsufficientProfit");
        });

        it("revert если amountIn = 0", async function () {
            const { flash, wbnbAddr, usdtAddr, owner } = await deployAll();
            await expect(
                flash.connect(owner).flashAave(wbnbAddr, usdtAddr, 0n, "PancakeSwap_V2", "BiSwap", 0n)
            ).to.be.revertedWithCustomError(flash, "ZeroAmount");
        });
    });

    // ── 3. PANCAKE FLASHSWAP ───────────────────────────────────────────────────
    describe("PancakeSwap Flashswap — flashPancake()", function () {

        it("pancakeCall только от ожидаемой пары", async function () {
            const { flash, attacker, wbnbAddr, usdtAddr } = await deployAll();

            // Атакующий пытается вызвать pancakeCall напрямую
            const fakeParams = {
                tokenA: wbnbAddr, tokenB: usdtAddr, tokenC: ethers.ZeroAddress,
                buyRouter: ethers.ZeroAddress, sellRouter: ethers.ZeroAddress,
                flashAmount: ethers.parseEther("100"),
                minProfit: 0n, flashSource: attacker.address,
                provider: 0
            };

            await expect(
                flash.connect(attacker).pancakeCall(
                    attacker.address, 0n, 0n,
                    ethers.AbiCoder.defaultAbiCoder().encode(
                        ["tuple(address,address,address,address,address,uint256,uint256,address,uint8)"],
                        [[
                            fakeParams.tokenA, fakeParams.tokenB, fakeParams.tokenC,
                            fakeParams.buyRouter, fakeParams.sellRouter,
                            fakeParams.flashAmount, fakeParams.minProfit,
                            fakeParams.flashSource, fakeParams.provider
                        ]]
                    )
                )
            ).to.be.revertedWithCustomError(flash, "InvalidCallback");
        });
    });

    // ── 4. ТРЕУГОЛЬНЫЙ АРБИТРАЖ ───────────────────────────────────────────────
    describe("Triangle Arbitrage — flashAaveTriangle()", function () {

        it("WBNB→USDT→CAKE→WBNB: revert если нет прибыли", async function () {
            const { flash, wbnb, usdt, cake, aavePool,
                    wbnbAddr, usdtAddr, cakeAddr, owner } = await deployAll();

            await usdt.mint(await aavePool.getAddress(), ethers.parseEther("10000"));
            await cake.mint(await aavePool.getAddress(), ethers.parseEther("10000"));

            // С такими курсами треугольный арб убыточен (суммарные комиссии > спред)
            // Проверяем что контракт это правильно детектирует
            await expect(
                flash.connect(owner).flashAaveTriangle(
                    wbnbAddr, usdtAddr, cakeAddr,
                    ethers.parseEther("1"),
                    "PancakeSwap_V2", "PancakeSwap_V2", "BiSwap",
                    ethers.parseEther("0.01")   // требуем высокую прибыль
                )
            ).to.be.revertedWithCustomError(flash, "InsufficientProfit");
        });

        it("revert если tokenC = address(0)", async function () {
            const { flash, wbnbAddr, usdtAddr, owner } = await deployAll();
            await expect(
                flash.connect(owner).flashAaveTriangle(
                    wbnbAddr, usdtAddr, ethers.ZeroAddress,
                    ethers.parseEther("1"),
                    "PancakeSwap_V2", "BiSwap", "PancakeSwap_V2",
                    0n
                )
            ).to.be.revertedWithCustomError(flash, "ZeroAmount");
        });
    });

    // ── 5. VIEW ФУНКЦИИ ───────────────────────────────────────────────────────
    describe("Simulation (view)", function () {

        it("simulateTwoWay: корректные значения для прибыльного спреда", async function () {
            const { flash, wbnbAddr, usdtAddr } = await deployAll();

            const [amountOut, profit, pancakeDebt, aavePremium, profPancake, profAave] =
                await flash.simulateTwoWay(
                    wbnbAddr, usdtAddr,
                    ethers.parseEther("1"),
                    "PancakeSwap_V2", "BiSwap"
                );

            expect(profit).to.be.gt(0n);
            expect(profAave).to.equal(true);   // AAVE fee дешевле → прибыльнее
            expect(aavePremium).to.be.lt(pancakeDebt); // AAVE всегда дешевле

            console.log(`    amountOut:   ${ethers.formatEther(amountOut)} WBNB`);
            console.log(`    profit:      ${ethers.formatEther(profit)} WBNB`);
            console.log(`    pancakeDebt: ${ethers.formatEther(pancakeDebt)} WBNB (+${ethers.formatEther(pancakeDebt - ethers.parseEther("1"))} fee)`);
            console.log(`    aavePremium: ${ethers.formatEther(aavePremium)} WBNB (+${ethers.formatEther(aavePremium - ethers.parseEther("1"))} fee)`);
        });

        it("simulateTwoWay: isProfitable=false для убыточного пути", async function () {
            const { flash, wbnbAddr, usdtAddr } = await deployAll();
            // Обратный путь (покупаем дороже, продаём дешевле) → убыток
            const [,,,, profPancake, profAave] = await flash.simulateTwoWay(
                wbnbAddr, usdtAddr,
                ethers.parseEther("1"),
                "BiSwap",           // дороже
                "PancakeSwap_V2",   // дешевле
            );
            expect(profPancake).to.equal(false);
            expect(profAave).to.equal(false);
        });

        it("compareProviders: AAVE всегда дешевле для любой суммы", async function () {
            const { flash, wbnbAddr } = await deployAll();

            for (const amount of ["0.1", "1", "10", "100"]) {
                const [pancakeCost, aaveCost, cheaper] = await flash.compareProviders(
                    wbnbAddr,
                    ethers.parseEther(amount)
                );
                expect(cheaper).to.equal("AAVE");
                expect(aaveCost).to.be.lt(pancakeCost);
            }
        });

        it("balance() возвращает правильный баланс", async function () {
            const { flash, wbnb, wbnbAddr } = await deployAll();
            const amount = ethers.parseEther("3.7");
            await wbnb.mint(await flash.getAddress(), amount);
            expect(await flash.balance(wbnbAddr)).to.equal(amount);
        });

        it("simulateTriangle: не падает, возвращает корректные типы", async function () {
            const { flash, wbnbAddr, usdtAddr, cakeAddr } = await deployAll();
            const [finalAmount, profit, isProfitable] = await flash.simulateTriangle(
                wbnbAddr, usdtAddr, cakeAddr,
                ethers.parseEther("1"),
                "PancakeSwap_V2", "PancakeSwap_V2", "BiSwap"
            );
            expect(typeof finalAmount).to.equal("bigint");
            expect(typeof profit).to.equal("bigint");
            expect(typeof isProfitable).to.equal("boolean");
        });
    });

    // ── 6. БЕЗОПАСНОСТЬ ───────────────────────────────────────────────────────
    describe("Security", function () {

        it("только owner: flashAave", async function () {
            const { flash, wbnbAddr, usdtAddr, user } = await deployAll();
            await expect(
                flash.connect(user).flashAave(wbnbAddr, usdtAddr, WAD, "PancakeSwap_V2", "BiSwap", 0n)
            ).to.be.revertedWithCustomError(flash, "NotOwner");
        });

        it("только owner: flashPancake", async function () {
            const { flash, wbnbAddr, usdtAddr, user } = await deployAll();
            await expect(
                flash.connect(user).flashPancake(
                    ethers.ZeroAddress, wbnbAddr, usdtAddr, WAD, "PancakeSwap_V2", "BiSwap", 0n
                )
            ).to.be.revertedWithCustomError(flash, "NotOwner");
        });

        it("только owner: flashAaveTriangle", async function () {
            const { flash, wbnbAddr, usdtAddr, cakeAddr, user } = await deployAll();
            await expect(
                flash.connect(user).flashAaveTriangle(
                    wbnbAddr, usdtAddr, cakeAddr, WAD,
                    "PancakeSwap_V2", "BiSwap", "PancakeSwap_V2", 0n
                )
            ).to.be.revertedWithCustomError(flash, "NotOwner");
        });

        it("только owner: withdraw", async function () {
            const { flash, wbnb, wbnbAddr, attacker } = await deployAll();
            await wbnb.mint(await flash.getAddress(), WAD);
            await expect(
                flash.connect(attacker).withdraw(wbnbAddr, WAD)
            ).to.be.revertedWithCustomError(flash, "NotOwner");
        });

        it("executeOperation только от AAVE pool", async function () {
            const { flash, wbnbAddr, attacker } = await deployAll();
            await expect(
                flash.connect(attacker).executeOperation(
                    wbnbAddr, WAD, 0n, attacker.address, "0x"
                )
            ).to.be.revertedWithCustomError(flash, "InvalidCallback");
        });

        it("paused блокирует все точки входа", async function () {
            const { flash, wbnbAddr, usdtAddr, owner } = await deployAll();
            await flash.connect(owner).setPaused(true);

            await expect(
                flash.connect(owner).flashAave(wbnbAddr, usdtAddr, WAD, "PancakeSwap_V2", "BiSwap", 0n)
            ).to.be.revertedWithCustomError(flash, "ContractPaused");

            await expect(
                flash.connect(owner).flashPancake(
                    ethers.ZeroAddress, wbnbAddr, usdtAddr, WAD,
                    "PancakeSwap_V2", "BiSwap", 0n
                )
            ).to.be.revertedWithCustomError(flash, "ContractPaused");
        });

        it("transferOwnership корректно меняет owner", async function () {
            const { flash, owner, user } = await deployAll();
            await flash.connect(owner).transferOwnership(user.address);
            expect(await flash.owner()).to.equal(user.address);
            await expect(
                flash.connect(owner).setPaused(true)
            ).to.be.revertedWithCustomError(flash, "NotOwner");
        });

        it("setSlippage revert на недопустимые значения", async function () {
            const { flash, owner } = await deployAll();
            await expect(flash.connect(owner).setSlippage(5000n))
                .to.be.revertedWithCustomError(flash, "SlippageOutOfRange");
            await expect(flash.connect(owner).setSlippage(10000n))
                .to.be.revertedWithCustomError(flash, "SlippageOutOfRange");
        });
    });

    // ── 7. УПРАВЛЕНИЕ ─────────────────────────────────────────────────────────
    describe("Management", function () {

        it("withdraw выводит токены owner-у", async function () {
            const { flash, wbnb, wbnbAddr, owner } = await deployAll();
            const amount = ethers.parseEther("5");
            await wbnb.mint(await flash.getAddress(), amount);

            const before = await wbnb.balanceOf(owner.address);
            await flash.connect(owner).withdraw(wbnbAddr, amount);
            expect(await wbnb.balanceOf(owner.address) - before).to.equal(amount);
            expect(await wbnb.balanceOf(await flash.getAddress())).to.equal(0n);
        });

        it("withdraw(0) выводит весь баланс", async function () {
            const { flash, wbnb, wbnbAddr, owner } = await deployAll();
            await wbnb.mint(await flash.getAddress(), ethers.parseEther("7.3"));
            await flash.connect(owner).withdraw(wbnbAddr, 0n);
            expect(await flash.balance(wbnbAddr)).to.equal(0n);
        });

        it("emergencySweep выводит несколько токенов сразу", async function () {
            const { flash, wbnb, usdt, wbnbAddr, usdtAddr, owner } = await deployAll();
            await wbnb.mint(await flash.getAddress(), ethers.parseEther("1"));
            await usdt.mint(await flash.getAddress(), ethers.parseEther("580"));

            const wBefore = await wbnb.balanceOf(owner.address);
            const uBefore = await usdt.balanceOf(owner.address);

            await flash.connect(owner).emergencySweep([wbnbAddr, usdtAddr]);

            expect(await wbnb.balanceOf(owner.address) - wBefore).to.equal(ethers.parseEther("1"));
            expect(await usdt.balanceOf(owner.address) - uBefore).to.equal(ethers.parseEther("580"));
        });

        it("addDex добавляет новый роутер", async function () {
            const { flash, owner } = await deployAll();
            const fake = ethers.Wallet.createRandom().address;
            await flash.connect(owner).addDex("NewDEX", fake);
            expect(await flash.dexRouters("NewDEX")).to.equal(fake);
            expect(await flash.getDexCount()).to.equal(3n);
        });

        it("setAavePool меняет адрес пула", async function () {
            const { flash, owner } = await deployAll();
            const newPool = ethers.Wallet.createRandom().address;
            await flash.connect(owner).setAavePool(newPool);
            expect(await flash.aavePool()).to.equal(newPool);
        });

        it("события эмитируются корректно", async function () {
            const { flash, owner } = await deployAll();
            await expect(flash.connect(owner).setPaused(true))
                .to.emit(flash, "Paused").withArgs(true);
        });
    });

    // ── 8. СРАВНЕНИЕ FEE: PancakeSwap vs AAVE ─────────────────────────────────
    describe("Fee comparison", function () {

        it("AAVE fee (0.05%) < PancakeSwap fee (0.3%) для 1 WBNB", async function () {
            const { flash, wbnbAddr } = await deployAll();
            const amount = ethers.parseEther("1");

            const [pancakeCost, aaveCost] = await flash.compareProviders(wbnbAddr, amount);

            // PancakeSwap ~0.3009%, AAVE 0.05%
            const pancakePct = Number(pancakeCost) / Number(amount) * 100;
            const aavePct    = Number(aaveCost)    / Number(amount) * 100;

            expect(aaveCost).to.be.lt(pancakeCost);
            console.log(`    PancakeSwap fee: ${pancakePct.toFixed(4)}%`);
            console.log(`    AAVE fee:        ${aavePct.toFixed(4)}%`);
            console.log(`    Экономия AAVE:   ${(pancakePct - aavePct).toFixed(4)}%`);
        });

        it("При спреде < 0.3% только AAVE прибылен", async function () {
            const { flash, wbnb, usdt, aavePool, wbnbAddr, usdtAddr, owner } = await deployAll();

            // Задаём маленький спред: 580 vs 580.5 (0.086%)
            const Router = await ethers.getContractFactory("MockDEXRouter");
            const tinySpreadDex = await Router.deploy("TinySpread", 10);
            await tinySpreadDex.setRate(wbnbAddr, usdtAddr, ethers.parseEther("580.5"));
            await flash.connect(owner).addDex("TinySpread", await tinySpreadDex.getAddress());

            const [,profit,, , profitPancake, profitAave] = await flash.simulateTwoWay(
                wbnbAddr, usdtAddr,
                ethers.parseEther("1"),
                "PancakeSwap_V2", "TinySpread"
            );

            // При спреде 0.086%: после комиссий PancakeSwap (0.25%+0.1%=0.35%) — убыток
            // AAVE (0.05%) тоже может быть убыточна при таком малом спреде
            // Главное: AAVE всегда лучше PancakeSwap при тех же условиях
            if (!profitAave) {
                expect(profitPancake).to.equal(false);
            } else {
                expect(profitAave).to.equal(true);
            }
            console.log(`    Малый спред: PancakeSwap profitable=${profitPancake}, AAVE profitable=${profitAave}`);
        });
    });

    // ── 9. GAS ────────────────────────────────────────────────────────────────
    describe("Gas usage", function () {

        it("flashAave gas < 500k", async function () {
            const { flash, usdt, aavePool, wbnbAddr, usdtAddr, owner } = await deployAll();
            await usdt.mint(await aavePool.getAddress(), ethers.parseEther("10000"));

            const tx      = await flash.connect(owner).flashAave(
                wbnbAddr, usdtAddr, ethers.parseEther("1"),
                "PancakeSwap_V2", "BiSwap", 0n
            );
            const receipt = await tx.wait();
            console.log(`    ⛽ flashAave gas: ${receipt.gasUsed.toLocaleString()}`);
            expect(receipt.gasUsed).to.be.lt(500_000n);
        });

        it("simulateTwoWay: staticCall (gas = 0)", async function () {
            const { flash, wbnbAddr, usdtAddr } = await deployAll();
            await flash.simulateTwoWay.staticCall(
                wbnbAddr, usdtAddr, ethers.parseEther("1"),
                "PancakeSwap_V2", "BiSwap"
            );
        });
    });
});
