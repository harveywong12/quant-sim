from __future__ import annotations

from qsim.strategies.pool import (
    ACTIVE_POOL,
    StrategyFamily,
    active_strategies,
    strategies_in_family,
)


def test_active_pool_has_four_families() -> None:
    families = set(ACTIVE_POOL.values())
    assert families == {
        StrategyFamily.TREND,
        StrategyFamily.MEAN_REVERSION,
        StrategyFamily.DEFENSIVE,
        StrategyFamily.CROSS_SECTIONAL,
    }


def test_active_pool_size_is_small() -> None:
    assert 7 <= len(active_strategies()) <= 9


def test_each_family_has_representative() -> None:
    for family in StrategyFamily:
        assert strategies_in_family(family), f"{family} has no active strategy"
