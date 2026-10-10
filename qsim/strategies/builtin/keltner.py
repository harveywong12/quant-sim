"""Keltner channel strategy migrated from app/strategies/keltner.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class KeltnerStrategy(Strategy):
    name = "keltner"

    def __init__(self, period: int = 20, k: float = 2.0) -> None:
        self.period = period
        self.k = k

    def generate(self, df: pd.DataFrame) -> str:
        if len(df) < self.period + 1:
            return "HOLD"
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(self.period).mean()
        ema = close.ewm(span=self.period, adjust=False).mean()
        last_close = close.iloc[-1]
        upper = (ema + self.k * atr).iloc[-1]
        lower = (ema - self.k * atr).iloc[-1]
        if pd.isna(upper) or pd.isna(lower):
            return "HOLD"
        if last_close > upper:
            return "BUY"
        if last_close < lower:
            return "SELL"
        return "HOLD"
