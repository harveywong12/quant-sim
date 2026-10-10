"""Simple deterministic regime detector used by the portfolio demo.

This is intentionally simple and transparent.  It is not a SOTA model; it
exists to let the portfolio layer be exercised with a repeatable signal.
"""

from __future__ import annotations

import math

import pandas as pd


class SimpleRegimeDetector:
    def __init__(
        self,
        ma_window: int = 20,
        vol_window: int = 20,
        high_vol: float = 0.25,
    ) -> None:
        self.ma_window = ma_window
        self.vol_window = vol_window
        self.high_vol = high_vol

    def detect(self, df: pd.DataFrame) -> dict[str, float]:
        if len(df) < self.ma_window + 1:
            return {"up": 0.3, "range": 0.4, "down": 0.3}
        close = df["close"]
        last = float(close.iloc[-1])
        ma = float(close.rolling(self.ma_window).mean().iloc[-1])
        vol = float(close.pct_change().rolling(self.vol_window).std().iloc[-1] * math.sqrt(252))

        up = 0.5 if last > ma else 0.1
        down = 0.1 if last > ma else 0.5
        range_prob = 0.4
        if vol and vol > self.high_vol:
            range_prob += 0.2
            up *= 0.5
            down *= 0.5
        total = up + down + range_prob
        return {
            "up": round(up / total, 4),
            "range": round(range_prob / total, 4),
            "down": round(down / total, 4),
        }
