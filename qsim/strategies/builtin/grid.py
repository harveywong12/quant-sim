"""ETF grid strategy migrated from the legacy app.strategies.grid module."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class GridStrategy(Strategy):
    name = "grid"

    def __init__(
        self,
        anchor_ma: int = 20,
        grid_pct: float = 0.02,
        grid_num: int = 5,
        base_grid: int = 1,
    ) -> None:
        self.anchor_ma = anchor_ma
        self.grid_pct = grid_pct
        self.grid_num = grid_num
        self.base_grid = base_grid

    def generate(self, df: pd.DataFrame):
        if len(df) < self.anchor_ma + 1:
            return "HOLD", 0.0
        close = df["close"]
        anchor = close.rolling(self.anchor_ma).mean().iloc[-1]
        if pd.isna(anchor):
            return "HOLD", 0.0
        price = close.iloc[-1]
        offset = (price - anchor) / anchor
        below = -offset / self.grid_pct
        grid = int(below)
        grid = max(self.base_grid, min(grid, self.grid_num))
        return "BUY", grid / self.grid_num
