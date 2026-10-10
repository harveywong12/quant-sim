"""Cross-sectional stock-selection strategy interface.

This interface is used by strategies that select a portfolio of stocks from a
universe on each rebalance date, e.g. multi-factor and Alpha158/Alpha191.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class CrossSectionContext:
    date: str
    prices: pd.Series
    universe: list[str]
    factor_scores: pd.Series | None = None
    extra: dict[str, Any] | None = None


class CrossSectionalStrategy(ABC):
    name: str = "cross_sectional"

    def reset(self) -> None:
        """Called before a backtest/simulation starts."""

    @abstractmethod
    def select(self, context: CrossSectionContext) -> list[str]:
        """Return the next portfolio symbol list, ordered by preference."""


class TopNScoreStrategy(CrossSectionalStrategy):
    """Select the top-N symbols by a precomputed factor score."""

    name = "top_n_score"

    def __init__(self, top_n: int = 10) -> None:
        self.top_n = top_n

    def rank(self, context: CrossSectionContext, limit: int | None = None) -> list[str]:
        """Return the deterministic score ranking before execution constraints.

        The runner asks for a wider candidate set before applying A-share
        board-lot and price feasibility checks.  Returning only ``top_n`` here
        can leave a mostly-cash sleeve when high-price names cannot be bought.
        """
        if context.factor_scores is None or context.factor_scores.empty:
            return []
        universe = set(context.universe)
        scores = context.factor_scores[context.factor_scores.index.isin(universe)].dropna()
        # ``DataFrame(...).T`` gives every rebalance row one shared column
        # order.  Pandas' default quicksort is not stable for equal scores,
        # so the old score order cannot be recovered from that row order.
        # Make the migration contract explicit: descending score, then
        # ascending symbol code.  This is also the order used by the legacy
        # multi_factor shadow reference.
        ordered = sorted(
            scores.index,
            key=lambda symbol: (-float(scores.loc[symbol]), str(symbol)),
        )
        return ordered[:limit] if limit is not None else ordered

    def select(self, context: CrossSectionContext) -> list[str]:
        return self.rank(context, limit=self.top_n)


class MultiFactorStrategy(TopNScoreStrategy):
    """Multi-factor stock selection using a precomputed factor score panel."""

    name = "multi_factor"


def is_star_board(symbol: str) -> bool:
    """Whether ``symbol`` belongs to the STAR Market in the stock universe."""
    return str(symbol).split(".", 1)[0].startswith(("688", "689"))


class NoStarMultiFactorStrategy(MultiFactorStrategy):
    """Keep the v3 ranking, but remove unsupported STAR Market candidates.

    The filter intentionally applies before the shared runner's affordability
    backfill, so an unavailable 688/689 name is replaced by the next score
    rather than creating a cash hole or changing the remaining rank order.
    """

    name = "multi_factor_v4"

    def rank(self, context: CrossSectionContext, limit: int | None = None) -> list[str]:
        ranked = super().rank(context, limit=None)
        eligible = [symbol for symbol in ranked if not is_star_board(symbol)]
        return eligible[:limit] if limit is not None else eligible


class Alpha158Strategy(TopNScoreStrategy):
    """Alpha158-style stock selection using a precomputed score panel."""

    name = "alpha158"
