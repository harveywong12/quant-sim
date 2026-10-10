"""Single moving average strategy migrated from app/strategies/single_ma.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class SingleMAStrategy(Strategy):
    name = "single_ma"

    def __init__(self, period: int = 13) -> None:
        self.period = period

    def generate(self, df: pd.DataFrame) -> str:
        if len(df) < self.period:
            return "HOLD"
        ma = df["close"].rolling(self.period).mean()
        last_close = df["close"].iloc[-1]
        if pd.isna(ma.iloc[-1]):
            return "HOLD"
        return "BUY" if last_close > ma.iloc[-1] else "SELL"
