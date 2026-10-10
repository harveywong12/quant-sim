"""RSI + Bollinger double-confirmation strategy migrated from app/strategies/rsi_boll.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class RsiBollStrategy(Strategy):
    name = "rsi_boll"

    def __init__(
        self,
        period: int = 20,
        k: float = 2.0,
        rsi_period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
    ) -> None:
        self.period = period
        self.k = k
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought

    def _rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        avg_gain = gain.ewm(alpha=1 / self.rsi_period, min_periods=self.rsi_period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / self.rsi_period, min_periods=self.rsi_period, adjust=False).mean()
        rs = avg_gain / avg_loss
        return 100 - 100 / (1 + rs)

    def generate(self, df: pd.DataFrame) -> str:
        if len(df) < self.period * 2:
            return "HOLD"
        close = df["close"]
        mid = close.rolling(self.period).mean()
        std = close.rolling(self.period).std()
        upper = (mid + self.k * std).iloc[-1]
        lower = (mid - self.k * std).iloc[-1]
        rsi = self._rsi(close).iloc[-1]
        last = close.iloc[-1]
        if pd.isna(upper) or pd.isna(rsi):
            return "HOLD"
        if rsi < self.oversold and last <= lower:
            return "BUY"
        if rsi > self.overbought and last >= upper:
            return "SELL"
        return "HOLD"
