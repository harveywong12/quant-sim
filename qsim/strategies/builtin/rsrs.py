"""RSRS timing strategy migrated from app/strategies/rsrs.py."""

from __future__ import annotations

import numpy as np
import pandas as pd

from qsim.strategies.base import Strategy


class RsrsStrategy(Strategy):
    name = "rsrs"

    def __init__(self, lookback: int = 18, threshold: float = 0.7) -> None:
        self.lookback = lookback
        self.threshold = threshold

    def _slope_and_r2(self, lows: np.ndarray, highs: np.ndarray) -> tuple[float, float]:
        slope, intercept = np.polyfit(lows, highs, 1)
        pred = slope * lows + intercept
        ss_res = float(((highs - pred) ** 2).sum())
        ss_tot = float(((highs - highs.mean()) ** 2).sum())
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
        return float(slope), r2

    def generate(self, df: pd.DataFrame) -> str:
        n = len(df)
        if n < self.lookback:
            return "HOLD"
        lows = df["low"].to_numpy()
        highs = df["high"].to_numpy()
        slopes: list[float] = []
        r2s: list[float] = []
        for i in range(self.lookback - 1, n):
            s, r2 = self._slope_and_r2(
                lows[i - self.lookback + 1: i + 1],
                highs[i - self.lookback + 1: i + 1],
            )
            slopes.append(s)
            r2s.append(r2)
        last_slope = slopes[-1]
        mean = float(np.mean(slopes))
        std = float(np.std(slopes))
        if std == 0:
            return "HOLD"
        zscore = (last_slope - mean) / std
        score = zscore * r2s[-1]
        if score > self.threshold:
            return "BUY"
        if score < -self.threshold:
            return "SELL"
        return "HOLD"
