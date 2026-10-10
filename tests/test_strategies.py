from __future__ import annotations

import datetime as dt

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.runner import run_backtest
from qsim.strategies.builtin.buy_hold import BuyHoldStrategy
from qsim.strategies.builtin.grid import GridStrategy
from qsim.strategies.builtin.hold import HoldStrategy
from qsim.strategies.builtin.tsmom import TsmomVolStrategy
from qsim.strategies.registry import BUILTIN, create_builtin


def test_hold_strategy() -> None:
    strategy = HoldStrategy()
    df = pd.DataFrame({"close": [1.0, 2.0]})
    assert strategy.generate(df) == "HOLD"


def test_buy_hold_strategy() -> None:
    strategy = BuyHoldStrategy()
    df = pd.DataFrame({"close": [1.0, 2.0]})
    assert strategy.generate(df) == "BUY"
    assert strategy.generate(df) == "HOLD"
    strategy.reset()
    assert strategy.generate(df) == "BUY"


def test_grid_strategy_weight() -> None:
    strategy = GridStrategy(anchor_ma=2, grid_pct=0.02, grid_num=5, base_grid=1)
    # Price below MA by more than 10% -> 5 grids -> full weight.
    df = pd.DataFrame({"close": [10.0, 10.0, 8.0]})
    signal, weight = strategy.generate(df)
    assert signal == "BUY"
    assert weight == 1.0


def test_tsmom_positive_momentum() -> None:
    strategy = TsmomVolStrategy(mom_n=2, skip=1, vol_n=2, target_vol=0.15, band=0.05)
    # Rising close gives positive momentum and low vol.
    df = pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0, 14.0]})
    signal = strategy.generate(df)
    assert signal == "BUY" or (isinstance(signal, tuple) and signal[0] == "BUY")


def test_builtin_registry() -> None:
    assert {
        "bollinger",
        "buy_hold",
        "donchian",
        "dual_ma",
        "grid",
        "grid_atr",
        "grid_trend",
        "hold",
        "keltner",
        "macd",
        "ma_slope",
        "rsi",
        "rsi_boll",
        "rsrs",
        "single_ma",
        "tsmom_vol",
    } <= set(BUILTIN)
    assert isinstance(create_builtin("hold"), HoldStrategy)
    assert isinstance(create_builtin("grid"), GridStrategy)


def test_all_builtin_strategies_instantiable() -> None:
    short = pd.DataFrame({"close": [1.0, 2.0], "high": [1.1, 2.1], "low": [0.9, 1.9]})
    for name, cls in BUILTIN.items():
        strategy = cls()
        if hasattr(strategy, "reset"):
            strategy.reset()
        # Most strategies return HOLD on too-short data; just ensure no exception.
        strategy.generate(short)


def test_run_backtest_with_grid() -> None:
    bars = pd.DataFrame(
        {
            "date": [dt.date(2026, 1, 1), dt.date(2026, 1, 2), dt.date(2026, 1, 3)],
            "open": [10.0, 10.0, 10.0],
            "high": [10.2, 10.2, 10.2],
            "low": [9.8, 9.8, 9.8],
            "close": [10.0, 10.0, 10.0],
            "volume": [0.0, 0.0, 0.0],
            "amount": [0.0, 0.0, 0.0],
        }
    )
    result = run_backtest(
        bars,
        GridStrategy(anchor_ma=2, grid_pct=0.02, grid_num=5, base_grid=1),
        initial_cash=100000.0,
        cost=CostModel(),
        symbol="512100",
    )
    assert len(result["equity"]) == 3
