"""TSMOM with volatility targeting, migrated from app.strategies.tsmom."""

from __future__ import annotations

import math

import pandas as pd

from qsim.strategies.base import Strategy


class TsmomVolStrategy(Strategy):
    name = "tsmom_vol"

    def __init__(
        self,
        mom_n: int = 252,
        skip: int = 1,
        vol_n: int = 60,
        target_vol: float = 0.15,
        band: float = 0.05,
    ) -> None:
        self.mom_n = mom_n
        self.skip = skip
        self.vol_n = vol_n
        self.target_vol = target_vol
        self.band = band
        self._last_w = 0.0

    def reset(self) -> None:
        self._last_w = 0.0

    def generate(self, df: pd.DataFrame):
        close = df["close"].astype(float)
        n = len(close)
        if n < self.mom_n + self.skip + 1:
            return "HOLD"
        mom = float(close.iloc[-1 - self.skip] / close.iloc[-1 - self.skip - self.mom_n] - 1)
        if mom <= 0:
            if self._last_w > 0:
                self._last_w = 0.0
                return "SELL"
            return "HOLD"
        vol = float(close.pct_change().iloc[-self.vol_n:].std() * math.sqrt(252))
        if not math.isfinite(vol) or vol <= 0:
            return "HOLD"
        w = min(1.0, self.target_vol / vol)
        if abs(w - self._last_w) < self.band:
            return "HOLD"
        self._last_w = w
        return ("BUY", round(w, 3))
