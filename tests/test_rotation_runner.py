from __future__ import annotations

import datetime as dt

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.rotation_runner import run_rotation_backtest
from qsim.strategies.rotation import MomentumRotation


def _bars(days, prices):
    return pd.DataFrame(
        {
            "date": [dt.date(2026, 1, d) for d in days],
            "open": prices,
            "high": [p + 0.1 for p in prices],
            "low": [p - 0.1 for p in prices],
            "close": prices,
            "volume": [0.0] * len(days),
            "amount": [0.0] * len(days),
        }
    )


def test_rotation_runner_runs() -> None:
    days = [1, 2, 3, 4, 5]
    data = {
        "A": _bars(days, [10.0, 11.0, 12.0, 13.0, 14.0]),
        "B": _bars(days, [10.0, 10.2, 10.4, 10.6, 10.8]),
    }
    result = run_rotation_backtest(
        data=data,
        strategy=MomentumRotation(lookback=1),
        initial_cash=100000.0,
        cost=CostModel(),
    )
    assert len(result["equity"]) == len(days)
    assert result["metrics"]["final_value"] > 0
