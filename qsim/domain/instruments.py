"""Instrument model."""

from __future__ import annotations

from dataclasses import dataclass


def venue_from_symbol(symbol: str) -> str:
    """Best-effort venue inference for Chinese A-share symbols.

    ETF codes are not self-describing; callers may override with an explicit venue.
    """
    if symbol.startswith(("6", "5", "9")):
        return "XSHG"
    if symbol.startswith(("0", "1", "2", "3")):
        return "XSHE"
    return "XSHG"


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    venue: str = ""
    quote_ccy: str = "CNY"
    name: str = ""

    def __post_init__(self) -> None:
        if not self.venue:
            object.__setattr__(self, "venue", venue_from_symbol(self.symbol))

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.symbol}"
