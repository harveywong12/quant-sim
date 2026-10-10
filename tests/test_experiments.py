from __future__ import annotations

from qsim.research.experiments import Candidate, GateThresholds, evaluate_candidate


def test_gate_passes_qualified_candidate() -> None:
    candidate = Candidate(
        name="good",
        oos_return=0.10,
        after_cost_return=0.06,
        max_correlation=0.50,
    )
    result = evaluate_candidate(candidate)
    assert result.passed is True
    assert result.failures == []


def test_gate_rejects_low_oos() -> None:
    candidate = Candidate(
        name="bad_oos",
        oos_return=0.01,
        after_cost_return=0.06,
        max_correlation=0.50,
    )
    result = evaluate_candidate(candidate)
    assert result.passed is False
    assert any("oos_return" in reason for reason in result.reasons)


def test_gate_rejects_high_correlation() -> None:
    candidate = Candidate(
        name="bad_corr",
        oos_return=0.10,
        after_cost_return=0.06,
        max_correlation=0.90,
    )
    result = evaluate_candidate(candidate, GateThresholds(max_correlation=0.70))
    assert result.passed is False
    assert any("correlation" in reason for reason in result.reasons)
