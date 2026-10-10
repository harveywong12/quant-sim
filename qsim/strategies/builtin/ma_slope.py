"""Moving average slope strategy migrated from app/strategies/ma_slope.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class MaSlopeStrategy(Strategy):
    name = "ma_slope"

    def __init__(self, period: int = 20, lookback: int = 3) -> None:
        self.period = period
        self.lookback = lookback

    def generate(self, df: pd.DataFrame) -> str:
        if len(df) < self.period + self.lookback:
            return "HOLD"
        ma = df["close"].rolling(self.period).mean()
        if pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-1 - self.lookback]):
            return "HOLD"
        slope = ma.iloc[-1] - ma.iloc[-1 - self.lookback]
        if slope > 0:
            return "BUY"
        if slope < 0:
            return "SELL"
        return "HOLD"
