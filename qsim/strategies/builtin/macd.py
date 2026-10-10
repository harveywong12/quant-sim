"""MACD cross strategy migrated from app/strategies/macd.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class MacdStrategy(Strategy):
    name = "macd"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        if fast >= slow:
            raise ValueError("fast must be smaller than slow")
        self.fast = fast
        self.slow = slow
        self.signal = signal

    def generate(self, df: pd.DataFrame) -> str:
        if len(df) < self.slow + self.signal:
            return "HOLD"
        close = df["close"]
        ema_fast = close.ewm(span=self.fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=self.signal, adjust=False).mean()
        if dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]:
            return "BUY"
        if dif.iloc[-2] >= dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1]:
            return "SELL"
        return "HOLD"
