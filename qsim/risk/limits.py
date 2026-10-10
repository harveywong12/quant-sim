"""Hard risk limits that override allocation and strategy signals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
import math


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_position_weight: float = 0.30
    max_single_strategy_weight: float = 0.40
    max_total_drawdown: float = -0.15
    max_turnover: float = 0.50


@dataclass(frozen=True, slots=True)
class RiskCheck:
    ok: bool
    violations: list[str]

    @property
    def reasons(self) -> list[str]:
        return self.violations


def check_risk(
    weights: Mapping[str, float],
    current_drawdown: float = 0.0,
    turnover: float = 0.0,
    limits: RiskLimits = RiskLimits(),
) -> RiskCheck:
    violations: list[str] = []
    if not math.isfinite(current_drawdown) or not math.isfinite(turnover):
        violations.append("nonfinite drawdown or turnover")
    if turnover < 0:
        violations.append("negative turnover")
    if any(not math.isfinite(weight) or weight < 0 for weight in weights.values()):
        violations.append("nonfinite or negative weights")
    elif sum(weights.values()) > 1 + 1e-10:
        violations.append("total weight exceeds 1")
    for strategy, weight in weights.items():
        if weight > limits.max_single_strategy_weight:
            violations.append(
                f"{strategy} weight {weight:.2f} > {limits.max_single_strategy_weight:.2f}"
            )
    if current_drawdown <= limits.max_total_drawdown:
        violations.append(
            f"drawdown {current_drawdown:.2f} <= {limits.max_total_drawdown:.2f}"
        )
    if turnover > limits.max_turnover:
        violations.append(f"turnover {turnover:.2f} > {limits.max_turnover:.2f}")
    return RiskCheck(ok=not violations, violations=violations)
