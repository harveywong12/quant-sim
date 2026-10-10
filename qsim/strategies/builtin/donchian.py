"""Donchian channel breakout strategy migrated from app/strategies/donchian.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class DonchianStrategy(Strategy):
    name = "donchian"

    def __init__(self, entry: int = 20, exit_period: int = 10) -> None:
        self.entry = entry
        self.exit_period = exit_period

    def generate(self, df: pd.DataFrame) -> str:
        n = len(df)
        if n < max(self.entry, self.exit_period) + 2:
            return "HOLD"
        prev_high = df["high"].iloc[-2]
        prev_low = df["low"].iloc[-2]
        entry_high = df["high"].iloc[-self.entry - 1: -2].max()
        exit_low = df["low"].iloc[-self.exit_period - 1: -2].min()
        if prev_high > entry_high:
            return "BUY"
        if prev_low < exit_low:
            return "SELL"
        return "HOLD"
