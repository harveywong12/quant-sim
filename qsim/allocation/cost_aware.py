"""Cost-aware rebalance gate.

A rebalance is only executed when the expected gain is greater than the
estimated transaction cost plus an optional improvement buffer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def turnover(current: Mapping[str, float], target: Mapping[str, float]) -> float:
    keys = set(current) | set(target)
    value = sum(abs(target.get(k, 0.0) - current.get(k, 0.0)) for k in keys) / 2.0
    return round(value, 6)


@dataclass(frozen=True, slots=True)
class CostModel:
    commission_rate: float = 0.0003
    slippage: float = 0.0005
    stamp_tax: float = 0.0005
    min_commission: float = 5.0


def estimated_cost(
    t: float,
    portfolio_value: float,
    cost: CostModel,
) -> float:
    traded_value = t * portfolio_value
    commission = max(cost.min_commission, traded_value * cost.commission_rate)
    stamp = traded_value * cost.stamp_tax
    slippage_cost = traded_value * cost.slippage
    return commission + stamp + slippage_cost


def should_rebalance(
    current: Mapping[str, float],
    target: Mapping[str, float],
    expected_gain: float,
    portfolio_value: float,
    cost: CostModel,
    min_improvement: float = 0.0,
) -> tuple[bool, float, float]:
    """Return (do_rebalance, turnover, estimated_cost)."""
    t = turnover(current, target)
    if t <= 1e-12:
        return False, 0.0, 0.0
    est_cost = estimated_cost(t, portfolio_value, cost)
    do = expected_gain > est_cost + min_improvement
    return do, round(t, 6), round(est_cost, 2)
