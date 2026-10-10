"""Adaptive grid with trend switch migrated from app/strategies/grid_trend.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class GridTrendStrategy(Strategy):
    name = "grid_trend"

    def __init__(
        self,
        anchor_ma: int = 20,
        grid_pct: float = 0.01,
        grid_num: int = 8,
        base_grid: int = 2,
        er_period: int = 20,
        trend_threshold: float = 0.30,
        range_threshold: float = 0.20,
        confirm_days: int = 3,
    ) -> None:
        self.anchor_ma = anchor_ma
        self.grid_pct = grid_pct
        self.grid_num = grid_num
        self.base_grid = base_grid
        self.er_period = er_period
        self.trend_threshold = trend_threshold
        self.range_threshold = range_threshold
        self.confirm_days = confirm_days
        self._state = "range"
        self._streak = 0

    def _er(self, close: pd.Series) -> float:
        n = self.er_period
        if len(close) < n + 1:
            return 0.0
        change = abs(float(close.iloc[-1] - close.iloc[-1 - n]))
        path = float(close.diff().abs().iloc[-n:].sum())
        return change / path if path > 0 else 0.0

    def _update_state(self, er: float) -> None:
        if self._state == "range":
            if er >= self.trend_threshold:
                self._streak += 1
                if self._streak >= self.confirm_days:
                    self._state = "trend"
                    self._streak = 0
            else:
                self._streak = 0
        else:
            if er <= self.range_threshold:
                self._streak += 1
                if self._streak >= self.confirm_days:
                    self._state = "range"
                    self._streak = 0
            else:
                self._streak = 0

    def _grid_weight(self, df: pd.DataFrame) -> float:
        if len(df) < self.anchor_ma + 1:
            return float(self.base_grid) / self.grid_num
        close = df["close"]
        anchor = close.rolling(self.anchor_ma).mean().iloc[-1]
        if pd.isna(anchor):
            return float(self.base_grid) / self.grid_num
        price = close.iloc[-1]
        offset = (price - anchor) / anchor
        below = -offset / self.grid_pct
        grid = max(self.base_grid, min(int(below), self.grid_num))
        return grid / self.grid_num

    def generate(self, df: pd.DataFrame):
        self._update_state(self._er(df["close"]))
        if self._state == "trend":
            close = df["close"]
            ma = close.rolling(self.anchor_ma).mean().iloc[-1]
            if not pd.isna(ma) and close.iloc[-1] > ma:
                return "BUY", 1.0
            return "BUY", 0.0
        return "BUY", self._grid_weight(df)
