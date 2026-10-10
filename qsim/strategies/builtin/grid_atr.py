"""ATR adaptive grid strategy migrated from app/strategies/grid_atr.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class GridATRStrategy(Strategy):
    name = "grid_atr"

    def __init__(
        self,
        anchor_ma: int = 20,
        atr_period: int = 20,
        k: float = 1.0,
        grid_num: int = 8,
        base_grid: int = 2,
        min_grid_pct: float = 0.004,
        max_grid_pct: float = 0.04,
    ) -> None:
        self.anchor_ma = anchor_ma
        self.atr_period = atr_period
        self.k = k
        self.grid_num = grid_num
        self.base_grid = base_grid
        self.min_grid_pct = min_grid_pct
        self.max_grid_pct = max_grid_pct

    def _atr(self, df: pd.DataFrame) -> float:
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return float(tr.rolling(self.atr_period).mean().iloc[-1])

    def generate(self, df: pd.DataFrame):
        if len(df) < max(self.anchor_ma, self.atr_period) + 1:
            return "HOLD", 0.0
        close = df["close"]
        anchor = close.rolling(self.anchor_ma).mean().iloc[-1]
        price = close.iloc[-1]
        if pd.isna(anchor):
            return "HOLD", 0.0
        atr = self._atr(df)
        grid_pct = self.k * atr / price
        grid_pct = max(self.min_grid_pct, min(grid_pct, self.max_grid_pct))
        offset = (price - anchor) / anchor
        below = -offset / grid_pct
        grid = max(self.base_grid, min(int(below), self.grid_num))
        return "BUY", grid / self.grid_num
