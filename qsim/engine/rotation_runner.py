"""Multi-asset rotation backtest runner."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from qsim.engine.cash_distributions import CashDistributionBook
from qsim.engine.costs import CostModel
from qsim.engine.matcher import buy, sell
from qsim.engine.metrics import portfolio_metrics
from qsim.strategies.rotation import RotationStrategy


def _date_str(value) -> str:
    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")
    return str(value).replace("-", "")


def run_rotation_backtest(
    data: dict[str, pd.DataFrame],
    strategy: RotationStrategy,
    initial_cash: float,
    cost: CostModel,
    start: str | None = None,
    end: str | None = None,
    distributions=(), frozen_signals: dict | None = None,
    strict_distributions: bool = False, warmup: bool = False,
    rounding_policy: str = "require_evidence",
) -> dict:
    data = {sym: df.copy().reset_index(drop=True) for sym, df in data.items()}
    if start and not warmup:
        data = {sym: df[df["date"].astype(str).str.replace("-", "") >= start].reset_index(drop=True) for sym, df in data.items()}
    if end:
        data = {sym: df[df["date"].astype(str).str.replace("-", "") <= end].reset_index(drop=True) for sym, df in data.items()}

    indexed = {sym: df.set_index(df["date"].astype(str).str.replace("-", "")) for sym, df in data.items()}
    all_dates = sorted(set().union(*[set(df["date"].astype(str).str.replace("-", "")) for df in data.values()]))
    if start:
        all_dates = [d for d in all_dates if d >= start]
    if not all_dates:
        raise ValueError("no dates in rotation backtest")

    if hasattr(strategy, "reset"):
        strategy.reset()

    cash = initial_cash
    shares = 0
    cur = "CASH"
    avg_cost = 0.0
    pending = "HOLD"
    trades: list[dict] = []
    skips: list[dict] = []
    signals: list[dict] = []
    equity_rows: list[dict] = []
    hist: dict[str, pd.DataFrame] = {}

    book = CashDistributionBook(distributions, strict_rounding=strict_distributions, rounding_policy=rounding_policy)
    distribution_nav = []
    for date in all_dates:
        cash += book.open(date)
        for sym, df in data.items():
            idx = df["date"].astype(str).str.replace("-", "").searchsorted(date, side="right")
            hist[sym] = df.iloc[:idx]

        def executable(sym):
            if sym not in indexed or date not in indexed[sym].index:
                return False
            return not strict_distributions or ('volume' in indexed[sym].columns and float(indexed[sym].loc[date,'volume']) > 0)

        # Execute pending target at today's open.
        if pending not in (None, "", "HOLD"):
            if pending == "CASH":
                if cur != "CASH" and executable(cur):
                    price = float(indexed[cur].loc[date, "open"])
                    cash, shares, trade, reason = sell(
                        cash, shares, price, date, cost, cur,
                        prev_close=_prev_close(hist, cur),
                        avg_cost=0.0,
                    )
                    if trade:
                        trades.append(trade.to_dict())
                        avg_cost = 0.0
                    if shares == 0:
                        cur = "CASH"
                elif shares == 0:
                    cur = "CASH"
            elif pending != cur:
                if cur != "CASH" and executable(cur):
                    price = float(indexed[cur].loc[date, "open"])
                    cash, shares, trade, reason = sell(
                        cash, shares, price, date, cost, cur,
                        prev_close=_prev_close(hist, cur),
                        avg_cost=0.0,
                    )
                    if trade:
                        trades.append(trade.to_dict())
                        avg_cost = 0.0
                    if shares == 0:
                        cur = "CASH"
                if shares == 0 and executable(pending):
                    price = float(indexed[pending].loc[date, "open"])
                    cash, shares, trade, reason = buy(
                        cash, shares, price, date, cost, pending,
                        prev_close=_prev_close(hist, pending),
                    )
                    if trade:
                        trades.append(trade.to_dict())
                        avg_cost = (trade.amount + trade.fee) / trade.shares
                        cur = pending
            pending = "HOLD"

        # Mark equity at close.
        if cur != "CASH" and cur in hist and not hist[cur].empty:
            pos_value = shares * float(hist[cur]["close"].iloc[-1])
        else:
            pos_value = 0.0
        cash += book.close(date, {cur: shares} if cur != "CASH" else {})
        components = book.valuation(date)
        distribution_nav.append({"date": date, **components})
        equity_rows.append({"date": date, "cash": cash, "position_value": pos_value, "total_value": cash + pos_value + components["distribution_value"]})

        # Generate tomorrow's target.
        generated = strategy.generate(hist)
        pending = (frozen_signals or {}).get(date, {}).get("signal", generated)
        signals.append({"date": date, "signal": str(pending), "weight": 1.0})

    equity = pd.DataFrame(equity_rows)
    metrics = portfolio_metrics(equity, initial_cash, len(trades))
    return {
        "signals": signals,
        "equity": equity,
        "trades": trades,
        "metrics": metrics,
        "skips": skips,
        "cash": cash,
        "shares": shares,
        "symbol": None if cur == "CASH" else cur,
        "avg_cost": avg_cost,
        "distribution_events": book.events, "distribution_nav": distribution_nav,
        "distribution_state": book.state(), "decisions": signals,
    }


def _prev_close(hist: dict[str, pd.DataFrame], sym: str) -> float | None:
    df = hist.get(sym)
    if df is not None and not df.empty and "pre_close" in df.columns:
        return float(df["pre_close"].iloc[-1])
    if df is None or len(df) < 2:
        return None
    return float(df["close"].iloc[-2])
