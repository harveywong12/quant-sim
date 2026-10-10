"""Portfolio models used by the strategy adapter and execution layer."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    qty: float
    avg_cost: float = 0.0
    sellable_qty: float = 0.0

    @property
    def market_value(self, price: float) -> float:
        return self.qty * price


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    date: dt.date
    cash: float
    positions: tuple[Position, ...] = ()
    total_value: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "cash": self.cash,
            "total_value": self.total_value,
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "avg_cost": p.avg_cost,
                    "sellable_qty": p.sellable_qty,
                }
                for p in self.positions
            ],
        }


@dataclass(frozen=True, slots=True)
class TargetPortfolio:
    date: dt.date
    weights: dict[str, float] = field(default_factory=dict)
    reason: str = ""
