"""Bar model."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any


def parse_date(value: Any) -> dt.date:
    if isinstance(value, dt.date):
        return value
    text = str(value).replace("-", "")
    return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))


@dataclass(frozen=True, slots=True)
class Bar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
        }
