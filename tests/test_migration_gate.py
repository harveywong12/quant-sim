from __future__ import annotations

from qsim.strategies.migration import MigrationState, migration_for, paper_enabled


def test_unapproved_strategy_is_not_paper_enabled() -> None:
    strategy = migration_for("grid_atr")

    assert strategy.state is MigrationState.RETIRED
    assert paper_enabled("grid_atr") is False


def test_approved_strategy_is_paper_enabled() -> None:
    strategy = migration_for("tsmom_vol")

    assert strategy.state is MigrationState.PAPER_ENABLED
    assert paper_enabled("tsmom_vol") is True


def test_operator_rebaseline_keeps_multi_factor_enabled() -> None:
    assert migration_for("multi_factor").state is MigrationState.PAPER_ENABLED
    assert paper_enabled("multi_factor") is True


def test_alpha158_retired_after_relative_value_review() -> None:
    assert migration_for("alpha158").state is MigrationState.RETIRED
    assert paper_enabled("alpha158") is False
