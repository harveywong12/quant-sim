"""Generic cross-sectional backtest runner.

The runner assumes factor scores are precomputed as a date x stock panel.
It only performs stock selection on rebalance dates and executes the target
portfolio on the next trading day's open.  Non-rebalance days only update NAV.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.execution import ExecutionPolicy
from qsim.engine.matcher import LOT_SIZE, buy_exact, sell_exact, minimum_order_quantity, quantity_step, valid_sell_quantity
from qsim.engine.cash_distributions import CashDistributionBook
from qsim.engine.metrics import portfolio_metrics
from qsim.strategies.cross_sectional import CrossSectionContext, CrossSectionalStrategy


@dataclass(frozen=True, slots=True)
class CrossSectionalData:
    dates: list[str]
    factor_scores: pd.DataFrame
    open: pd.DataFrame
    close: pd.DataFrame
    universe: pd.DataFrame | None = None
    volume: pd.DataFrame | None = None
    # Equal-weight scores used only when the primary (IC-weighted) panel has
    # no row for a rebalance date.  This mirrors the legacy fallback to
    # ``factor.rank_universe`` for dates whose IC labels have not settled.
    fallback_scores: pd.DataFrame | None = None
    pre_close: pd.DataFrame | None = None

    def __post_init__(self) -> None:
        if not self.dates:
            raise ValueError("dates must not be empty")
        if not self.factor_scores.index.isin(self.dates).all():
            raise ValueError("factor_scores index contains unknown dates")


def monthly_rebalance_dates(dates: Iterable[str]) -> list[str]:
    """First trading day of each month, matching the legacy backtests."""
    out: list[str] = []
    last_month: str | None = None
    for date in sorted(dates):
        month = date[:6]
        if month != last_month:
            out.append(date)
            last_month = month
    return out


def weekly_rebalance_dates(dates: Iterable[str]) -> list[str]:
    out: list[str] = []
    last_week: tuple[int, int] | None = None
    for date in sorted(dates):
        d = dt.date(int(date[:4]), int(date[4:6]), int(date[6:8]))
        week = d.isocalendar()[:2]
        if week != last_week:
            last_week = week
            out.append(date)
    return out


def _position_value(shares: int, price: float) -> float:
    return shares * price


def affordable_equal_weight_targets(
    ranked_codes: Iterable[str],
    prices: pd.Series,
    total_value: float,
    desired_count: int,
    cost: CostModel,
) -> list[str]:
    """Return the best-ranked A-share targets that can each buy one lot.

    The legacy live allocator ranks more candidates than its final portfolio
    size, then removes symbols that cannot fill one board lot at equal weight.
    Keeping that step in qsim prevents a high-priced top score from silently
    collapsing a 10-name sleeve into one name plus cash.
    """
    ranked = list(dict.fromkeys(str(code) for code in ranked_codes))
    desired = min(max(int(desired_count), 0), len(ranked))
    capital = max(float(total_value), 0.0)
    for count in range(desired, 0, -1):
        per_position = capital / count
        selected: list[str] = []
        for symbol in ranked:
            if symbol not in prices.index or pd.isna(prices[symbol]):
                continue
            open_price = float(prices[symbol])
            if open_price <= 0:
                continue
            fill = cost.fill_price(open_price, "BUY", symbol)
            minimum = minimum_order_quantity(symbol)
            lot_cost = fill * minimum + cost.calc_fee(fill, minimum, "BUY", symbol)
            if lot_cost <= per_position:
                selected.append(symbol)
                if len(selected) == count:
                    return selected
    return []


def target_shares_for_budget(
    budget: float,
    open_price: float,
    symbol: str,
    cost: CostModel,
) -> int:
    """Largest board-lot target whose filled value and buy fee fit budget."""
    if budget <= 0 or open_price <= 0:
        return 0
    fill = cost.fill_price(open_price, "BUY", symbol)
    step, minimum = quantity_step(symbol), minimum_order_quantity(symbol)
    shares = int(float(budget) / fill / step) * step
    while shares >= minimum:
        total = fill * shares + cost.calc_fee(fill, shares, "BUY", symbol)
        if total <= budget + 1e-9:
            return shares
        shares -= step
    return 0


def run_cross_sectional_backtest(
    data: CrossSectionalData,
    strategy: CrossSectionalStrategy,
    initial_cash: float,
    cost: CostModel,
    rebalance: str = "monthly",
    start: str | None = None,
    end: str | None = None,
    rebalance_dates: Iterable[str] | None = None,
    allocation_by_signal_date: dict[str, float] | None = None,
    sizing: str = "parity",
    policy: ExecutionPolicy | None = None,
    distributions=(),
    frozen_signals: dict | None = None,
    strict_distributions: bool = False, rounding_policy: str = "require_evidence",
) -> dict:
    if sizing not in ("parity", "live_affordable"):
        raise ValueError(f"unknown sizing policy: {sizing}")
    dates = data.dates
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]

    if rebalance_dates is not None:
        selected_rebalance_dates = set(rebalance_dates).intersection(dates)
    elif rebalance == "monthly":
        selected_rebalance_dates = set(monthly_rebalance_dates(dates))
    elif rebalance == "weekly":
        selected_rebalance_dates = set(weekly_rebalance_dates(dates))
    elif rebalance == "daily":
        selected_rebalance_dates = set(dates)
    else:
        raise ValueError(f"unknown rebalance rule: {rebalance}")

    if hasattr(strategy, "reset"):
        strategy.reset()

    # sizing="parity" 复刻 legacy alpha_engine 的仓位数学（无资金分配时的
    # 纯回测口径）；sizing="live_affordable" 是实盘/风控路径，带可负担性
    # 过滤与费用预留 sizing。两者是同一执行核心上的显式策略，不是隐式分支。
    live_sizing = sizing == "live_affordable"
    cash = initial_cash
    shares: dict[str, int] = {}
    avg_cost: dict[str, float] = {}
    pending_candidates: list[str] | None = None
    pending_signal_date: str | None = None
    trades: list[dict] = []
    signals: list[dict] = []
    skips: list[dict] = []
    equity_rows: list[dict] = []
    close_v = data.close.ffill()
    book = CashDistributionBook(distributions, strict_rounding=strict_distributions, rounding_policy=rounding_policy)
    decisions = []
    distribution_nav = []
    allocations = dict(allocation_by_signal_date or {})

    def _fill_cap(sym: str, date: str) -> int | None:
        if policy is None:
            return None
        if data.volume is None or date not in data.volume.index or sym not in data.volume.columns:
            return policy.fill_cap(None)
        v = data.volume.loc[date, sym]
        return policy.fill_cap(float(v) if pd.notna(v) else None)

    def _record_skip(date: str, sym: str, reason: str) -> None:
        if policy is not None and policy.record_suspensions:
            skips.append({"date": date, "symbol": sym, "reason": reason})

    for date in dates:
        cash += book.open(date)
        open_row = data.open.loc[date] if date in data.open.index else pd.Series(dtype=float)
        close_row = close_v.loc[date] if date in close_v.index else pd.Series(dtype=float)
        prev_close_row = pd.Series(dtype=float)
        if date in data.close.index:
            idx = data.close.index.get_loc(date)
            if idx > 0:
                prev_close_row = data.close.iloc[idx - 1]

        def _prev(sym: str) -> float | None:
            if data.pre_close is not None and date in data.pre_close.index and sym in data.pre_close.columns and pd.notna(data.pre_close.loc[date, sym]):
                return float(data.pre_close.loc[date, sym])
            if sym in prev_close_row.index and pd.notna(prev_close_row[sym]):
                return float(prev_close_row[sym])
            return None

        # Execute pending selection at today's open.
        if pending_candidates is not None:
            # Resolve the intended basket against today's executable prices
            # before touching an existing portfolio.  A sparse/invalid score
            # panel must hold the prior portfolio, never create a one-stock
            # pseudo-rebalance or an accidental liquidation.
            pre_position_value = sum(
                _position_value(
                    shares[sym],
                    float(open_row[sym]) if sym in open_row.index and pd.notna(open_row[sym])
                    else (_prev(sym) or 0.0),
                )
                for sym in list(shares)
            )
            target_count = int(getattr(strategy, "top_n", len(pending_candidates)))
            allocation = float(allocations.get(pending_signal_date or "", 1.0))
            allocation = min(max(allocation, 0.0), 1.0)
            # An explicit zero allocation is a deliberate risk-gate exit;
            # unlike an invalid score, it is allowed to move the sleeve cash.
            if allocation <= 0.0:
                selected = []
            elif live_sizing:
                selected = affordable_equal_weight_targets(
                    pending_candidates,
                    open_row,
                    (cash + pre_position_value + book.valuation(date)["distribution_value"]) * allocation,
                    target_count,
                    cost,
                )
            else:
                selected = list(pending_candidates)[:target_count]
            if selected or allocation <= 0.0:
                target_set = set(selected)

                # 1) Sell positions that are no longer in the target set.
                for sym in list(shares):
                    if sym in target_set or sym not in open_row.index or pd.isna(open_row[sym]):
                        if sym not in target_set:
                            _record_skip(date, sym, "停牌或开盘价缺失")
                        continue
                    open_price = float(open_row[sym])
                    cap = _fill_cap(sym, date)
                    n = shares[sym] if cap is None else min(shares[sym], cap)
                    if not valid_sell_quantity(sym, n, shares[sym]):
                        _record_skip(date, sym, "成交量约束下不足一手")
                        continue
                    cash, shares[sym], trade, _ = sell_exact(
                        cash, shares[sym], open_price, date, cost, sym, n,
                        prev_close=_prev(sym), avg_cost=avg_cost.get(sym, 0.0),
                    )
                    if trade:
                        trades.append(trade.to_dict())
                    else:
                        _record_skip(date, sym, "卖出未成交；保留原持仓")
                    if shares.get(sym, 0) == 0:
                        shares.pop(sym, None)
                        avg_cost.pop(sym, None)

                # 2) Equal-weight the *feasible* names, matching the legacy
                # live rebalancer after its affordability pass.
                position_value = sum(
                    _position_value(
                        shares[sym],
                        float(open_row[sym]) if sym in open_row.index and pd.notna(open_row[sym])
                        else (_prev(sym) or 0.0),
                    )
                    for sym in list(shares)
                )
                total_value = cash + position_value + book.valuation(date)["distribution_value"]
                if not selected:
                    target_value = 0.0
                elif live_sizing:
                    target_value = total_value * allocation / len(selected)
                else:
                    target_value = total_value * allocation / target_count
                for sym in selected:
                    if sym not in open_row.index or pd.isna(open_row[sym]):
                        _record_skip(date, sym, "停牌或开盘价缺失")
                        continue
                    open_price = float(open_row[sym])
                    cur = shares.get(sym, 0)
                    if live_sizing:
                        target_shares = target_shares_for_budget(
                            target_value, open_price, sym, cost,
                        )
                    else:
                        target_shares = (
                            int(target_value / open_price / quantity_step(sym)) * quantity_step(sym)
                        )
                    cap = _fill_cap(sym, date)
                    if cap is not None:
                        desired = target_shares - cur
                        target_shares = (
                            min(target_shares, cur + cap)
                            if desired > 0 else max(target_shares, cur - cap)
                        )
                        if abs(target_shares - cur) < LOT_SIZE and abs(desired) >= LOT_SIZE:
                            _record_skip(date, sym, "成交量约束下不足一手")
                            continue
                    diff = target_shares - cur
                    if diff < 0 and valid_sell_quantity(sym, -diff, cur):
                        cash, shares[sym], trade, _ = sell_exact(
                            cash, shares.get(sym, 0), open_price, date, cost, sym, -diff,
                            prev_close=_prev(sym), avg_cost=avg_cost.get(sym, 0.0),
                        )
                        if trade:
                            trades.append(trade.to_dict())
                        else:
                            _record_skip(date, sym, "减仓未成交；保留原持仓")
                        if shares.get(sym, 0) == 0:
                            shares.pop(sym, None)
                            avg_cost.pop(sym, None)
                    elif diff >= minimum_order_quantity(sym):
                        reserved_tax = book.tax_reserve(date, shares)
                        available_cash = max(0.0, cash - reserved_tax)
                        available_cash, shares[sym], trade, _ = buy_exact(
                            available_cash, shares.get(sym, 0), open_price, date, cost, sym, diff,
                            prev_close=_prev(sym),
                        )
                        if trade:
                            cash = available_cash + reserved_tax
                            prev_shares = shares[sym] - trade.shares
                            avg_cost[sym] = (
                                (avg_cost.get(sym, 0.0) * prev_shares + trade.amount + trade.fee) / shares[sym]
                                if shares[sym] > 0 else 0.0
                            )
                            trades.append(trade.to_dict())
                        elif shares.get(sym, 0) == 0:
                            shares.pop(sym, None)
            pending_candidates = None
            pending_signal_date = None

        # Mark receivables and outstanding dividend tax separately from holdings.
        for action in book.actions:
            if action.ex_date == date and shares.get(action.symbol,0) and (action.symbol not in data.close.columns or date not in data.close.index or pd.isna(data.close.loc[date,action.symbol])):
                raise RuntimeError('held ex-date instrument needs a verified post-action valuation')
        cash += book.close(date, shares)
        components = book.valuation(date)
        distribution_nav.append({"date": date, **components})
        position_value_close = sum(
            _position_value(shares[sym], float(close_row[sym]))
            for sym in list(shares)
            if sym in close_row.index and pd.notna(close_row[sym])
        )
        equity_rows.append(
            {
                "date": date,
                "cash": cash,
                "position_value": position_value_close,
                "total_value": cash + position_value_close + components["distribution_value"],
            }
        )

        # Generate next period selection on rebalance dates.
        if date in selected_rebalance_dates or date in (frozen_signals or {}):
            scores = data.factor_scores.loc[date].dropna() if date in data.factor_scores.index else pd.Series(dtype=float)
            used_fallback = False
            if scores.empty and data.fallback_scores is not None and date in data.fallback_scores.index:
                fb = data.fallback_scores.loc[date].dropna()
                if not fb.empty:
                    scores = fb
                    used_fallback = True
            if data.universe is not None and date in data.universe.index:
                universe = list(data.universe.loc[date][data.universe.loc[date]].index)
            else:
                universe = list(scores.index)
            context = CrossSectionContext(
                date=date,
                prices=close_row,
                universe=universe,
                factor_scores=scores,
            )
            desired = int(getattr(strategy, "top_n", 0) or 0)
            candidate_count = max(desired * 5, desired)
            if hasattr(strategy, "rank"):
                pending_candidates = strategy.rank(context, limit=candidate_count)
            else:
                pending_candidates = strategy.select(context)
            if date in (frozen_signals or {}):
                decision = frozen_signals[date]
                pending_candidates = list(decision['candidates'])
                allocations[date] = float(decision['allocation'])
            pending_signal_date = date
            decisions.append({'date': date, 'candidates': list(pending_candidates),
                              'allocation': float(allocations.get(date, 1.0))})
            if not used_fallback:
                signals.append({
                    "date": date,
                    "signal": json.dumps(
                        pending_candidates[:desired] if desired else pending_candidates,
                        ensure_ascii=False, separators=(",", ":"),
                    ),
                    "weight": 1.0,
                })

    equity = pd.DataFrame(equity_rows)
    metrics = portfolio_metrics(equity, initial_cash, len(trades))
    return {
        "equity": equity,
        "signals": signals,
        "trades": trades,
        "metrics": metrics,
        "skips": skips,
        "cash": cash,
        "shares": dict(shares),
        "avg_cost": dict(avg_cost),
        "distribution_events": book.events,
        "distribution_nav": distribution_nav,
        "distribution_state": book.state(),
        "decisions": decisions,
    }
