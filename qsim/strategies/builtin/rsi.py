"""RSI mean-reversion strategy migrated from app/strategies/rsi.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class RsiStrategy(Strategy):
    name = "rsi"

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
    ) -> None:
        if not (0 < oversold < overbought < 100):
            raise ValueError("need 0 < oversold < overbought < 100")
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def _rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        avg_gain = gain.ewm(alpha=1 / self.period, min_periods=self.period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / self.period, min_periods=self.period, adjust=False).mean()
        rs = avg_gain / avg_loss
        return 100 - 100 / (1 + rs)

    def generate(self, df: pd.DataFrame) -> str:
        if len(df) < self.period * 2:
            return "HOLD"
        rsi = self._rsi(df["close"])
        cur = rsi.iloc[-1]
        if pd.isna(cur):
            return "HOLD"
        if cur < self.oversold:
            return "BUY"
        if cur > self.overbought:
            return "SELL"
        return "HOLD"
