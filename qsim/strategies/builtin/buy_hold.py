"""Buy-and-hold strategy: buy once on the first signal, then hold."""

from __future__ import annotations

import pandas as pd

from qsim.strategies.base import Strategy


class BuyHoldStrategy(Strategy):
    name = "buy_hold"

    def __init__(self) -> None:
        self._first = True

    def reset(self) -> None:
        self._first = True

    def generate(self, df: pd.DataFrame):
        if self._first:
            self._first = False
            return "BUY"
        return "HOLD"
