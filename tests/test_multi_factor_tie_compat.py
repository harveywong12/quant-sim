from __future__ import annotations

import pandas as pd

from qsim.strategies.cross_sectional import CrossSectionContext, MultiFactorStrategy


def test_multi_factor_ties_use_symbol_code_as_deterministic_tiebreak() -> None:
    # Deliberately put the tied symbols in reverse source order.  The result
    # must not depend on DataFrame construction order or pandas' default sort.
    scores = pd.Series(
        [1.0, 1.0, 0.5],
        index=["601006.SH", "600252.SH", "000001.SZ"],
    )
    context = CrossSectionContext(
        date="20161010",
        prices=pd.DataFrame(index=scores.index),
        universe=list(scores.index),
        factor_scores=scores,
    )

    assert MultiFactorStrategy(top_n=3).select(context) == [
        "600252.SH",
        "601006.SH",
        "000001.SZ",
    ]
