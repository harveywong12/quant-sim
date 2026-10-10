from __future__ import annotations

import pandas as pd

from qsim.projections.shadow_diff import (
    compare_equity,
    compare_event_stream,
    compare_metrics,
    compare_strategy_outputs,
)


def test_compare_metrics_ok() -> None:
    result = compare_metrics(
        {"total_return": 0.10, "annual_return": 0.03, "max_drawdown": -0.20, "sharpe": 0.8},
        {"total_return": 0.10, "annual_return": 0.03, "max_drawdown": -0.20, "sharpe": 0.8},
    )
    assert result.ok is True
    assert all(v == 0.0 for v in result.differences.values())


def test_compare_metrics_detect_diff() -> None:
    result = compare_metrics(
        {"total_return": 0.10, "sharpe": 0.8},
        {"total_return": 0.20, "sharpe": 0.8},
        tolerances={"total_return": 0.01},
    )
    assert result.ok is False
    assert "total_return" in result.differences


def test_compare_equity_ok() -> None:
    dates = ["20260101", "20260102"]
    q = pd.DataFrame({"date": dates, "total_value": [100.0, 101.0]})
    o = pd.DataFrame({"date": dates, "total_value": [100.0, 101.0]})
    result = compare_equity(q, o)
    assert result.ok is True


def test_compare_equity_detect_diff() -> None:
    dates = ["20260101", "20260102"]
    q = pd.DataFrame({"date": dates, "total_value": [100.0, 101.0]})
    o = pd.DataFrame({"date": dates, "total_value": [100.0, 103.0]})
    result = compare_equity(q, o)
    assert result.ok is False
    assert result.differences["max_total_value_diff"] > 0


def test_compare_event_stream_requires_ordered_parity() -> None:
    same = [{"date": "20260102", "signal": "BUY", "weight": 1.0}]
    assert compare_event_stream(same, same, keys=("date", "signal", "weight")).ok

    shifted = [{"date": "20260103", "signal": "BUY", "weight": 1.0}]
    result = compare_event_stream(same, shifted, keys=("date", "signal", "weight"))
    assert result.ok is False
    assert "date" in result.differences


def test_compare_strategy_outputs_requires_all_sections() -> None:
    output = {
        "signals": [{"date": "20260102", "signal": "HOLD", "weight": 0.0}],
        "trades": [],
        "equity": pd.DataFrame({"date": ["20260102"], "total_value": [100.0]}),
        "metrics": {"total_return": 0.0},
    }
    result = compare_strategy_outputs(output, output)
    assert result.ok is False
    assert result.differences["metrics.missing_annual_return"] == 1.0
    result = compare_strategy_outputs(output, {"signals": []})
    assert result.ok is False
    assert result.differences["missing_sections"] > 0
