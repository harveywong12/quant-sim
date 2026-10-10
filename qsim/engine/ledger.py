"""Append-only event ledger and simple projection helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LedgerEntry:
    seq: int
    date: str
    type: str
    payload: dict[str, Any]


@dataclass
class Ledger:
    initial_cash: float
    entries: list[LedgerEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._next_seq = 1

    def record(self, date: str, type: str, payload: dict[str, Any]) -> LedgerEntry:
        entry = LedgerEntry(self._next_seq, date, type, dict(payload))
        self._next_seq += 1
        self.entries.append(entry)
        return entry

    def trades(self) -> list[dict[str, Any]]:
        return [entry.payload for entry in self.entries if entry.type in ("BUY", "SELL")]

    def snapshots(self) -> list[dict[str, Any]]:
        return [entry.payload for entry in self.entries if entry.type == "SNAPSHOT"]
