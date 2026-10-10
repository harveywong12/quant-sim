"""Multi-asset rotation strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class RotationStrategy(ABC):
    """Multi-asset rotation strategy interface.

    ``generate(data)`` returns a target symbol, ``"CASH"`` or ``"HOLD"``.
    """

    name: str = "rotation"

    def reset(self) -> None:
        """Called before a backtest/simulation starts."""

    @abstractmethod
    def generate(self, data: dict[str, pd.DataFrame]) -> str: ...


class MomentumRotation(RotationStrategy):
    """Momentum rotation: hold the symbol with the strongest N-day return."""

    name = "momentum_rotation"

    def __init__(self, lookback: int = 20, cash_when_negative: bool = True) -> None:
        self.lookback = lookback
        self.cash_when_negative = cash_when_negative

    def generate(self, data: dict[str, pd.DataFrame]) -> str:
        scores: dict[str, float] = {}
        for sym, df in data.items():
            if df is None or len(df) < self.lookback + 1:
                continue
            close = df["close"]
            ret = float(close.iloc[-1] / close.iloc[-1 - self.lookback] - 1)
            scores[sym] = ret
        if not scores:
            return "HOLD"
        best = max(scores, key=scores.get)
        if self.cash_when_negative and scores[best] < 0:
            return "CASH"
        return best


class TrendFallback(RotationStrategy):
    """Trend following with a defensive fallback symbol."""

    name = "trend_fallback"

    def __init__(self, symbol: str, ma: int = 60, fallback: str = "511380") -> None:
        self.symbol = str(symbol)
        self.ma = int(ma)
        self.fallback = str(fallback)

    def generate(self, data: dict[str, pd.DataFrame]) -> str:
        df = data.get(self.symbol)
        if df is None or len(df) < self.ma + 1:
            return self.fallback
        close = df["close"]
        ma_val = close.rolling(self.ma).mean().iloc[-1]
        if pd.isna(ma_val):
            return self.fallback
        return self.symbol if float(close.iloc[-1]) > float(ma_val) else self.fallback


ROTATION_BUILTIN: dict[str, type[RotationStrategy]] = {
    "momentum_rotation": MomentumRotation,
    "trend_fallback": TrendFallback,
}
