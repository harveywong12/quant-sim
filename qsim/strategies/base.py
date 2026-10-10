"""Strategy interface shared by backtest, paper trading and hot-zone loading."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class Strategy(ABC):
    name: str = "base"

    def reset(self) -> None:
        """Called once before a backtest/simulation starts."""

    @abstractmethod
    def generate(self, df: pd.DataFrame):
        """Return 'BUY' / 'SELL' / 'HOLD' or ('BUY'|'SELL', target_weight)."""
