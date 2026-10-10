"""Bollinger mean-reversion strategy migrated from app/strategies/bollinger.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class BollingerStrategy(Strategy):
    name = "bollinger"

    def __init__(self, period: int = 20, k: float = 2.0) -> None:
        self.period = period
        self.k = k

    def generate(self, df: pd.DataFrame) -> str:
        if len(df) < self.period:
            return "HOLD"
        close = df["close"]
        mid = close.rolling(self.period).mean()
        std = close.rolling(self.period).std()
        upper = (mid + self.k * std).iloc[-1]
        lower = (mid - self.k * std).iloc[-1]
        last = close.iloc[-1]
        if pd.isna(upper) or pd.isna(lower):
            return "HOLD"
        if last < lower:
            return "BUY"
        if last > upper:
            return "SELL"
        return "HOLD"
