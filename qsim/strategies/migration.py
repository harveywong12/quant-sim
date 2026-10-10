"""Durable migration contract for the eight active strategies.

This module is intentionally metadata-only.  It does not select a strategy or
change allocation weights; it gives the registry, shadow runner, and operator
reports one source of truth for migration state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import json
from dataclasses import replace
from pathlib import Path


class ExecutionKind(str, Enum):
    SIGNAL = "signal"
    ROTATION = "rotation"
    CROSS_SECTIONAL = "cross_sectional"


class MigrationState(str, Enum):
    PARITY_PENDING = "parity_pending"
    SHADOWING = "shadowing"
    PAPER_ENABLED = "paper_enabled"
    HISTORICAL_ONLY = "historical_only"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class StrategyMigration:
    name: str
    family: str
    execution: ExecutionKind
    legacy_sources: tuple[str, ...]
    qsim_target: str
    parity_required: bool = True
    state: MigrationState = MigrationState.PARITY_PENDING

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["execution"] = self.execution.value
        value["state"] = self.state.value
        value["legacy_sources"] = list(self.legacy_sources)
        return value


ACTIVE_MIGRATIONS: tuple[StrategyMigration, ...] = (
    StrategyMigration(
        "tsmom_vol", "trend", ExecutionKind.SIGNAL,
        ("app/strategies/tsmom.py",), "qsim/strategies/builtin/tsmom.py",
    ),
    StrategyMigration(
        "dual_ma", "trend", ExecutionKind.SIGNAL,
        ("app/strategies/dual_ma.py",), "qsim/strategies/builtin/dual_ma.py",
    ),
    StrategyMigration(
        "grid_atr", "mean_reversion", ExecutionKind.SIGNAL,
        ("app/strategies/grid_atr.py",), "qsim/strategies/builtin/grid_atr.py",
    ),
    StrategyMigration(
        "rsi_boll", "mean_reversion", ExecutionKind.SIGNAL,
        ("app/strategies/rsi_boll.py",), "qsim/strategies/builtin/rsi_boll.py",
    ),
    StrategyMigration(
        "trend_fallback", "defensive", ExecutionKind.ROTATION,
        ("app/strategies/trend_fallback.py",), "qsim/strategies/rotation.py",
    ),
    StrategyMigration(
        "cb_dual_low", "defensive", ExecutionKind.SIGNAL,
        ("data/strategies/cb_dual_low.py",), "qsim/strategies/builtin/cb_dual_low.py",
    ),
    StrategyMigration(
        "multi_factor", "cross_sectional", ExecutionKind.CROSS_SECTIONAL,
        ("app/factor.py", "app/factor_live.py",),
        "qsim/features/factor.py + qsim/strategies/cross_sectional.py",
    ),
    StrategyMigration(
        "alpha158", "cross_sectional", ExecutionKind.CROSS_SECTIONAL,
        ("app/alpha_engine.py", "app/alpha_live.py",),
        "qsim/features/alpha158.py + qsim/strategies/cross_sectional.py",
    ),
)

MIGRATION_BY_NAME = {item.name: item for item in ACTIVE_MIGRATIONS}

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "strategy-migration.json"


def _configured_states() -> dict[str, MigrationState]:
    """Read only durable state overrides; malformed state fails closed."""
    try:
        payload = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        values = payload.get("strategies", [])
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    result: dict[str, MigrationState] = {}
    for item in values:
        if not isinstance(item, dict) or "name" not in item:
            continue
        try:
            result[str(item["name"])] = MigrationState(str(item.get("state", "")))
        except ValueError:
            # Unknown/corrupt state is intentionally ignored, leaving the
            # immutable code default of parity_pending in force.
            continue
    return result


def migration_manifest() -> list[dict[str, object]]:
    return [migration_for(item.name).as_dict() for item in ACTIVE_MIGRATIONS]


def migration_for(name: str) -> StrategyMigration:
    try:
        item = MIGRATION_BY_NAME[name]
    except KeyError as exc:
        raise KeyError(f"unknown active strategy: {name}") from exc
    return replace(item, state=_configured_states().get(name, item.state))


def active_migration_names() -> tuple[str, ...]:
    return tuple(item.name for item in ACTIVE_MIGRATIONS)


def paper_enabled(name: str) -> bool:
    """Return whether a strategy has explicitly passed the paper gate."""
    return migration_for(name).state is MigrationState.PAPER_ENABLED
