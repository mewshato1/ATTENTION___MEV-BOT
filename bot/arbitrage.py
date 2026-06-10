# =============================================================================
# arbitrage.py — Логика поиска и оценки арбитражных возможностей
# =============================================================================
# Арбитраж: покупаем токен дешевле на одном DEX, продаём дороже на другом.
#
# Пример:
#   PancakeSwap: 1 BNB = 580 USDT
#   BiSwap:      1 BNB = 584 USDT
#   Спред: 0.69% → покупаем BNB на PancakeSwap, продаём на BiSwap → прибыль ~0.69%
#   Минус газ (≈ 0.003 BNB) — если остаток > MIN_PROFIT_BNB → исполняем
# =============================================================================

from dataclasses import dataclass, field
from typing import List, Optional, Dict
from web3 import Web3
from config import TOKENS, WATCH_PAIRS, MIN_PROFIT_BNB, MAX_TRADE_BNB, DEX, DEX_V3

# Единый справочник fee_bps для V2 и V3
_ALL_DEX_FEE: Dict[str, int] = {name: info["fee_bps"] for name, info in {**DEX, **DEX_V3}.items()}

# Импортируем get_last_v3_quotes здесь чтобы не делать это внутри цикла каждый блок
from multicall import get_last_v3_quotes as _get_v3_quotes
from dex import get_amount_out_for_trade, fetch_all_prices
from chain import estimate_gas_cost_bnb
from logger import get_logger

# Флаг: использовать multicall (быстро) или поочерёдные запросы (медленно)
_USE_MULTICALL = True

log = get_logger()


@dataclass
class ArbOpportunity:
    """Найденная арбитражная возможность."""
    token_a:        str        # Токен-вход (round-trip A→B→A)
    token_b:        str        # Промежуточный токен
    buy_dex:        str        # DEX для 1-й ноги A→B (MAX price = макс. B за A)
    sell_dex:       str        # DEX для 2-й ноги B→A (MIN price = макс. A за B)
    price_buy:      float      # price на buy_dex  (token_b за token_a)
    price_sell:     float      # price на sell_dex (token_b за token_a)
    spread_pct:     float      # Спред в процентах
    trade_amount:   float      # Размер сделки в BNB
    gross_profit:   float      # Прибыль до вычета газа (в BNB)
    gas_cost:       float      # Стоимость газа (в BNB)
    net_profit:     float      # Чистая прибыль (в BNB)
    profitable:     bool       # True если net_profit > MIN_PROFIT_BNB
    # Маршруты (список символов токенов). Длина > 2 означает multihop.
    buy_path:       List[str]  = field(default_factory=list)
    sell_path:      List[str]  = field(default_factory=list)

    def __str__(self):
        sign = "✅" if self.profitable else "⚠️"
        buy_route  = "→".join(self.buy_path)  if self.buy_path  else f"{self.token_a}→{self.token_b}"
        sell_route = "→".join(self.sell_path) if self.sell_path else f"{self.token_b}→{self.token_a}"
        return (
            f"{sign} ARB {self.token_a}/{self.token_b} | "
            f"Buy [{buy_route}] on {self.buy_dex} @ {self.price_buy:.6f} | "
            f"Sell [{sell_route}] on {self.sell_dex} @ {self.price_sell:.6f} | "
            f"Spread: {self.spread_pct:.3f}% | "
            f"Net: {self.net_profit:+.6f} BNB"
        )


def find_arbitrage(
    w3: Web3,
    token_a_sym: str,
    token_b_sym: str,
    trade_amount_bnb: float = None
) -> List[ArbOpportunity]:
    """
    Ищет арбитражные возможности для пары токенов по всем DEX.

    Алгоритм:
    1. Запрашиваем цены на всех DEX
    2. Находим DEX с минимальной и максимальной ценой
    3. Считаем спред и прибыль с учётом газа
    4. Возвращаем список возможностей (обычно 0 или 1)
    """
    trade_amount = trade_amount_bnb or MAX_TRADE_BNB
    opportunities = []

    token_a_addr = TOKENS.get(token_a_sym)
    token_b_addr = TOKENS.get(token_b_sym)
    if not token_a_addr or not token_b_addr:
        log.warning(f"Токены не найдены: {token_a_sym}, {token_b_sym}")
        return []

    # --- Шаг 1: Получаем цены на всех DEX ---
    prices = fetch_all_prices(w3, token_a_sym, token_b_sym)
    valid  = {dex: p for dex, p in prices.items() if p is not None and p > 0}

    if len(valid) < 2:
        log.debug(f"{token_a_sym}/{token_b_sym}: меньше 2 DEX с ценой, пропуск")
        return []

    log.debug(f"{token_a_sym}/{token_b_sym} цены: " +
              " | ".join(f"{d}: {p:.6f}" for d, p in valid.items()))

    # --- Шаг 2: Находим лучшую пару buy/sell ---
    # price = r_b / r_a = "token_b за 1 token_a". Round-trip A→B→A даёт:
    #   A_final = A × price_buy / price_sell
    # Прибыль возможна только при price_buy > price_sell.
    # buy_dex  = первая нога (A→B), хотим МАКС price → максимум B за A.
    # sell_dex = вторая нога (B→A), хотим МИН  price → максимум A за B.
    buy_dex  = max(valid, key=valid.get)
    sell_dex = min(valid, key=valid.get)

    price_buy  = valid[buy_dex]
    price_sell = valid[sell_dex]
    spread_pct = (price_buy - price_sell) / price_sell * 100

    if spread_pct <= 0:
        return []

    # --- Шаг 3: Считаем прибыль ---
    # Переводим trade_amount (в BNB) → реальное кол-во token_a в Wei.
    # Для не-WBNB: 0.5 BNB ≠ 0.5 token_a (иначе торгуем 0.5 BTCB вместо 0.5 BNB).
    if token_a_sym == "WBNB":
        bnb_rate = 1.0
    else:
        try:
            from multicall import get_last_reserves
            from triangle import _get_bnb_rate
            _cached = get_last_reserves()
            bnb_rate = _get_bnb_rate(token_a_sym, _cached) if _cached else 0.0
        except Exception:
            bnb_rate = 0.0
        if bnb_rate <= 0:
            log.debug(f"{token_a_sym}/{token_b_sym}: нет BNB rate для {token_a_sym}, пропуск")
            return []
    amount_in_wei = int(trade_amount * bnb_rate * 1e18)

    # Сколько token_b получим купив на buy_dex
    received_b_wei = get_amount_out_for_trade(
        w3, buy_dex, token_a_addr, token_b_addr, amount_in_wei
    )
    if not received_b_wei or received_b_wei == 0:
        log.debug(f"Не удалось рассчитать выход на {buy_dex}")
        return []

    # Сколько token_a получим продав token_b на sell_dex
    received_a_wei = get_amount_out_for_trade(
        w3, sell_dex, token_b_addr, token_a_addr, received_b_wei
    )
    if not received_a_wei or received_a_wei == 0:
        log.debug(f"Не удалось рассчитать обратный выход на {sell_dex}")
        return []

    # Прибыль = получили - вложили, конвертируем в BNB через bnb_rate
    gross_profit = (received_a_wei - amount_in_wei) / 1e18 / bnb_rate
    gas_cost     = estimate_gas_cost_bnb(w3) * 2  # Два свапа = двойной газ
    net_profit   = gross_profit - gas_cost
    profitable   = net_profit >= MIN_PROFIT_BNB

    opp = ArbOpportunity(
        token_a      = token_a_sym,
        token_b      = token_b_sym,
        buy_dex      = buy_dex,
        sell_dex     = sell_dex,
        price_buy    = price_buy,
        price_sell   = price_sell,
        spread_pct   = spread_pct,
        trade_amount = trade_amount,
        gross_profit = gross_profit,
        gas_cost     = gas_cost,
        net_profit   = net_profit,
        profitable   = profitable,
    )
    opportunities.append(opp)

    if profitable:
        log.profit(str(opp))
    else:
        log.debug(
            f"{token_a_sym}/{token_b_sym}: spread={spread_pct:.3f}% "
            f"gross={gross_profit:+.6f} gas={gas_cost:.6f} net={net_profit:+.6f} BNB — ниже порога"
        )

    return opportunities


def scan_all_pairs(w3: Web3) -> List[ArbOpportunity]:
    """
    Сканирует все пары из конфига и возвращает прибыльные возможности.

    Режим MULTICALL (по умолчанию):
      Все цены получаются за 2-4 RPC запроса независимо от числа пар/DEX.
      6 пар × 3 DEX = 18 вызовов → 2 батча → ~50мс вместо ~900мс.

    Режим SEQUENTIAL (fallback):
      Поочерёдные запросы — медленнее, но проще для отладки.
      Включить: установить _USE_MULTICALL = False выше.
    """
    global _USE_MULTICALL

    # ── Быстрый путь: multicall ────────────────────────────────────────────────
    if _USE_MULTICALL:
        try:
            return _scan_multicall(w3)
        except Exception as e:
            log.warning(f"Multicall упал ({e}), переключаюсь на sequential")
            _USE_MULTICALL = False   # Авто-откат на медленный режим

    # ── Медленный путь: один запрос за раз ────────────────────────────────────
    return _scan_sequential(w3)


def _scan_multicall(w3: Web3) -> List[ArbOpportunity]:
    """
    Быстрый скан через multicall — все пары за 2 RPC.

    RPC 1: V2 getReserves для всех пулов (calc_amount_out — точная формула x*y=k).
    RPC 2: V3 QuoterV2.quoteExactInputSingle батчем (точный amountOut, не виртуальные резервы).

    V2 profit: calc_amount_out(reserves) — точно.
    V3 profit: Quoter amountOut масштабируется на реальный trade_size — ~точно при малом slippage.
    """
    from multicall import scan_all_prices_multicall, get_last_reserves
    from dex import calc_amount_out
    try:
        from config import USE_OPTIMAL_SIZE
    except ImportError:
        USE_OPTIMAL_SIZE = False

    # Один вызов — все цены + резервы сохраняются
    all_prices   = scan_all_prices_multicall(w3)
    all_reserves = get_last_reserves()
    gas_cost     = estimate_gas_cost_bnb(w3) * 2

    try:
        from pool_filter import pool_filter as _pf
    except ImportError:
        _pf = None

    from multihop import find_best_route
    from config import DEX as _V2_DEX
    from triangle import _get_bnb_rate

    all_v3_quotes = _get_v3_quotes()

    all_opps = []
    for sym_a, sym_b in WATCH_PAIRS:
        pair_key = f"{sym_a}/{sym_b}"
        prices   = all_prices.get(pair_key, {})
        pair_reserves = all_reserves.get(pair_key, {})
        pair_v3_quotes = all_v3_quotes.get(pair_key, {})

        # ── Фильтр: только пулы с ценой И достаточной ликвидностью ────────
        # V2: проверяем резервы через pool_filter.
        # V3: проверяем что Quoter вернул ненулевой fwd результат.
        viable = {}
        for dex_name, price in prices.items():
            if price is None or price <= 0:
                continue
            if dex_name in DEX_V3:
                q = pair_v3_quotes.get(dex_name, {})
                if not q or q.get("fwd", 0) <= 0:
                    continue
            else:
                reserves = pair_reserves.get(dex_name)
                if not reserves:
                    continue
                r_a, r_b = reserves
                if _pf and not _pf.is_viable(pair_key, dex_name, r_a, r_b):
                    continue
            viable[dex_name] = price

        if len(viable) < 2:
            log.debug(f"SKIP {pair_key}: только {len(viable)} живых DEX ({list(viable.keys())})")
            continue

        # ── Конвертация BNB → token_a ─────────────────────────────────────
        bnb_rate = _get_bnb_rate(sym_a, all_reserves)
        if bnb_rate <= 0:
            log.debug(f"SKIP {pair_key}: нет BNB rate для {sym_a}")
            continue

        amount_in_wei = int(MAX_TRADE_BNB * bnb_rate * 1e18)
        trade_amount  = MAX_TRADE_BNB

        # ── Симуляция round-trip для КАЖДОЙ комбинации (buy_dex, sell_dex) ─
        # Сравнение по "цене" ломается на V2↔V3: V2 price = r_b/r_a (mid,
        # до slippage), V3 price = amount_out/amount_in Quoter (после slippage).
        # Несопоставимо → тонкий V3-пул выглядит "дешёвым" и съедает сделку
        # на обратном свапе. Правильно — считать реальный amount_out на обеих
        # ногах для всех комбинаций и выбирать комбо с максимальным net.
        def _simulate_buy(dex_name: str, amt_in: int):
            """Возвращает (received_b, hop_token_or_None)."""
            if dex_name in _V2_DEX:
                return find_best_route(sym_a, sym_b, dex_name, amt_in, all_reserves)
            if dex_name in DEX_V3:
                q = pair_v3_quotes.get(dex_name, {})
                std_in  = q.get("amount_a", 0)
                std_out = q.get("fwd", 0)
                if std_in <= 0 or std_out <= 0:
                    return 0, None
                return int(std_out * amt_in / std_in), None
            return 0, None

        def _simulate_sell(dex_name: str, amt_in: int):
            """Возвращает (received_a, hop_token_or_None)."""
            if dex_name in _V2_DEX:
                return find_best_route(sym_b, sym_a, dex_name, amt_in, all_reserves)
            if dex_name in DEX_V3:
                q = pair_v3_quotes.get(dex_name, {})
                std_in  = q.get("amount_b", 0)
                std_out = q.get("rev", 0)
                if std_in <= 0 or std_out <= 0:
                    return 0, None
                return int(std_out * amt_in / std_in), None
            return 0, None

        best = None  # (net, gross, buy_dex, sell_dex, rec_b, rec_a, buy_hop, sell_hop)
        for buy_dex in viable:
            rec_b, buy_hop = _simulate_buy(buy_dex, amount_in_wei)
            if not rec_b:
                continue
            for sell_dex in viable:
                if sell_dex == buy_dex:
                    continue
                rec_a, sell_hop = _simulate_sell(sell_dex, rec_b)
                if not rec_a:
                    continue
                gross = (rec_a - amount_in_wei) / 1e18 / bnb_rate
                net   = gross - gas_cost
                if best is None or net > best[0]:
                    best = (net, gross, buy_dex, sell_dex, rec_b, rec_a, buy_hop, sell_hop)

        if best is None:
            continue

        net, gross, buy_dex, sell_dex, received_b, received_a, buy_hop, sell_hop = best

        p_buy    = viable[buy_dex]
        p_sell   = viable[sell_dex]
        spread   = (p_buy - p_sell) / p_sell * 100 if p_sell > 0 else 0.0

        buy_path  = [sym_a, buy_hop, sym_b]  if buy_hop  else [sym_a, sym_b]
        sell_path = [sym_b, sell_hop, sym_a] if sell_hop else [sym_b, sym_a]

        is_prof = net >= MIN_PROFIT_BNB

        opp = ArbOpportunity(
            token_a      = sym_a,
            token_b      = sym_b,
            buy_dex      = buy_dex,
            sell_dex     = sell_dex,
            price_buy    = p_buy,
            price_sell   = p_sell,
            spread_pct   = spread,
            trade_amount = trade_amount,
            gross_profit = gross,
            gas_cost     = gas_cost,
            net_profit   = net,
            profitable   = is_prof,
            buy_path     = buy_path,
            sell_path    = sell_path,
        )
        all_opps.append(opp)

        if is_prof:
            log.profit(str(opp))
        else:
            log.debug(f"{sym_a}/{sym_b}: best net={net:+.6f} BNB gross={gross:+.6f} [{buy_dex}→{sell_dex}]")

    all_opps.sort(key=lambda o: o.net_profit, reverse=True)
    return all_opps


def _scan_sequential(w3: Web3) -> List[ArbOpportunity]:
    """Медленный скан — один RPC за раз. Fallback при недоступности multicall."""
    all_opps = []
    for sym_a, sym_b in WATCH_PAIRS:
        try:
            opps = find_arbitrage(w3, sym_a, sym_b)
            all_opps.extend(opps)
        except Exception as e:
            log.error(f"Ошибка сканирования {sym_a}/{sym_b}: {e}")
    all_opps.sort(key=lambda o: o.net_profit, reverse=True)
    return all_opps
