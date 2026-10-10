"""Active strategy pool: four families, a small set of representative strategies.

This is the institutional answer to "too many similar strategies".  The full
registry may keep legacy strategies, but only ACTIVE_POOL entries are eligible
for live allocation and quarterly review.
"""

from __future__ import annotations

from enum import Enum

from qsim.strategies.migration import (
    ACTIVE_MIGRATIONS,
    MigrationState,
    migration_for,
)


class StrategyFamily(str, Enum):
    TREND = "trend"
    MEAN_REVERSION = "mean_reversion"
    DEFENSIVE = "defensive"
    CROSS_SECTIONAL = "cross_sectional"


ACTIVE_POOL: dict[str, StrategyFamily] = {
    # Trend / momentum
    "tsmom_vol": StrategyFamily.TREND,
    # Mean-reversion / grid
    "grid_atr": StrategyFamily.MEAN_REVERSION,
    "rsi_boll": StrategyFamily.MEAN_REVERSION,
    # Defensive
    "trend_fallback": StrategyFamily.DEFENSIVE,
    "cb_dual_low": StrategyFamily.DEFENSIVE,
    # Cross-sectional
    "multi_factor": StrategyFamily.CROSS_SECTIONAL,
    "alpha158": StrategyFamily.CROSS_SECTIONAL,
}


def active_strategies() -> list[str]:
    return sorted(ACTIVE_POOL)


def family_of(name: str) -> StrategyFamily | None:
    return ACTIVE_POOL.get(name)


def strategies_in_family(family: StrategyFamily) -> list[str]:
    return sorted(name for name, fam in ACTIVE_POOL.items() if fam == family)


def migration_state(name: str) -> MigrationState:
    """Return migration state without making it an allocation decision."""
    return migration_for(name).state


def validate_active_pool() -> list[str]:
    """Return contract violations while allowing retired history contracts.

    Retired strategies remain in the migration manifest for audit and replay,
    but are intentionally absent from the online allocation pool.
    """
    errors: list[str] = []
    manifest_names = {item.name for item in ACTIVE_MIGRATIONS}
    for name in sorted(ACTIVE_POOL):
        if name not in manifest_names:
            errors.append(f"active strategy missing migration contract: {name}")
    inactive_states = {MigrationState.RETIRED, MigrationState.HISTORICAL_ONLY}
    for item in sorted(ACTIVE_MIGRATIONS, key=lambda value: value.name):
        if item.name not in ACTIVE_POOL and migration_for(item.name).state not in inactive_states:
            errors.append(f"migration contract missing from active pool: {item.name}")
    return errors
