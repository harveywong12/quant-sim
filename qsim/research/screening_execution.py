"""Research-only stock/ETF basket executor. Never writes the paper ledger.

Unlike legacy parity replay, rejected sells retain their position. Signal
selection, lot budgets and matchers remain the existing strategy contracts.
"""
from __future__ import annotations
import math
import pandas as pd
from qsim.engine.cross_sectional_runner import affordable_equal_weight_targets, target_shares_for_budget
from qsim.engine.matcher import LOT_SIZE, buy_exact, sell_exact
from qsim.strategies.cross_sectional import CrossSectionContext


def simulate_basket(opens, closes, scores, strategy, days, cost, *, sizing="parity",
                    allocations=None, volume=None, participation=.05, initial=100000.):
    closes = closes.ffill()  # past marks only, never backfill a future price
    previous = closes.shift(1)
    cash, holdings, pending = initial, {}, None
    trades, skips, equity, signals = [], [], [], []
    allocations = allocations or {}
    for date in days:
        prices = opens.loc[date]
        if pending is not None:
            signal_date, candidates = pending
            allocation = min(1., max(0., float(allocations.get(signal_date, 1.))))
            # Selection affordability must use the opening mark, not today's close.
            opening_value = cash + sum(n * float(prices[s] if pd.notna(prices[s]) else previous.loc[date, s])
                                       for s, n in holdings.items())
            selected = ([] if allocation == 0 else
                        affordable_equal_weight_targets(candidates, prices, opening_value * allocation,
                                                        strategy.top_n, cost)
                        if sizing == "live_affordable" else candidates[:strategy.top_n])
            if selected or allocation == 0:
                denom = len(selected) if sizing == "live_affordable" else strategy.top_n
                # Sell removed names first, then compute target budgets at open.
                def fill(symbol, desired):
                    nonlocal cash
                    old = holdings.get(symbol, 0)
                    if symbol not in prices or pd.isna(prices[symbol]) or prices[symbol] <= 0:
                        skips.append({"date": date, "symbol": symbol, "reason": "开盘价缺失"})
                        return
                    n = abs(desired)
                    if volume is not None:
                        v = volume.loc[date, symbol]
                        cap = int(float(v) * participation / LOT_SIZE) * LOT_SIZE if pd.notna(v) and v > 0 else 0
                        n = min(n, cap)
                    if n < LOT_SIZE:
                        skips.append({"date": date, "symbol": symbol, "reason": "成交量不足一手"})
                        return
                    prev = previous.loc[date, symbol]
                    matcher = sell_exact if desired < 0 else buy_exact
                    cash, actual, trade, reason = matcher(cash, old, float(prices[symbol]), date, cost,
                        symbol, n, prev_close=float(prev) if pd.notna(prev) else None)
                    if actual:
                        holdings[symbol] = actual
                    else:
                        holdings.pop(symbol, None)
                    if trade:
                        trades.append(trade.to_dict())
                    else:
                        skips.append({"date": date, "symbol": symbol, "reason": reason or "未成交"})
                for symbol in list(holdings):
                    if symbol not in selected:
                        fill(symbol, -holdings[symbol])
                opening_value = cash + sum(n * float(prices[s] if pd.notna(prices[s]) else previous.loc[date, s])
                                           for s, n in holdings.items())
                budget = opening_value * allocation / denom if denom and selected else 0
                targets = {}
                for s in selected:
                    if pd.isna(prices[s]) or prices[s] <= 0:
                        skips.append({"date": date, "symbol": s, "reason": "开盘价缺失"})
                        continue
                    targets[s] = (target_shares_for_budget(budget, float(prices[s]), s, cost)
                                  if sizing == "live_affordable" else int(budget / float(prices[s]) / LOT_SIZE) * LOT_SIZE)
                # Release cash before buying; a rejected or partial sell preserves shares.
                for side in (-1, 1):
                    for s, n in targets.items():
                        diff = n - holdings.get(s, 0)
                        if abs(diff) >= LOT_SIZE and (diff < 0) == (side < 0):
                            fill(s, diff)
            pending = None
        value = sum(n * float(closes.loc[date, s]) for s, n in holdings.items())
        if not math.isfinite(value):
            raise ValueError("position has no known historical mark")
        equity.append({"date": date, "cash": cash, "position_value": value, "total_value": cash + value})
        if date in scores.index:
            row = scores.loc[date].dropna()
            context = CrossSectionContext(date, closes.loc[date], list(row.index), row)
            ranked = strategy.rank(context, limit=strategy.top_n * 5)
            if ranked:
                pending = (date, ranked)
                signals.append({"date": date, "candidates": ranked, "allocation": allocations.get(date, 1.)})
    return {"equity": pd.DataFrame(equity), "trades": trades, "skips": skips,
            "signals": signals, "shares": holdings, "cash": cash}
