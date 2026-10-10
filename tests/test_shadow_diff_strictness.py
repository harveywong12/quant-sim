from __future__ import annotations

import pandas as pd

from qsim.projections.shadow_diff import compare_equity, compare_metrics


def test_compare_metrics_fails_on_missing_required_metric() -> None:
    result = compare_metrics(
        {"total_return": 0.1},
        {"total_return": 0.1, "annual_return": 0.1},
        tolerances={"total_return": 0.01, "annual_return": 0.01},
    )

    assert not result.ok
    assert "missing_annual_return" in result.differences


def test_compare_metrics_fails_on_nonfinite_value() -> None:
    result = compare_metrics(
        {"total_return": float("nan")},
        {"total_return": 0.1},
        tolerances={"total_return": 0.01},
    )

    assert not result.ok
    assert "nonfinite_total_return" in result.differences


def test_compare_equity_fails_on_date_sequence_mismatch() -> None:
    qsim = pd.DataFrame({"date": ["20260803", "20260805"], "total_value": [1.0, 1.0]})
    old = pd.DataFrame({"date": ["20260803", "20260804"], "total_value": [1.0, 1.0]})

    result = compare_equity(qsim, old)

    assert not result.ok
    assert result.differences["date_sequence_mismatch"] == 1.0
    assert result.differences["missing_equity_dates"] == 1.0
    assert result.differences["extra_equity_dates"] == 1.0
