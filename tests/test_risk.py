from __future__ import annotations

from qsim.risk.limits import RiskLimits, check_risk


def test_risk_ok() -> None:
    result = check_risk({"a": 0.3, "b": 0.3}, current_drawdown=-0.05, turnover=0.2)
    assert result.ok is True
    assert result.violations == []


def test_risk_detects_overweight() -> None:
    result = check_risk({"a": 0.8}, limits=RiskLimits(max_single_strategy_weight=0.4))
    assert result.ok is False
    assert any("weight" in v for v in result.violations)


def test_risk_detects_drawdown() -> None:
    result = check_risk({"a": 0.3}, current_drawdown=-0.20)
    assert result.ok is False
    assert any("drawdown" in v for v in result.violations)
