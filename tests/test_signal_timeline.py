from __future__ import annotations

import pandas as pd
import pandas.testing as pdt

from qsim.allocation.policy import RegimeAllocationPolicy
from qsim.allocation.regime import SimpleRegimeDetector
from qsim.engine.combined_runner import (
    CrossSleeve,
    RotationSleeve,
    precompute_signal_timeline,
    run_combined_portfolio_backtest,
)
from qsim.engine.costs import CostModel
from qsim.engine.portfolio_runner import Sleeve
from qsim.strategies.base import Strategy
from qsim.strategies.cross_sectional import TopNScoreStrategy
from qsim.strategies.rotation import MomentumRotation


class StubDataSource:
    def __init__(self, bars: dict[str, pd.DataFrame]) -> None:
        self.bars = bars

    def get_bars(self, symbol, start=None, end=None):
        return self.bars[symbol].copy()


class CountingStrategy(Strategy):
    """Stateful stand-in: signal depends on the generate-call sequence."""

    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def reset(self) -> None:
        self.calls = 0

    def generate(self, df: pd.DataFrame):
        self.calls += 1
        if self.calls < 3:
            return "HOLD"
        if self.calls < 40:
            return ("BUY", 1.0)
        return "SELL"


def _frame(dates: list[str], base: float) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {
            "date": dates,
            "open": [base + (i % 7) * 0.05 for i in range(n)],
            "high": [base + 0.2] * n,
            "low": [base - 0.2] * n,
            "close": [base + ((i + 1) % 7) * 0.05 for i in range(n)],
            "volume": [1.0] * n,
            "amount": [1.0] * n,
        }
    )


def _setup():
    dates = pd.date_range("2026-01-01", periods=70).strftime("%Y%m%d").tolist()
    source = StubDataSource(
        {
            "ETF": _frame(dates, 10.0),
            "R1": _frame(dates, 20.0),
            "R2": _frame(dates, 30.0),
            "MKT": _frame(dates, 15.0),
        }
    )
    stock_open = pd.DataFrame(
        {
            "S1": [5.0 + (i % 5) * 0.02 for i in range(len(dates))],
            "S2": [50.0 + (i % 3) * 0.1 for i in range(len(dates))],
        },
        index=dates,
    )
    stock_close = stock_open * 1.01
    scores = pd.DataFrame(
        {"S1": [1.0] * len(dates), "S2": [2.0] * len(dates)},
        index=dates,
    )
    single_sleeves = [Sleeve("counting", "ETF", CountingStrategy())]
    cross_sleeves = [
        CrossSleeve(
            "cross",
            TopNScoreStrategy(top_n=1),
            lambda d: scores.loc[d] if d in scores.index else pd.Series(dtype=float),
        )
    ]
    rotation_sleeves = [
        RotationSleeve("rotation", MomentumRotation(lookback=5), ("R1", "R2"))
    ]
    policy_a = RegimeAllocationPolicy(
        {
            "up": {"counting": 0.4, "cross": 0.4, "rotation": 0.2},
            "range": {"counting": 0.4, "cross": 0.4, "rotation": 0.2},
            "down": {"counting": 0.8, "cross": 0.0, "rotation": 0.2},
        }
    )
    policy_b = RegimeAllocationPolicy(
        {
            "up": {"counting": 0.1, "cross": 0.7, "rotation": 0.2},
            "range": {"counting": 0.1, "cross": 0.7, "rotation": 0.2},
            "down": {"counting": 0.0, "cross": 0.9, "rotation": 0.1},
        }
    )
    return (
        dates,
        source,
        stock_open,
        stock_close,
        single_sleeves,
        cross_sleeves,
        rotation_sleeves,
        policy_a,
        policy_b,
    )


def _run(policy, source, stock_open, stock_close, single_sleeves, cross_sleeves, rotation_sleeves, timeline=None):
    kwargs = {}
    if timeline is not None:
        kwargs["signal_timeline"] = timeline
    return run_combined_portfolio_backtest(
        single_sleeves=single_sleeves,
        cross_sleeves=cross_sleeves,
        rotation_sleeves=rotation_sleeves,
        policy=policy,
        data_source=source,
        stock_open=stock_open,
        stock_close=stock_close,
        regime_detector=SimpleRegimeDetector(),
        initial_cash=100000.0,
        cost=CostModel(),
        market_symbol="MKT",
        **kwargs,
    )


def test_timeline_replay_matches_live_signals() -> None:
    (
        dates,
        source,
        stock_open,
        stock_close,
        single_sleeves,
        cross_sleeves,
        rotation_sleeves,
        policy_a,
        policy_b,
    ) = _setup()
    live_a = _run(policy_a, source, stock_open, stock_close, single_sleeves, cross_sleeves, rotation_sleeves)
    live_b = _run(policy_b, source, stock_open, stock_close, single_sleeves, cross_sleeves, rotation_sleeves)
    strategy = single_sleeves[0].strategy
    assert strategy.calls == len(dates)

    timeline = precompute_signal_timeline(
        single_sleeves=single_sleeves,
        cross_sleeves=cross_sleeves,
        rotation_sleeves=rotation_sleeves,
        data_source=source,
        stock_open=stock_open,
        stock_close=stock_close,
        regime_detector=SimpleRegimeDetector(),
        market_symbol="MKT",
    )
    replay_a = _run(policy_a, source, stock_open, stock_close, single_sleeves, cross_sleeves, rotation_sleeves, timeline)
    replay_b = _run(policy_b, source, stock_open, stock_close, single_sleeves, cross_sleeves, rotation_sleeves, timeline)

    for live, replay in ((live_a, replay_a), (live_b, replay_b)):
        pdt.assert_frame_equal(live["equity"], replay["equity"])
        assert live["trades"] == replay["trades"]
        assert live["skips"] == replay["skips"]
        assert live["metrics"] == replay["metrics"]

    assert live_a["equity"]["total_value"].iloc[-1] != live_b["equity"]["total_value"].iloc[-1]
    assert len(live_a["trades"]) > 0
