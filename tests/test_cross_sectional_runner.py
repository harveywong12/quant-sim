from __future__ import annotations

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.cross_sectional_runner import (
    CrossSectionalData,
    target_shares_for_budget,
    monthly_rebalance_dates,
    run_cross_sectional_backtest,
)
from qsim.strategies.cross_sectional import TopNScoreStrategy


def test_monthly_rebalance_dates() -> None:
    dates = ["20260105", "20260120", "20260203", "20260227", "20260310"]
    assert monthly_rebalance_dates(dates) == ["20260105", "20260203", "20260310"]


def test_cross_sectional_backtest_runs() -> None:
    dates = ["20260105", "20260120", "20260203", "20260227", "20260310"]
    stocks = ["A", "B", "C"]
    scores = pd.DataFrame(
        {
            "A": [1.0, 2.0, 1.0, 2.0, 1.0],
            "B": [2.0, 1.0, 2.0, 1.0, 2.0],
            "C": [0.5, 0.5, 0.5, 0.5, 0.5],
        },
        index=dates,
    )
    open_prices = pd.DataFrame(
        {
            "A": [10.0] * len(dates),
            "B": [20.0] * len(dates),
            "C": [30.0] * len(dates),
        },
        index=dates,
    )
    close_prices = open_prices * 1.01
    data = CrossSectionalData(
        dates=dates,
        factor_scores=scores,
        open=open_prices,
        close=close_prices,
    )
    result = run_cross_sectional_backtest(
        data=data,
        strategy=TopNScoreStrategy(top_n=2),
        initial_cash=100000.0,
        cost=CostModel(),
        rebalance="monthly",
    )
    assert len(result["equity"]) == len(dates)
    assert result["metrics"]["final_value"] > 0
    assert len(result["trades"]) > 0


def test_allocation_gate_is_applied_before_affordability_selection() -> None:
    dates = ["20260901", "20260902"]
    stocks = ["EXPENSIVE", "CHEAP1", "CHEAP2"]
    scores = pd.DataFrame(
        [[3.0, 2.0, 1.0]], index=[dates[0]], columns=stocks,
    )
    opens = pd.DataFrame(
        [[80.0, 10.0, 10.0], [80.0, 10.0, 10.0]], index=dates, columns=stocks,
    )
    result = run_cross_sectional_backtest(
        data=CrossSectionalData(dates, scores, opens, opens),
        strategy=TopNScoreStrategy(top_n=2),
        initial_cash=100000.0,
        cost=CostModel(round_fee=False),
        rebalance_dates=["20260901"],
        allocation_by_signal_date={"20260901": 0.10},
        sizing="live_affordable",
    )

    bought = {trade["symbol"] for trade in result["trades"] if trade["side"] == "BUY"}
    assert bought == {"CHEAP1", "CHEAP2"}
    assert "EXPENSIVE" not in result["shares"]


def test_sizing_defaults_to_parity_even_with_allocation() -> None:
    dates = ["20260901", "20260902"]
    stocks = ["EXPENSIVE", "CHEAP1"]
    scores = pd.DataFrame(
        [[3.0, 2.0]], index=[dates[0]], columns=stocks,
    )
    opens = pd.DataFrame(
        [[80.0, 10.0], [80.0, 10.0]], index=dates, columns=stocks,
    )
    result = run_cross_sectional_backtest(
        data=CrossSectionalData(dates, scores, opens, opens),
        strategy=TopNScoreStrategy(top_n=1),
        initial_cash=100000.0,
        cost=CostModel(round_fee=False),
        rebalance_dates=["20260901"],
        allocation_by_signal_date={"20260901": 1.0},
    )
    assert set(result["shares"]) == {"EXPENSIVE"}


def test_unknown_sizing_policy_rejected() -> None:
    dates = ["20260901", "20260902"]
    scores = pd.DataFrame([[1.0]], index=[dates[0]], columns=["A"])
    opens = pd.DataFrame([[10.0], [10.0]], index=dates, columns=["A"])
    try:
        run_cross_sectional_backtest(
            data=CrossSectionalData(dates, scores, opens, opens),
            strategy=TopNScoreStrategy(top_n=1),
            initial_cash=100000.0,
            cost=CostModel(),
            rebalance_dates=["20260901"],
            sizing="unknown",
        )
    except ValueError:
        return
    raise AssertionError("unknown sizing policy should raise")


def test_target_shares_reserves_fill_slippage_and_fee() -> None:
    cost = CostModel(round_fee=False)
    assert target_shares_for_budget(1000.0, 10.0, "600001.SH", cost) == 0
    assert target_shares_for_budget(1100.0, 10.0, "600001.SH", cost) == 100
