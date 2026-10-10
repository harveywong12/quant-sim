"""Hold strategy: never trade."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class HoldStrategy(Strategy):
    name = "hold"

    def generate(self, df: pd.DataFrame):
        return "HOLD"
