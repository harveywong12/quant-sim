"""Adaptive market-state rotator migrated from app/strategies/adaptive.py."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy
from qsim.strategies.registry import create_builtin


class AdaptiveRotator(Strategy):
    name = "adaptive"

    def __init__(
        self,
        trend_strategy: str = "ma_slope",
        range_strategy: str = "rsi_boll",
        er_period: int = 20,
        trend_threshold: float = 0.30,
        range_threshold: float = 0.20,
        confirm_days: int = 3,
        trend_params: dict | None = None,
        range_params: dict | None = None,
    ) -> None:
        self.trend = create_builtin(trend_strategy, trend_params or {})
        self.rng = create_builtin(range_strategy, range_params or {})
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

    def generate(self, df: pd.DataFrame) -> str:
        self._update_state(self._er(df["close"]))
        if self._state == "trend":
            return self.trend.generate(df)
        return self.rng.generate(df)
