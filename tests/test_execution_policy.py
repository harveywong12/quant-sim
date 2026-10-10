from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qsim.engine.costs import CostModel
from qsim.engine.cross_sectional_runner import (
    CrossSectionalData,
    run_cross_sectional_backtest,
)
from qsim.engine.execution import ExecutionPolicy
from qsim.engine.matcher import buy
from qsim.engine.runner import run_daily_executor
from qsim.strategies.cross_sectional import TopNScoreStrategy
from qsim.strategies.base import Strategy


class _AlwaysBuy(Strategy):
    def generate(self, df: pd.DataFrame):
        return "BUY"


def _daily_bars(volume: float) -> pd.DataFrame:
    dates = pd.date_range("2026-01-02", periods=80, freq="B")
    close = np.full(len(dates), 10.0)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.full(len(dates), float(volume)),
        }
    )


def test_policy_defaults_are_uncapped() -> None:
    policy = ExecutionPolicy()
    assert policy.fill_cap(1e8) is None
    assert policy.cap_shares(500, 1e8) == 500


def test_policy_validates_participation() -> None:
    with pytest.raises(ValueError):
        ExecutionPolicy(volume_participation=1.5)


def test_fill_cap_lot_rounds_down() -> None:
    policy = ExecutionPolicy(volume_participation=0.1)
    assert policy.fill_cap(1500) == 100
    assert policy.fill_cap(99) == 0
    assert policy.fill_cap(None) == 0  # unknown liquidity cannot waive an enabled cap
    assert policy.fill_cap(0.0) == 0


def test_matcher_buy_shares_cap() -> None:
    cost = CostModel(round_fee=False)
    cash, shares, trade, reason = buy(
        100000.0, 0, 10.0, "20260105", cost, "600001.SH", shares_cap=300,
    )
    assert trade is not None and trade.shares == 300
    cash2, shares2, trade2, _ = buy(
        100000.0, 0, 10.0, "20260105", cost, "600001.SH", shares_cap=0,
    )
    assert trade2 is None and reason != "资金不足一手"


def test_daily_executor_policy_caps_fills() -> None:
    bars = _daily_bars(volume=1500)
    base = run_daily_executor(
        bars, _AlwaysBuy(), 100000.0, CostModel(round_fee=False), "600001.SH"
    )
    capped = run_daily_executor(
        bars, _AlwaysBuy(), 100000.0, CostModel(round_fee=False), "600001.SH",
        policy=ExecutionPolicy(volume_participation=0.1),
    )
    base_shares = base["trades"][0]["shares"]
    capped_shares = capped["trades"][0]["shares"]
    assert base_shares == 9900
    assert capped_shares == 100
    assert capped["cash"] > base["cash"]
    for o, n in zip(base["signals"], capped["signals"]):
        assert o == n


def _xs_data(volume: float | None) -> CrossSectionalData:
    dates = ["20260901", "20260902", "20260903"]
    scores = pd.DataFrame([[1.0, 0.5]], index=[dates[0]], columns=["A", "B"])
    opens = pd.DataFrame(
        [[10.0, 10.0]] * 3, index=dates, columns=["A", "B"]
    )
    volume_panel = (
        None
        if volume is None
        else pd.DataFrame([[volume, volume]] * 3, index=dates, columns=["A", "B"])
    )
    return CrossSectionalData(dates, scores, opens, opens, volume=volume_panel)


_ZERO_COST = CostModel(
    round_fee=False, slippage=0.0, commission_rate=0.0,
    min_commission=0.0, transfer_fee_rate=0.0,
)


def test_cross_sectional_policy_caps_and_skips() -> None:
    data = _xs_data(volume=1500)
    base = run_cross_sectional_backtest(
        data=data, strategy=TopNScoreStrategy(top_n=1), initial_cash=101000.0,
        cost=_ZERO_COST, rebalance_dates=["20260901"],
    )
    capped = run_cross_sectional_backtest(
        data=data, strategy=TopNScoreStrategy(top_n=1), initial_cash=101000.0,
        cost=_ZERO_COST, rebalance_dates=["20260901"],
        policy=ExecutionPolicy(volume_participation=0.1, record_suspensions=True),
    )
    assert base["shares"]["A"] == 10100
    assert capped["shares"]["A"] == 100
    assert base["trades"][0]["shares"] == 10100
    assert capped["trades"][0]["shares"] == 100
    assert base["skips"] == []
    assert capped["skips"] == []


def test_cross_sectional_volume_panel_ignored_without_policy() -> None:
    data = _xs_data(volume=1.0)
    base = run_cross_sectional_backtest(
        data=CrossSectionalData(data.dates, data.factor_scores, data.open, data.close),
        strategy=TopNScoreStrategy(top_n=1), initial_cash=101000.0,
        cost=_ZERO_COST, rebalance_dates=["20260901"],
    )
    with_volume = run_cross_sectional_backtest(
        data=data, strategy=TopNScoreStrategy(top_n=1), initial_cash=101000.0,
        cost=_ZERO_COST, rebalance_dates=["20260901"],
    )
    assert base["trades"] == with_volume["trades"]
    assert base["shares"] == with_volume["shares"]
    assert with_volume["skips"] == []


def test_cross_sectional_records_suspension_skips() -> None:
    dates = ["20260901", "20260902", "20260903", "20260904"]
    scores = pd.DataFrame(
        [[1.0, 0.5], [0.5, 1.0]], index=[dates[0], dates[2]], columns=["A", "B"]
    )
    opens = pd.DataFrame(
        [[10.0, 10.0], [10.0, 10.0], [10.0, 10.0], [np.nan, 10.0]],
        index=dates, columns=["A", "B"],
    )
    data = CrossSectionalData(dates, scores, opens, opens)
    result = run_cross_sectional_backtest(
        data=data, strategy=TopNScoreStrategy(top_n=1), initial_cash=101000.0,
        cost=_ZERO_COST, rebalance_dates=[dates[0], dates[2]],
        policy=ExecutionPolicy(record_suspensions=True),
    )
    reasons = [(s["date"], s["symbol"], s["reason"]) for s in result["skips"]]
    assert ("20260904", "A", "停牌或开盘价缺失") in reasons
