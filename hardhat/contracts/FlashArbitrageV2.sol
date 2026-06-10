// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

// =============================================================================
// FlashArbitrageV2.sol — Оптимизированный флэшлоан арбитраж для BNB Chain
// =============================================================================
//
// Оптимизации по сравнению с V1:
//   1. _swap() больше НЕ вызывает getAmountsOut() — экономия ~5000 gas за свап
//      Защита через minProfit revert в конце, а не slippage на каждом шаге
//   2. approveMax() — одноразовый безлимитный approve для роутеров
//      Экономия ~46000 gas за свап (нет approve на каждой сделке)
//   3. Проверка allowance перед approve (fallback если не вызвали approveMax)
//   4. bytes32 ключи для DEX вместо string — экономия ~2000-5000 gas
//   5. Флаг-байт вместо try/catch для определения типа арбитража
//   6. unchecked арифметика где безопасно
//   7. Кэширование переменных в memory вместо повторных SLOAD
//
// Суммарная экономия: ~100,000-150,000 gas за сделку
// =============================================================================

// ─────────────────────────────────────────────────────────────────────────────
// ИНТЕРФЕЙСЫ
// ─────────────────────────────────────────────────────────────────────────────

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function allowance(address owner, address spender) external view returns (uint256);
}

interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);

    function getAmountsOut(
        uint256 amountIn,
        address[] calldata path
    ) external view returns (uint256[] memory amounts);
}

interface IUniswapV2Pair {
    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;
    function token0() external view returns (address);
    function token1() external view returns (address);
    function getReserves() external view returns (uint112, uint112, uint32);
}

interface IPoolAAVE {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16  referralCode
    ) external;
}

interface IFlashLoanSimpleReceiver {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}

interface IPancakeCallee {
    function pancakeCall(address sender, uint256 amount0, uint256 amount1, bytes calldata data) external;
}

// ─────────────────────────────────────────────────────────────────────────────
// ГЛАВНЫЙ КОНТРАКТ
// ─────────────────────────────────────────────────────────────────────────────

contract FlashArbitrageV2 is IFlashLoanSimpleReceiver, IPancakeCallee {

    // ── Константы ─────────────────────────────────────────────────────────────

    uint16  private constant AAVE_REFERRAL     = 0;
    uint256 private constant DEADLINE_BUFFER   = 60;

    // Типы арбитража (кодируются в params)
    uint8   private constant ARB_TWO_WAY   = 0;
    uint8   private constant ARB_TRIANGLE  = 1;

    // ── Reentrancy guard ──────────────────────────────────────────────────────
    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED     = 2;
    uint256 private _status = _NOT_ENTERED;

    // ── Состояние ─────────────────────────────────────────────────────────────

    address public owner;
    bool    public paused;
    address public aavePool;

    // DEX роутеры: bytes32(name) → адрес (дешевле чем string mapping)
    mapping(bytes32 => address) public dexRouters;
    bytes32[]                   public dexKeys;

    // Обратная совместимость: string → bytes32 конвертация
    // Клиент по-прежнему передаёт строки, конвертируем внутри
    mapping(string => bytes32) public dexNameToKey;
    string[] public dexNames;  // для UI/view

    // ── Кастомные ошибки ──────────────────────────────────────────────────────

    error NotOwner();
    error ContractPaused();
    error Reentrancy();
    error ZeroAmount();
    error InvalidCallback(address caller);
    error RouterNotFound(string name);
    error InsufficientProfit(uint256 got, uint256 need);
    error TransferFailed(address token, address to, uint256 amount);

    // ── События ───────────────────────────────────────────────────────────────

    event ArbitrageExecuted(
        uint8   indexed provider,   // 0=PANCAKE, 1=AAVE
        address indexed tokenA,
        address         tokenB,
        uint256         flashAmount,
        uint256         profit,
        uint256         timestamp
    );
    event DexAdded(string name, address router);
    event Withdrawn(address token, address to, uint256 amount);
    event Paused(bool status);
    event MaxApproved(address token, address router);

    // ── Модификаторы ──────────────────────────────────────────────────────────

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier notPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    modifier nonReentrant() {
        if (_status == _ENTERED) revert Reentrancy();
        _status = _ENTERED;
        _;
        _status = _NOT_ENTERED;
    }

    // ── Конструктор ───────────────────────────────────────────────────────────

    constructor(
        address   _aavePool,
        address[] memory _routerAddrs,
        string[]  memory _routerNames
    ) {
        require(_routerAddrs.length == _routerNames.length, "Length mismatch");
        owner    = msg.sender;
        aavePool = _aavePool;
        for (uint i = 0; i < _routerAddrs.length; i++) {
            require(_routerAddrs[i] != address(0), "Zero router");
            bytes32 key = keccak256(bytes(_routerNames[i]));
            dexRouters[key] = _routerAddrs[i];
            dexKeys.push(key);
            dexNameToKey[_routerNames[i]] = key;
            dexNames.push(_routerNames[i]);
            emit DexAdded(_routerNames[i], _routerAddrs[i]);
        }
    }

    // =========================================================================
    // ОПТИМИЗАЦИЯ #1: Одноразовый безлимитный approve
    // =========================================================================
    //
    // Вызвать ОДИН РАЗ после деплоя для каждой пары (токен, роутер).
    // После этого _swap() не будет тратить ~46000 gas на approve при каждой сделке.

    function approveMax(address token, address router) external onlyOwner {
        IERC20(token).approve(router, type(uint256).max);
        emit MaxApproved(token, router);
    }

    /// @notice Массовый approve — вызвать после деплоя
    function approveMaxBatch(
        address[] calldata tokens,
        address[] calldata routers
    ) external onlyOwner {
        for (uint i = 0; i < tokens.length; i++) {
            for (uint j = 0; j < routers.length; j++) {
                IERC20(tokens[i]).approve(routers[j], type(uint256).max);
                emit MaxApproved(tokens[i], routers[j]);
            }
        }
    }

    // =========================================================================
    // ПУБЛИЧНЫЕ ТОЧКИ ВХОДА
    // =========================================================================

    function flashPancake(
        address pairAddress,
        address tokenA,
        address tokenB,
        uint256 flashAmount,
        string  calldata buyDex,
        string  calldata sellDex,
        uint256 minProfit
    )
        external
        onlyOwner
        notPaused
        nonReentrant
    {
        if (flashAmount == 0) revert ZeroAmount();
        address buyRouter  = _requireRouter(buyDex);
        address sellRouter = _requireRouter(sellDex);

        // Кодируем параметры компактно
        bytes memory params = abi.encode(
            ARB_TWO_WAY,          // тип арбитража
            tokenA,
            tokenB,
            address(0),           // tokenC (не используется)
            buyRouter,
            sellRouter,
            address(0),           // dex2Router (не используется)
            flashAmount,
            minProfit,
            pairAddress           // flashSource
        );

        IUniswapV2Pair pair  = IUniswapV2Pair(pairAddress);
        address        token0 = pair.token0();

        uint256 out0 = (tokenA == token0) ? flashAmount : 0;
        uint256 out1 = (tokenA == token0) ? 0 : flashAmount;

        pair.swap(out0, out1, address(this), params);
    }

    function flashAave(
        address tokenA,
        address tokenB,
        uint256 flashAmount,
        string  calldata buyDex,
        string  calldata sellDex,
        uint256 minProfit
    )
        external
        onlyOwner
        notPaused
        nonReentrant
    {
        if (flashAmount == 0) revert ZeroAmount();
        address buyRouter  = _requireRouter(buyDex);
        address sellRouter = _requireRouter(sellDex);

        bytes memory params = abi.encode(
            ARB_TWO_WAY,
            tokenA,
            tokenB,
            address(0),
            buyRouter,
            sellRouter,
            address(0),
            flashAmount,
            minProfit,
            aavePool
        );

        IPoolAAVE(aavePool).flashLoanSimple(
            address(this),
            tokenA,
            flashAmount,
            params,
            AAVE_REFERRAL
        );
    }

    function flashAaveTriangle(
        address tokenA,
        address tokenB,
        address tokenC,
        uint256 flashAmount,
        string  calldata dex1,
        string  calldata dex2,
        string  calldata dex3,
        uint256 minProfit
    )
        external
        onlyOwner
        notPaused
        nonReentrant
    {
        if (flashAmount == 0) revert ZeroAmount();
        if (tokenC == address(0)) revert ZeroAmount();

        address dex1Router = _requireRouter(dex1);
        address dex2Router = _requireRouter(dex2);
        address dex3Router = _requireRouter(dex3);

        bytes memory params = abi.encode(
            ARB_TRIANGLE,         // тип = треугольный
            tokenA,
            tokenB,
            tokenC,
            dex1Router,           // buyRouter = dex1
            dex3Router,           // sellRouter = dex3
            dex2Router,           // dex2Router
            flashAmount,
            minProfit,
            aavePool
        );

        IPoolAAVE(aavePool).flashLoanSimple(
            address(this),
            tokenA,
            flashAmount,
            params,
            AAVE_REFERRAL
        );
    }

    // =========================================================================
    // CALLBACKS
    // =========================================================================

    function pancakeCall(
        address sender,
        uint256 /*amount0*/,
        uint256 /*amount1*/,
        bytes calldata data
    ) external override {
        // Декодируем тип + параметры
        (
            uint8   arbType,
            address tokenA,
            address tokenB,
            ,  // tokenC
            address buyRouter,
            address sellRouter,
            ,  // dex2Router
            uint256 flashAmount,
            uint256 minProfit,
            address flashSource
        ) = abi.decode(data, (uint8, address, address, address, address, address, address, uint256, uint256, address));

        // Проверки безопасности
        if (sender     != address(this)) revert InvalidCallback(sender);
        if (msg.sender != flashSource)   revert InvalidCallback(msg.sender);

        // Арбитраж (только двусторонний для PancakeSwap)
        uint256 receivedB = _swapOptimized(buyRouter,  tokenA, tokenB, flashAmount);
        uint256 receivedA = _swapOptimized(sellRouter, tokenB, tokenA, receivedB);

        // Долг PancakeSwap: flashAmount * 1000 / 997
        uint256 debt;
        unchecked {
            debt = (flashAmount * 1000 + 996) / 997;
        }

        if (receivedA < debt) revert InsufficientProfit(0, minProfit);
        unchecked {
            uint256 profit = receivedA - debt;
            if (profit < minProfit) revert InsufficientProfit(profit, minProfit);

            _safeTransfer(tokenA, flashSource, debt);
            emit ArbitrageExecuted(0, tokenA, tokenB, flashAmount, profit, block.timestamp);
        }
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        if (msg.sender != aavePool)      revert InvalidCallback(msg.sender);
        if (initiator  != address(this)) revert InvalidCallback(initiator);

        uint256 debt = amount + premium;

        // Читаем тип арбитража из первого слота params (без try/catch!)
        uint8 arbType = abi.decode(params[:32], (uint8));

        if (arbType == ARB_TRIANGLE) {
            _executeTriangleArb(params, amount, debt);
        } else {
            _executeTwoWayArb(params, amount, debt);
        }

        // Approve AAVE на возврат долга
        IERC20(asset).approve(aavePool, debt);
        return true;
    }

    // =========================================================================
    // ВНУТРЕННИЕ ФУНКЦИИ
    // =========================================================================

    function _executeTwoWayArb(bytes calldata params, uint256 amount, uint256 debt) internal {
        (
            ,  // arbType
            address tokenA,
            address tokenB,
            ,  // tokenC
            address buyRouter,
            address sellRouter,
            ,  // dex2Router
            ,  // flashAmount
            uint256 minProfit,
              // flashSource
        ) = abi.decode(params, (uint8, address, address, address, address, address, address, uint256, uint256, address));

        uint256 receivedB = _swapOptimized(buyRouter,  tokenA, tokenB, amount);
        uint256 receivedA = _swapOptimized(sellRouter, tokenB, tokenA, receivedB);

        if (receivedA < debt) revert InsufficientProfit(0, minProfit);
        unchecked {
            uint256 profit = receivedA - debt;
            if (profit < minProfit) revert InsufficientProfit(profit, minProfit);
            emit ArbitrageExecuted(1, tokenA, tokenB, amount, profit, block.timestamp);
        }
    }

    function _executeTriangleArb(bytes calldata params, uint256 amount, uint256 debt) internal {
        (
            ,  // arbType
            address tokenA,
            address tokenB,
            address tokenC,
            address dex1Router,
            address dex3Router,
            address dex2Router,
            ,  // flashAmount
            uint256 minProfit,
              // flashSource
        ) = abi.decode(params, (uint8, address, address, address, address, address, address, uint256, uint256, address));

        // A → B → C → A
        uint256 receivedB = _swapOptimized(dex1Router, tokenA, tokenB, amount);
        uint256 receivedC = _swapOptimized(dex2Router, tokenB, tokenC, receivedB);
        uint256 receivedA = _swapOptimized(dex3Router, tokenC, tokenA, receivedC);

        if (receivedA < debt) revert InsufficientProfit(0, minProfit);
        unchecked {
            uint256 profit = receivedA - debt;
            if (profit < minProfit) revert InsufficientProfit(profit, minProfit);
            emit ArbitrageExecuted(1, tokenA, tokenB, amount, profit, block.timestamp);
        }
    }

    /// @notice ОПТИМИЗИРОВАННЫЙ свап: без getAmountsOut, без approve на каждый вызов
    /// Защита от убытка — через minProfit revert в конце арбитража
    function _swapOptimized(
        address router,
        address tokenIn,
        address tokenOut,
        uint256 amountIn
    ) internal returns (uint256) {
        // Проверяем allowance — если уже дали max approve, пропускаем (~46000 gas)
        if (IERC20(tokenIn).allowance(address(this), router) < amountIn) {
            IERC20(tokenIn).approve(router, type(uint256).max);
        }

        address[] memory path = new address[](2);
        path[0] = tokenIn;
        path[1] = tokenOut;

        // amountOutMin = 1 — мы НЕ полагаемся на slippage per-swap
        // Вместо этого: проверка minProfit в конце всей операции
        // Это безопасно т.к. вся операция атомарна (flashloan callback)
        uint256[] memory amounts = IUniswapV2Router(router).swapExactTokensForTokens(
            amountIn,
            1,                              // ← экономим ~5000 gas (нет getAmountsOut)
            path,
            address(this),
            block.timestamp + DEADLINE_BUFFER
        );

        return amounts[amounts.length - 1];
    }

    function _safeTransfer(address token, address to, uint256 amount) internal {
        bool ok = IERC20(token).transfer(to, amount);
        if (!ok) revert TransferFailed(token, to, amount);
    }

    function _requireRouter(string calldata name) internal view returns (address r) {
        bytes32 key = dexNameToKey[name];
        r = dexRouters[key];
        if (r == address(0)) {
            // Fallback: попробуем хеш напрямую (для новых DEX)
            r = dexRouters[keccak256(bytes(name))];
            if (r == address(0)) revert RouterNotFound(name);
        }
    }

    // =========================================================================
    // VIEW ФУНКЦИИ (gas = 0)
    // =========================================================================

    function simulateTwoWay(
        address tokenA,
        address tokenB,
        uint256 amountIn,
        string  calldata buyDex,
        string  calldata sellDex
    ) external view returns (
        uint256 amountOut,
        int256  profit,
        uint256 pancakeDebt,
        uint256 aavePremium,
        bool    profitableAfterPancake,
        bool    profitableAfterAave
    ) {
        address buyRouter  = _requireRouter(buyDex);
        address sellRouter = _requireRouter(sellDex);

        address[] memory path = new address[](2);

        path[0] = tokenA; path[1] = tokenB;
        uint256[] memory out1 = IUniswapV2Router(buyRouter).getAmountsOut(amountIn, path);

        path[0] = tokenB; path[1] = tokenA;
        uint256[] memory out2 = IUniswapV2Router(sellRouter).getAmountsOut(out1[1], path);

        amountOut = out2[1];
        profit    = int256(amountOut) - int256(amountIn);

        unchecked {
            pancakeDebt = (amountIn * 1000 + 996) / 997;
            aavePremium = amountIn + (amountIn * 5) / 10000;
        }

        profitableAfterPancake = amountOut > pancakeDebt;
        profitableAfterAave    = amountOut > aavePremium;
    }

    function simulateTriangle(
        address tokenA,
        address tokenB,
        address tokenC,
        uint256 amountIn,
        string  calldata dex1,
        string  calldata dex2,
        string  calldata dex3
    ) external view returns (
        uint256 finalAmount,
        int256  profit,
        bool    isProfitableAfterAave
    ) {
        address r1 = _requireRouter(dex1);
        address r2 = _requireRouter(dex2);
        address r3 = _requireRouter(dex3);

        if (r1 == address(0) || r2 == address(0) || r3 == address(0)) {
            return (0, 0, false);
        }

        address[] memory path = new address[](2);

        path[0] = tokenA; path[1] = tokenB;
        uint256 bOut = IUniswapV2Router(r1).getAmountsOut(amountIn, path)[1];

        path[0] = tokenB; path[1] = tokenC;
        uint256 cOut = IUniswapV2Router(r2).getAmountsOut(bOut, path)[1];

        path[0] = tokenC; path[1] = tokenA;
        uint256 aOut = IUniswapV2Router(r3).getAmountsOut(cOut, path)[1];

        finalAmount           = aOut;
        profit                = int256(aOut) - int256(amountIn);
        isProfitableAfterAave = aOut > amountIn + (amountIn * 5) / 10000;
    }

    function compareProviders(
        address /* token */,
        uint256 amount
    ) external pure returns (
        uint256 pancakeCost,
        uint256 aaveCost,
        string  memory cheaper
    ) {
        unchecked {
            pancakeCost = (amount * 1000 + 996) / 997 - amount;
            aaveCost    = (amount * 5) / 10000;
        }
        cheaper = aaveCost < pancakeCost ? "AAVE" : "PANCAKE";
    }

    function balance(address token) external view returns (uint256) {
        return IERC20(token).balanceOf(address(this));
    }

    function getDexCount() external view returns (uint256) {
        return dexNames.length;
    }

    // =========================================================================
    // УПРАВЛЕНИЕ
    // =========================================================================

    function addDex(string calldata name, address router) external onlyOwner {
        require(router != address(0), "Zero router");
        bytes32 key = keccak256(bytes(name));
        dexRouters[key] = router;
        dexKeys.push(key);
        dexNameToKey[name] = key;
        dexNames.push(name);
        emit DexAdded(name, router);
    }

    function setAavePool(address pool) external onlyOwner {
        require(pool != address(0), "Zero pool");
        aavePool = pool;
    }

    function setPaused(bool _p) external onlyOwner {
        paused = _p;
        emit Paused(_p);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "Zero address");
        owner = newOwner;
    }

    function withdraw(address token, uint256 amount) external onlyOwner {
        uint256 bal    = IERC20(token).balanceOf(address(this));
        uint256 toSend = (amount == 0) ? bal : amount;
        require(toSend <= bal, "Insufficient");
        _safeTransfer(token, owner, toSend);
        emit Withdrawn(token, owner, toSend);
    }

    function withdrawBNB() external onlyOwner {
        uint256 bal = address(this).balance;
        require(bal > 0, "No BNB");
        (bool ok,) = owner.call{value: bal}("");
        require(ok, "BNB transfer failed");
        emit Withdrawn(address(0), owner, bal);
    }

    function emergencySweep(address[] calldata tokens) external onlyOwner {
        for (uint i = 0; i < tokens.length; i++) {
            uint256 bal = IERC20(tokens[i]).balanceOf(address(this));
            if (bal > 0) {
                IERC20(tokens[i]).transfer(owner, bal);
            }
        }
        if (address(this).balance > 0) {
            (bool ok,) = owner.call{value: address(this).balance}("");
            require(ok, "BNB sweep failed");
        }
    }

    receive() external payable {}
}
