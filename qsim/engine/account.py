"""Single-asset account state used by the backtest and paper runners."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Account:
    symbol: str
    cash: float
    shares: int = 0
    avg_cost: float = 0.0
    pending_signal: str = "HOLD"
    pending_weight: float = 0.0
    pending_date: str | None = None
    last_update_date: str | None = None

    def position_value(self, price: float) -> float:
        return self.shares * price

    def total_value(self, price: float) -> float:
        return self.cash + self.position_value(price)
