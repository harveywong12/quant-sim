"""Quarterly strategy review decisions.

The review is deliberately mechanical: promote / keep / demote are based on a
small set of measurable criteria, not subjective judgement.  When per-config
return series are supplied, decisions are additionally gated by bootstrap
confidence intervals and the scan-level PBO (probability of backtest
overfitting): a promote is downgraded to keep when the selection process is
statistically indistinguishable from overfitting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np

from qsim.governance.overfitting import bootstrap_ci, pbo

BOOTSTRAP_SEED = 42


@dataclass(frozen=True, slots=True)
class StrategyMetrics:
    name: str
    return_pct: float
    max_drawdown_pct: float
    sharpe: float
    correlation: float = 0.0


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    name: str
    action: str  # promote | keep | demote
    reasons: list[str]
    pbo: float | None = None
    return_ci: tuple[float, float] | None = None


@dataclass(frozen=True, slots=True)
class ReviewThresholds:
    promote_return: float = 10.0
    demote_return: float = -5.0
    max_drawdown: float = -30.0
    min_sharpe: float = 0.3
    max_correlation: float = 0.80
    max_pbo: float = 0.5


def review_strategy(
    metrics: StrategyMetrics,
    thresholds: ReviewThresholds = ReviewThresholds(),
) -> ReviewDecision:
    reasons: list[str] = []
    if (
        metrics.return_pct >= thresholds.promote_return
        and metrics.sharpe >= thresholds.min_sharpe
        and metrics.max_drawdown_pct >= thresholds.max_drawdown
        and metrics.correlation <= thresholds.max_correlation
    ):
        return ReviewDecision(metrics.name, "promote", ["return and risk pass promote criteria"])

    if (
        metrics.return_pct <= thresholds.demote_return
        or metrics.max_drawdown_pct <= thresholds.max_drawdown
        or metrics.sharpe < thresholds.min_sharpe
        or metrics.correlation > thresholds.max_correlation
    ):
        if metrics.return_pct <= thresholds.demote_return:
            reasons.append(f"return {metrics.return_pct:.2f}% <= {thresholds.demote_return:.2f}%")
        if metrics.max_drawdown_pct <= thresholds.max_drawdown:
            reasons.append(f"drawdown {metrics.max_drawdown_pct:.2f}% <= {thresholds.max_drawdown:.2f}%")
        if metrics.sharpe < thresholds.min_sharpe:
            reasons.append(f"sharpe {metrics.sharpe:.2f} < {thresholds.min_sharpe:.2f}")
        if metrics.correlation > thresholds.max_correlation:
            reasons.append(f"correlation {metrics.correlation:.2f} > {thresholds.max_correlation:.2f}")
        return ReviewDecision(metrics.name, "demote", reasons)

    return ReviewDecision(metrics.name, "keep", ["within review thresholds"])


def summarize_experiments(
    records: Iterable[Mapping[str, Any]],
    thresholds: ReviewThresholds = ReviewThresholds(),
    returns_by_config: Mapping[str, Iterable[float]] | None = None,
) -> list[tuple[str, dict[str, float], ReviewDecision]]:
    """Group auto-scan summary records by config and produce review decisions.

    ``returns_by_config`` maps a config name to its periodic (e.g. daily)
    return series.  When supplied, each decision is annotated with a
    bootstrap CI of the total return, and a scan-level PBO across the
    available configs is computed; a ``promote`` is downgraded to ``keep``
    when PBO exceeds ``thresholds.max_pbo`` (IS selection has no OOS skill)
    or when the return CI lower bound is not positive.
    """
    summaries: dict[str, list[dict[str, float]]] = {}
    for record in records:
        if record.get("kind") != "auto_scan_summary":
            continue
        config = str(record.get("config", ""))
        summaries.setdefault(config, []).append(record)

    pbo_value: float | None = None
    if returns_by_config:
        series = {
            name: np.asarray(list(values), dtype=float)
            for name, values in returns_by_config.items()
            if name in summaries
        }
        if len(series) >= 2:
            try:
                pbo_value = pbo(series).pbo
            except ValueError:
                pbo_value = None

    out: list[tuple[str, dict[str, float], ReviewDecision]] = []
    for config, items in sorted(summaries.items()):
        avg_return = sum(float(r.get("avg_return", 0.0)) for r in items) / len(items)
        avg_mdd = sum(float(r.get("avg_max_drawdown", 0.0)) for r in items) / len(items)
        avg_sharpe = sum(float(r.get("avg_sharpe", 0.0)) for r in items) / len(items)
        metrics = StrategyMetrics(
            name=config,
            return_pct=avg_return * 100.0,
            max_drawdown_pct=avg_mdd * 100.0,
            sharpe=avg_sharpe,
        )
        decision = review_strategy(metrics, thresholds)
        return_ci: tuple[float, float] | None = None
        if returns_by_config and config in returns_by_config:
            ci = bootstrap_ci(
                np.asarray(list(returns_by_config[config]), dtype=float),
                metric="total_return",
                seed=BOOTSTRAP_SEED,
            )
            return_ci = (ci.lower, ci.upper)
        reasons = list(decision.reasons)
        action = decision.action
        if action == "promote":
            if pbo_value is not None and pbo_value > thresholds.max_pbo:
                action = "keep"
                reasons.append(
                    f"PBO {pbo_value:.2f} > {thresholds.max_pbo:.2f}: "
                    "IS selection shows no OOS skill"
                )
            elif return_ci is not None and return_ci[0] <= 0.0:
                action = "keep"
                reasons.append(
                    f"return CI [{return_ci[0]:.2%}, {return_ci[1]:.2%}] "
                    "includes non-positive outcomes"
                )
        final = ReviewDecision(
            name=config,
            action=action,
            reasons=reasons,
            pbo=pbo_value,
            return_ci=return_ci,
        )
        out.append((config, {
            "avg_return": avg_return,
            "avg_max_drawdown": avg_mdd,
            "avg_sharpe": avg_sharpe,
        }, final))
    return out
