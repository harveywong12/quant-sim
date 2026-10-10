from __future__ import annotations

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.cross_sectional_runner import CrossSectionalData, run_cross_sectional_backtest
from qsim.strategies.cross_sectional import MultiFactorStrategy


def test_cross_sectional_num_trades_matches_trade_events() -> None:
    dates = ["20260803", "20260804", "20260805"]
    symbols = ["600000.SH", "600001.SH"]
    close = pd.DataFrame(
        [[1.0, 2.0], [1.05, 2.05], [1.1, 2.1]],
        index=dates,
        columns=symbols,
    )
    data = CrossSectionalData(
        dates=dates,
        factor_scores=pd.DataFrame([[2.0, 1.0]], index=[dates[0]], columns=symbols),
        open=close,
        close=close,
    )

    result = run_cross_sectional_backtest(
        data, MultiFactorStrategy(top_n=1), 1000.0,
        CostModel(round_fee=False), rebalance_dates=[dates[0]],
    )

    assert result["metrics"]["num_trades"] == len(result["trades"])
    assert result["metrics"]["num_trades"] == 1
