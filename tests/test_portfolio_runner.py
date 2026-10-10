from __future__ import annotations

import datetime as dt

import pandas as pd

from qsim.allocation.policy import RegimeAllocationPolicy
from qsim.allocation.regime import SimpleRegimeDetector
from qsim.engine.costs import CostModel
from qsim.engine.portfolio_runner import Sleeve, run_portfolio_backtest
from qsim.strategies.builtin.buy_hold import BuyHoldStrategy
from qsim.strategies.builtin.hold import HoldStrategy


class StubDataSource:
    def __init__(self, bars: dict[str, pd.DataFrame]) -> None:
        self.bars = bars

    def get_bars(self, symbol, start=None, end=None):
        df = self.bars[symbol].copy()
        if start:
            df = df[df["date"] >= pd.to_datetime(start).date()]
        if end:
            df = df[df["date"] <= pd.to_datetime(end).date()]
        return df.reset_index(drop=True)


def _frame(days, price=10.0):
    return pd.DataFrame(
        {
            "date": [dt.date(2026, 1, d) for d in days],
            "open": [price] * len(days),
            "high": [price + 0.1] * len(days),
            "low": [price - 0.1] * len(days),
            "close": [price] * len(days),
            "volume": [0.0] * len(days),
            "amount": [0.0] * len(days),
        }
    )


def test_portfolio_runner_runs() -> None:
    days = [1, 2, 3, 4, 5]
    data = {
        "A": _frame(days, 10.0),
        "B": _frame(days, 5.0),
    }
    source = StubDataSource(data)
    policy = RegimeAllocationPolicy(
        {
            "up": {"buy_hold": 0.6, "hold": 0.4},
            "range": {"buy_hold": 0.5, "hold": 0.5},
            "down": {"buy_hold": 0.0, "hold": 1.0},
        }
    )
    sleeves = [
        Sleeve("buy_hold", "A", BuyHoldStrategy()),
        Sleeve("hold", "B", HoldStrategy()),
    ]
    result = run_portfolio_backtest(
        sleeves=sleeves,
        policy=policy,
        data_source=source,
        regime_detector=SimpleRegimeDetector(),
        initial_cash=100000.0,
        cost=CostModel(),
        market_symbol="A",
        assumed_alpha=0.02,
    )
    assert len(result["equity"]) == len(days)
    assert result["metrics"]["final_value"] > 0
    assert "trades" in result


def test_portfolio_runner_handles_suspended_days() -> None:
    source = StubDataSource(
        {
            "A": _frame([1, 2, 3, 4, 5], 10.0),
            "B": _frame([1, 2, 4, 5], 5.0),
        }
    )
    policy = RegimeAllocationPolicy(
        {
            "up": {"buy_hold": 0.6, "hold": 0.4},
            "range": {"buy_hold": 0.5, "hold": 0.5},
            "down": {"buy_hold": 0.0, "hold": 1.0},
        }
    )
    sleeves = [
        Sleeve("buy_hold", "A", BuyHoldStrategy()),
        Sleeve("hold", "B", HoldStrategy()),
    ]
    result = run_portfolio_backtest(
        sleeves=sleeves,
        policy=policy,
        data_source=source,
        regime_detector=SimpleRegimeDetector(),
        initial_cash=100000.0,
        cost=CostModel(commission_rate=0.0, min_commission=0.0, slippage=0.0),
        market_symbol="A",
    )
    equity = result["equity"]
    assert len(equity) == 5
    assert equity["total_value"].notna().all()
    assert result["metrics"]["num_trades"] == len(result["trades"])
