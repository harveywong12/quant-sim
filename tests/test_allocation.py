from __future__ import annotations

from qsim.allocation.cost_aware import CostModel, should_rebalance, turnover
from qsim.allocation.policy import RegimeAllocationPolicy, default_policy


def test_target_weights_from_probabilities() -> None:
    policy = RegimeAllocationPolicy(
        {
            "up": {"a": 0.6, "b": 0.4},
            "down": {"a": 0.2, "c": 0.8},
        }
    )
    weights = policy.target_weights({"up": 0.5, "down": 0.5})
    assert weights["a"] == 0.4
    assert weights["b"] == 0.2
    assert weights["c"] == 0.4


def test_default_policy_returns_weights() -> None:
    policy = default_policy()
    weights = policy.target_weights({"up": 0.5, "range": 0.3, "down": 0.2})
    assert weights
    assert sum(weights.values()) <= 1.0 + 1e-6


def test_turnover() -> None:
    assert turnover({"a": 0.6, "b": 0.4}, {"a": 0.4, "b": 0.6}) == 0.2


def test_cost_aware_gate() -> None:
    do, t, cost = should_rebalance(
        current={"a": 1.0},
        target={"a": 0.5, "b": 0.5},
        expected_gain=1000.0,
        portfolio_value=100000.0,
        cost=CostModel(),
    )
    assert do is True
    assert t == 0.5
    assert cost > 0


def test_cost_aware_gate_rejects_small_gain() -> None:
    do, _, _ = should_rebalance(
        current={"a": 1.0},
        target={"a": 0.5, "b": 0.5},
        expected_gain=1.0,
        portfolio_value=100000.0,
        cost=CostModel(),
    )
    assert do is False
