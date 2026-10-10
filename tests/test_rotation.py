from __future__ import annotations

import pandas as pd

from qsim.strategies.builtin.adaptive import AdaptiveRotator
from qsim.strategies.rotation import MomentumRotation, TrendFallback


def _frame(closes):
    return pd.DataFrame({"close": closes})


def test_momentum_rotation_picks_best() -> None:
    data = {
        "A": _frame([10.0, 11.0, 12.0]),
        "B": _frame([10.0, 10.5, 10.8]),
    }
    strategy = MomentumRotation(lookback=1)
    assert strategy.generate(data) == "A"


def test_momentum_rotation_cash_when_negative() -> None:
    data = {
        "A": _frame([10.0, 9.0, 8.0]),
        "B": _frame([10.0, 9.5, 9.0]),
    }
    strategy = MomentumRotation(lookback=1)
    assert strategy.generate(data) == "CASH"


def test_trend_fallback_fallback() -> None:
    data = {"518880": _frame([10.0, 9.0, 8.0])}
    strategy = TrendFallback(symbol="518880", ma=2, fallback="511380")
    assert strategy.generate(data) == "511380"


def test_adaptive_rotator_runs() -> None:
    df = _frame([10.0, 10.1, 10.2, 10.3, 10.4, 10.5])
    strategy = AdaptiveRotator()
    assert strategy.generate(df) in ("BUY", "SELL", "HOLD")
