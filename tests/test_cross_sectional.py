from __future__ import annotations

import pandas as pd

from qsim.strategies.cross_sectional import (
    CrossSectionContext,
    CrossSectionalStrategy,
    TopNScoreStrategy,
)


def test_cross_sectional_context() -> None:
    prices = pd.DataFrame({"close": [10.0, 20.0]}, index=["A", "B"])
    context = CrossSectionContext(
        date="20260101",
        prices=prices,
        universe=["A", "B"],
    )
    assert context.date == "20260101"
    assert context.universe == ["A", "B"]


def test_top_n_score_strategy() -> None:
    scores = pd.Series({"A": 1.0, "B": 3.0, "C": 2.0, "D": 0.5})
    context = CrossSectionContext(
        date="20260101",
        prices=pd.DataFrame(index=["A", "B", "C", "D"]),
        universe=["A", "B", "C", "D"],
        factor_scores=scores,
    )
    strategy = TopNScoreStrategy(top_n=2)
    assert strategy.select(context) == ["B", "C"]


def test_top_n_score_strategy_respects_universe() -> None:
    scores = pd.Series({"A": 1.0, "B": 3.0, "C": 2.0, "D": 0.5})
    context = CrossSectionContext(
        date="20260101",
        prices=pd.DataFrame(index=["A", "B", "C", "D"]),
        universe=["A", "C"],
        factor_scores=scores,
    )
    strategy = TopNScoreStrategy(top_n=5)
    assert strategy.select(context) == ["C", "A"]


class _FixedSelectionStrategy(CrossSectionalStrategy):
    name = "fixed"

    def __init__(self, selected: list[str]) -> None:
        self.selected = selected

    def select(self, context: CrossSectionContext) -> list[str]:
        return self.selected


def test_cross_sectional_interface_is_extensible() -> None:
    context = CrossSectionContext(
        date="20260101",
        prices=pd.DataFrame(index=["A"]),
        universe=["A"],
    )
    strategy = _FixedSelectionStrategy(["A"])
    assert strategy.select(context) == ["A"]
