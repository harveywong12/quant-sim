"""Strategy executor shared by backtest and simulation.

The core rule is T+1: a signal produced at day ``d`` close is executed at day
``d+1`` open.  Backtest and paper trading run this exact loop; ``warmup=True``
lets a paper replay generate signals from full history while only recording
the explicit window, and ``sink`` persists executions without altering them.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from qsim.engine.cash_distributions import CashDistributionBook
from qsim.engine.account import Account
from qsim.engine.costs import CostModel
from qsim.engine.execution import ExecutionPolicy
from qsim.engine.ledger import Ledger
from qsim.engine.matcher import adjust_position
from qsim.engine.metrics import portfolio_metrics


def _date_str(value) -> str:
    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")
    return str(value).replace("-", "")


class DailyExecutionSink:
    """Persistence hook for the executor; the executor never depends on one."""

    def on_trade(self, date: str, trade, reason: str | None) -> None:
        raise NotImplementedError

    def on_snapshot(self, date: str, cash: float, position_value: float,
                    total_value: float) -> None:
        raise NotImplementedError

    def on_skip(self, date: str, side: str, reason: str) -> None:
        pass


def run_daily_executor(
    df: pd.DataFrame,
    strategy,
    initial_cash: float,
    cost: CostModel,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    max_pos_w: float = 0.0,
    warmup: bool = False,
    sink: DailyExecutionSink | None = None,
    policy: ExecutionPolicy | None = None,
    distributions=(), frozen_signals: dict | None = None,
    freeze_last_signal: bool = False, strict_distributions: bool = False,
    rounding_policy: str = "require_evidence",
) -> dict:
    """Run the T+1 daily loop over ``[start, end]`` (default: full range).

    ``warmup=False`` (backtest mode) slices the frame first, so strategies
    only see in-window history.  ``warmup=True`` (paper mode) iterates the
    window but lets ``generate`` see the full history up to each day.
    """
    full = df.copy().reset_index(drop=True)
    bounds = pd.to_datetime(full["date"])
    mask = np.ones(len(full), dtype=bool)
    if start:
        mask &= (bounds >= pd.to_datetime(start)).to_numpy()
    if end:
        mask &= (bounds <= pd.to_datetime(end)).to_numpy()
    window = np.flatnonzero(mask)
    if len(window) == 0:
        raise ValueError("回测区间内无数据")

    account = Account(symbol=symbol, cash=initial_cash)
    ledger = Ledger(initial_cash)
    skips: list[dict] = []
    signals: list[dict] = []

    if hasattr(strategy, "reset"):
        strategy.reset()

    book = CashDistributionBook(distributions, strict_rounding=strict_distributions, rounding_policy=rounding_policy)
    distribution_nav = []
    history_from = 0 if warmup else int(window[0])
    for j, idx in enumerate(window):
        idx = int(idx)
        bar = full.iloc[idx]
        date = _date_str(bar["date"])
        account.cash += book.open(date)
        open_price = float(bar["open"])
        close_price = float(bar["close"])
        prev_close = float(bar["pre_close"]) if "pre_close" in full.columns else (float(full.iloc[idx - 1]["close"]) if j > 0 else None)

        if j > 0 and account.pending_signal != "HOLD":
            volume = float(bar["volume"]) if "volume" in full.columns else None
            volume_cap = policy.fill_cap(volume) if policy else None
            if strict_distributions and (volume is None or volume <= 0):
                volume_cap = 0
            total_value = account.total_value(open_price)
            cash, shares, trade, reason = adjust_position(
                account.cash,
                account.shares,
                open_price,
                date,
                cost,
                account.pending_weight,
                total_value,
                symbol,
                prev_close=prev_close,
                max_pos_w=max_pos_w,
                avg_cost=account.avg_cost,
                volume_cap=volume_cap,
            )
            if trade:
                if trade.side == "BUY":
                    prev_shares = shares - trade.shares
                    account.avg_cost = (
                        (account.avg_cost * prev_shares + trade.amount + trade.fee) / shares
                        if shares > 0
                        else 0.0
                    )
                ledger.record(date, trade.side, trade.to_dict())
                if sink:
                    sink.on_trade(date, trade, reason)
            elif reason:
                skips.append({"date": date, "symbol": symbol, "side": account.pending_signal, "reason": reason})
                if sink:
                    sink.on_skip(date, account.pending_signal, reason)
            account.cash = cash
            account.shares = shares
            account.pending_signal = "HOLD"
            account.pending_weight = 0.0
            account.pending_date = None

        account.cash += book.close(date, {symbol: account.shares})
        components = book.valuation(date)
        distribution_nav.append({"date": date, **components})
        ledger.record(
            date,
            "SNAPSHOT",
            {
                "date": date,
                "cash": account.cash,
                "position_value": account.position_value(close_price),
                "total_value": account.total_value(close_price) + components["distribution_value"],
            },
        )
        if sink:
            sink.on_snapshot(
                date,
                account.cash,
                account.position_value(close_price),
                account.total_value(close_price) + components["distribution_value"],
            )

        if j < len(window) - 1 or freeze_last_signal:
            history = full.iloc[history_from: idx + 1]
            if hasattr(strategy, "on_bar"):
                strategy.on_bar(history)
            signal = strategy.generate(history)
            if isinstance(signal, tuple):
                raw_signal, weight = signal
            else:
                raw_signal = signal
                weight = 0.0 if raw_signal == "SELL" else 1.0
            if date in (frozen_signals or {}):
                raw_signal = frozen_signals[date]["signal"]
                weight = float(frozen_signals[date]["weight"])
            signals.append({
                "date": date,
                "signal": str(raw_signal),
                "weight": float(weight),
            })
            account.pending_signal = raw_signal
            account.pending_weight = weight
            account.pending_date = date

    equity = pd.DataFrame(
        ledger.snapshots(),
        columns=["date", "cash", "position_value", "total_value"],
    )
    trades = ledger.trades()
    metrics = portfolio_metrics(equity, initial_cash, len(trades))
    return {
        "signals": signals,
        "equity": equity,
        "trades": trades,
        "metrics": metrics,
        "skips": skips,
        "cash": account.cash,
        "shares": account.shares,
        "avg_cost": account.avg_cost,
        "distribution_events": book.events, "distribution_nav": distribution_nav,
        "distribution_state": book.state(), "decisions": signals,
    }


def run_backtest(
    df: pd.DataFrame,
    strategy,
    initial_cash: float,
    cost: CostModel,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    max_pos_w: float = 0.0,
) -> dict:
    return run_daily_executor(
        df, strategy, initial_cash, cost, symbol,
        start=start, end=end, max_pos_w=max_pos_w,
    )
