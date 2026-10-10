"""Dual moving average strategy migrated from app/strategies/dual_ma.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class DualMAStrategy(Strategy):
    name = "dual_ma"

    def __init__(self, short_window: int = 5, long_window: int = 20) -> None:
        if short_window >= long_window:
            raise ValueError("short_window must be smaller than long_window")
        self.short_window = short_window
        self.long_window = long_window

    def generate(self, df: pd.DataFrame) -> str:
        if len(df) < self.long_window + 1:
            return "HOLD"
        close = df["close"]
        ma_s = close.rolling(self.short_window).mean()
        ma_l = close.rolling(self.long_window).mean()
        prev_s, prev_l = ma_s.iloc[-2], ma_l.iloc[-2]
        cur_s, cur_l = ma_s.iloc[-1], ma_l.iloc[-1]
        if any(pd.isna(x) for x in (prev_s, prev_l, cur_s, cur_l)):
            return "HOLD"
        if prev_s <= prev_l and cur_s > cur_l:
            return "BUY"
        if prev_s >= prev_l and cur_s < cur_l:
            return "SELL"
        return "HOLD"
