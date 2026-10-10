"""Experiment admission gate.

A candidate strategy must pass three gates before entering the live pool:

1. out-of-sample return
2. after-cost return
3. correlation with existing strategies
"""

from __future__ import annotations

import math
from dataclasses import dataclass

try:
    from scipy.stats import norm
except ImportError:  # pragma: no cover
    norm = None


@dataclass(frozen=True, slots=True)
class GateThresholds:
    min_oos_return: float = 0.05
    min_after_cost_return: float = 0.03
    max_correlation: float = 0.70


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    oos_return: float
    after_cost_return: float
    max_correlation: float


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    failures: list[str]

    @property
    def reasons(self) -> list[str]:
        return self.failures


def evaluate_candidate(
    candidate: Candidate,
    thresholds: GateThresholds = GateThresholds(),
) -> GateResult:
    failures: list[str] = []
    for name in ('oos_return', 'after_cost_return', 'max_correlation'):
        value = getattr(candidate, name)
        if value is None or not math.isfinite(float(value)):
            failures.append(f"{name} undefined or nonfinite")
    if failures:
        return GateResult(passed=False, failures=failures)
    if candidate.oos_return < thresholds.min_oos_return:
        failures.append(
            f"oos_return {candidate.oos_return:.4f} < {thresholds.min_oos_return:.4f}"
        )
    if candidate.after_cost_return < thresholds.min_after_cost_return:
        failures.append(
            f"after_cost_return {candidate.after_cost_return:.4f} < "
            f"{thresholds.min_after_cost_return:.4f}"
        )
    if candidate.max_correlation > thresholds.max_correlation:
        failures.append(
            f"max_correlation {candidate.max_correlation:.4f} > "
            f"{thresholds.max_correlation:.4f}"
        )
    return GateResult(passed=not failures, failures=failures)


def multiple_testing_pvalue(
    sharpe: float,
    num_observations: int,
    num_trials: int,
) -> float | None:
    """Bonferroni-adjusted p-value for an observed Sharpe ratio.

    This is a conservative multiple-testing correction.  If scipy is not
    available, returns None.
    """
    if norm is None or sharpe <= 0 or num_observations <= 1 or num_trials <= 0:
        return None
    p_raw = 1.0 - norm.cdf(sharpe * math.sqrt(num_observations))
    return min(1.0, p_raw * num_trials)
