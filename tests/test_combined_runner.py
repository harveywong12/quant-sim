from __future__ import annotations

import datetime as dt

import pandas as pd

from qsim.allocation.policy import RegimeAllocationPolicy
from qsim.allocation.regime import SimpleRegimeDetector
from qsim.engine.costs import CostModel
from qsim.engine.combined_runner import (
    CrossSleeve,
    RotationSleeve,
    run_combined_portfolio_backtest,
)
from qsim.engine.portfolio_runner import Sleeve
from qsim.strategies.cross_sectional import TopNScoreStrategy
from qsim.strategies.builtin.hold import HoldStrategy
from qsim.strategies.rotation import MomentumRotation


class StubDataSource:
    def __init__(self, bars: dict[str, pd.DataFrame]) -> None:
        self.bars = bars

    def get_bars(self, symbol, start=None, end=None):
        return self.bars[symbol].copy()


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


def test_combined_runner_runs() -> None:
    days = [1, 2, 3, 4, 5]
    dates = [dt.date(2026, 1, d).strftime("%Y%m%d") for d in days]
    source = StubDataSource({"ETF": _frame(days)})
    stock_open = pd.DataFrame(
        {"S1": [10.0] * len(days), "S2": [20.0] * len(days)},
        index=dates,
    )
    stock_close = stock_open * 1.01

    policy = RegimeAllocationPolicy(
        {
            "up": {"hold": 0.4, "cross": 0.4, "rotation": 0.2},
            "range": {"hold": 0.4, "cross": 0.4, "rotation": 0.2},
            "down": {"hold": 0.8, "cross": 0.0, "rotation": 0.2},
        }
    )
    scores = pd.DataFrame(
        {"S1": [1.0] * len(days), "S2": [2.0] * len(days)},
        index=dates,
    )

    result = run_combined_portfolio_backtest(
        single_sleeves=[Sleeve("hold", "ETF", HoldStrategy())],
        cross_sleeves=[
            CrossSleeve(
                "cross",
                TopNScoreStrategy(top_n=1),
                lambda d: scores.loc[d] if d in scores.index else pd.Series(dtype=float),
            )
        ],
        rotation_sleeves=[
            RotationSleeve("rotation", MomentumRotation(lookback=1), ("ETF",))
        ],
        policy=policy,
        data_source=source,
        stock_open=stock_open,
        stock_close=stock_close,
        regime_detector=SimpleRegimeDetector(),
        initial_cash=100000.0,
        cost=CostModel(),
        market_symbol="ETF",
    )
    assert len(result["equity"]) == len(days)
    assert result["metrics"]["final_value"] > 0
