from __future__ import annotations

from qsim.strategies.migration import (
    ACTIVE_MIGRATIONS,
    ExecutionKind,
    MigrationState,
    active_migration_names,
)
from qsim.strategies.pool import validate_active_pool
from qsim.strategies.registry import active_migration_manifest, migration_spec


def test_all_active_strategies_have_parity_contracts() -> None:
    assert len(ACTIVE_MIGRATIONS) == 8
    assert set(active_migration_names()) == {
        "tsmom_vol", "dual_ma", "grid_atr", "rsi_boll",
        "trend_fallback", "cb_dual_low", "multi_factor", "alpha158",
    }
    assert all(item.parity_required for item in ACTIVE_MIGRATIONS)
    assert all(item.state is MigrationState.PARITY_PENDING for item in ACTIVE_MIGRATIONS)


def test_manifest_preserves_execution_kinds() -> None:
    assert migration_spec("trend_fallback")["execution"] == ExecutionKind.ROTATION.value
    assert migration_spec("multi_factor")["execution"] == ExecutionKind.CROSS_SECTIONAL.value
    assert migration_spec("alpha158")["parity_required"] is True
    assert len(active_migration_manifest()) == 8


def test_active_pool_and_migration_manifest_are_aligned() -> None:
    assert validate_active_pool() == []
