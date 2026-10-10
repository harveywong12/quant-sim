"""Opt-in execution semantics layered on top of the parity-critical matchers.

Defaults reproduce the historical behavior exactly (uncapped fills, silent
suspension skips, open-price-unavailable -> skip).  Enabling a policy field
only ever *restricts* fills or adds observability; it never changes prices,
fees, or signal generation.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from qsim.engine.matcher import LOT_SIZE


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    volume_participation: float = 0.0
    record_suspensions: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.volume_participation <= 1.0:
            raise ValueError("volume_participation must be within [0, 1]")

    def fill_cap(self, volume: float | None) -> int | None:
        """Absolute share cap for a bar, or None when uncapped."""
        if self.volume_participation <= 0.0:
            return None
        if volume is None or not math.isfinite(float(volume)) or volume <= 0:
            return 0
        return int(float(volume) * self.volume_participation) // LOT_SIZE * LOT_SIZE

    def cap_shares(self, shares: int, volume: float | None) -> int:
        cap = self.fill_cap(volume)
        if cap is None:
            return shares
        return max(0, min(int(shares), cap))
