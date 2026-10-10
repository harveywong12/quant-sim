"""Shadow reconciliation: compare qsim results with the legacy system."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping

import pandas as pd


@dataclass(frozen=True, slots=True)
class ShadowDiffResult:
    ok: bool
    differences: dict[str, float]
    details: list[str]

    @property
    def reasons(self) -> list[str]:
        return self.details


def compare_metrics(
    qsim_metrics: Mapping[str, Any],
    old_metrics: Mapping[str, Any],
    tolerances: Mapping[str, float] | None = None,
) -> ShadowDiffResult:
    tolerances = tolerances or {
        "total_return": 0.01,
        "annual_return": 0.01,
        "max_drawdown": 0.02,
        "sharpe": 0.10,
    }
    differences: dict[str, float] = {}
    details: list[str] = []
    for key, tol in tolerances.items():
        if key not in qsim_metrics or key not in old_metrics:
            differences[f"missing_{key}"] = 1.0
            details.append(
                f"{key} is missing: qsim={'present' if key in qsim_metrics else 'absent'}, "
                f"old={'present' if key in old_metrics else 'absent'}"
            )
            continue
        try:
            q = float(qsim_metrics[key])
            o = float(old_metrics[key])
        except (TypeError, ValueError):
            differences[f"invalid_{key}"] = 1.0
            details.append(f"{key} is not numeric: qsim={qsim_metrics[key]!r}, old={old_metrics[key]!r}")
            continue
        if not math.isfinite(q) or not math.isfinite(o):
            differences[f"nonfinite_{key}"] = 1.0
            details.append(f"{key} is non-finite: qsim={q!r}, old={o!r}")
            continue
        diff = abs(q - o)
        differences[key] = round(diff, 6)
        if diff > tol:
            details.append(
                f"{key} diff={diff:.6f} > tol={tol:.6f} (qsim={q:.6f}, old={o:.6f})"
            )
    return ShadowDiffResult(ok=not details, differences=differences, details=details)


def compare_equity(
    qsim_equity: pd.DataFrame | Iterable[Mapping[str, Any]],
    old_equity: pd.DataFrame | Iterable[Mapping[str, Any]],
    cash_tol: float = 0.01,
) -> ShadowDiffResult:
    if not isinstance(qsim_equity, pd.DataFrame):
        qsim_equity = pd.DataFrame(list(qsim_equity))
    if not isinstance(old_equity, pd.DataFrame):
        old_equity = pd.DataFrame(list(old_equity))
    required = {"date", "total_value"}
    if not required.issubset(qsim_equity.columns) or not required.issubset(old_equity.columns):
        return ShadowDiffResult(False, {"missing_equity_columns": 1.0}, ["equity is missing date/total_value columns"])
    q = qsim_equity[["date", "total_value"]].copy()
    o = old_equity[["date", "total_value"]].copy()
    q["date"] = q["date"].astype(str)
    o["date"] = o["date"].astype(str)
    q_dates = q["date"].tolist()
    o_dates = o["date"].tolist()
    details: list[str] = []
    differences: dict[str, float] = {}
    if q["date"].duplicated().any() or o["date"].duplicated().any():
        differences["duplicate_equity_dates"] = 1.0
        details.append("equity contains duplicate dates")
    if q_dates != o_dates:
        q_set = set(q_dates)
        o_set = set(o_dates)
        missing = sorted(o_set - q_set)
        extra = sorted(q_set - o_set)
        differences["date_sequence_mismatch"] = 1.0
        details.append(
            "equity date sequence differs: "
            f"qsim_count={len(q_dates)} old_count={len(o_dates)}"
        )
        if missing:
            differences["missing_equity_dates"] = float(len(missing))
            details.append(f"missing qsim equity dates: {missing[:5]}")
        if extra:
            differences["extra_equity_dates"] = float(len(extra))
            details.append(f"extra qsim equity dates: {extra[:5]}")
    merged = q.merge(
        o, on="date", suffixes=("_qsim", "_old"), how="inner", validate="one_to_one"
    )
    if merged.empty:
        details.append("no overlapping dates")
        return ShadowDiffResult(False, differences, details)
    diff = (merged["total_value_qsim"] - merged["total_value_old"]).abs()
    max_diff = float(diff.max())
    if max_diff > cash_tol:
        details.append(f"max total_value diff={max_diff:.4f} > {cash_tol}")
    differences["max_total_value_diff"] = round(max_diff, 6)
    return ShadowDiffResult(ok=not details, differences=differences, details=details)


def compare_event_stream(
    qsim_events: Iterable[Mapping[str, Any]],
    old_events: Iterable[Mapping[str, Any]],
    *,
    keys: tuple[str, ...],
    numeric_tolerances: Mapping[str, float] | None = None,
) -> ShadowDiffResult:
    """Compare ordered strategy decisions or fills from the same input snapshot.

    Ordering is intentional: a matching aggregate count is not sufficient for
    parity when a signal moved to a different trading day.
    """
    q = list(qsim_events)
    o = list(old_events)
    tolerances = numeric_tolerances or {}
    details: list[str] = []
    differences: dict[str, float] = {}
    if len(q) != len(o):
        details.append(f"event count differs: qsim={len(q)} old={len(o)}")
        differences["event_count"] = float(abs(len(q) - len(o)))

    for index, (q_event, o_event) in enumerate(zip(q, o)):
        for key in keys:
            if key not in q_event or key not in o_event:
                details.append(f"event[{index}] missing key: {key}")
                differences["missing_fields"] = differences.get("missing_fields", 0.0) + 1.0
                continue
            q_value, o_value = q_event[key], o_event[key]
            tolerance = tolerances.get(key, 0.0)
            try:
                diff = abs(float(q_value) - float(o_value))
                comparable_numeric = isinstance(q_value, (int, float)) and isinstance(
                    o_value, (int, float)
                )
            except (TypeError, ValueError):
                diff = 0.0
                comparable_numeric = False
            mismatch = diff > tolerance if comparable_numeric else q_value != o_value
            if mismatch:
                details.append(
                    f"event[{index}].{key} differs: qsim={q_value!r} old={o_value!r}"
                )
                differences[key] = max(differences.get(key, 0.0), float(diff or 1.0))
    return ShadowDiffResult(not details, differences, details)


def compare_strategy_outputs(
    qsim_output: Mapping[str, Any],
    old_output: Mapping[str, Any],
    *,
    equity_cash_tol: float = 0.01,
    metric_tolerances: Mapping[str, float] | None = None,
) -> ShadowDiffResult:
    """Compare the canonical output envelope used by strategy shadow runs.

    Expected keys are ``signals``, ``trades``, ``equity`` and ``metrics``.
    Missing sections are treated as a parity failure rather than silently
    reducing the comparison surface.
    """
    required_sections = ("signals", "trades", "equity", "metrics")
    missing_sections = [
        section
        for section in required_sections
        if section not in qsim_output or section not in old_output
    ]
    checks = [
        (
            "signals",
            compare_event_stream(
                qsim_output.get("signals", []),
                old_output.get("signals", []),
                keys=("date", "signal", "weight"),
                numeric_tolerances={"weight": 1e-9},
            ),
        ),
        (
            "trades",
            compare_event_stream(
                qsim_output.get("trades", []),
                old_output.get("trades", []),
                keys=("date", "symbol", "side", "shares", "price", "fee"),
                numeric_tolerances={"shares": 0.0, "price": 1e-6, "fee": 1e-2},
            ),
        ),
        (
            "equity",
            compare_equity(
                qsim_output.get("equity", pd.DataFrame()),
                old_output.get("equity", pd.DataFrame()),
                cash_tol=equity_cash_tol,
            ),
        ),
        (
            "metrics",
            compare_metrics(
                qsim_output.get("metrics", {}),
                old_output.get("metrics", {}),
                tolerances=metric_tolerances,
            ),
        ),
    ]
    differences: dict[str, float] = {}
    details: list[str] = []
    if missing_sections:
        details.append(f"missing output sections: {', '.join(missing_sections)}")
        differences["missing_sections"] = float(len(missing_sections))
    for section, result in checks:
        for key, value in result.differences.items():
            differences[f"{section}.{key}"] = value
        details.extend(f"{section}: {detail}" for detail in result.details)
    return ShadowDiffResult(not details, differences, details)
