"""Minimal domain event container.

Step 2 only needs a transport-neutral event record; the event-sourced ledger
itself will be implemented in the engine layer.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    ts: dt.datetime
    payload: dict[str, Any] = field(default_factory=dict)
