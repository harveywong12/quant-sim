from __future__ import annotations

from qsim.governance.quarterly_review import StrategyMetrics, review_strategy


def test_promote_good_strategy() -> None:
    metrics = StrategyMetrics(
        name="a",
        return_pct=15.0,
        max_drawdown_pct=-10.0,
        sharpe=1.0,
        correlation=0.4,
    )
    assert review_strategy(metrics).action == "promote"


def test_keep_neutral_strategy() -> None:
    metrics = StrategyMetrics(
        name="b",
        return_pct=2.0,
        max_drawdown_pct=-20.0,
        sharpe=0.5,
        correlation=0.5,
    )
    assert review_strategy(metrics).action == "keep"


def test_demote_bad_strategy() -> None:
    metrics = StrategyMetrics(
        name="c",
        return_pct=-10.0,
        max_drawdown_pct=-35.0,
        sharpe=0.1,
        correlation=0.9,
    )
    decision = review_strategy(metrics)
    assert decision.action == "demote"
    assert len(decision.reasons) >= 1
