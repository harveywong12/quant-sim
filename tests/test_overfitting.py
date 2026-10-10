from __future__ import annotations

import numpy as np
import pytest

from qsim.governance.overfitting import (
    bootstrap_ci,
    max_drawdown,
    pbo,
    sharpe,
    total_return,
)
from qsim.governance.quarterly_review import summarize_experiments


def test_metric_helpers() -> None:
    assert sharpe(np.full(252, 0.5)) == 0.0
    r = np.concatenate([np.full(126, 0.002), np.full(126, -0.001)])
    expected = r.mean() / r.std() * np.sqrt(252)
    assert sharpe(r) == pytest.approx(expected)
    assert total_return(np.array([0.01, -0.01])) == pytest.approx(-0.0001)
    assert max_drawdown(np.array([0.1, -0.2])) == pytest.approx(-0.2)


def test_bootstrap_ci_is_seeded_and_contains_point() -> None:
    rng = np.random.default_rng(7)
    r = 0.01 + rng.normal(0, 0.005, 500)
    a = bootstrap_ci(r, metric="total_return", seed=42)
    b = bootstrap_ci(r, metric="total_return", seed=42)
    assert (a.lower, a.point, a.upper) == (b.lower, b.point, b.upper)
    assert a.lower <= a.point <= a.upper
    assert a.lower > 0
    with pytest.raises(ValueError):
        bootstrap_ci(r, metric="nope")
    with pytest.raises(ValueError):
        bootstrap_ci(np.array([0.01]))


def _with_trend(base: float, n: int, scale: float = 1e-4) -> np.ndarray:
    wave = np.sin(np.arange(n)) * scale
    return np.full(n, base) + wave


def test_pbo_zero_when_one_config_dominates() -> None:
    n = 120
    returns = {
        "A": _with_trend(0.002, n),
        "B": _with_trend(0.0002, n),
        "C": _with_trend(-0.0002, n),
    }
    result = pbo(returns, n_splits=4)
    assert result.pbo == 0.0
    assert result.n_combinations == 6


def test_pbo_one_when_selection_always_reverses() -> None:
    half = 100
    reverser = np.concatenate([np.full(half, 0.01), np.full(half, -0.01)])
    flat = np.full(2 * half, 0.0001)
    result = pbo({"reverser": reverser, "flat": flat}, n_splits=2)
    assert result.pbo == 1.0
    assert result.oos_ranks == (0.0, 0.0)


def test_pbo_no_skill_is_near_one_half() -> None:
    rng = np.random.default_rng(13)
    returns = {f"c{i}": rng.normal(0, 0.01, 400) for i in range(8)}
    result = pbo(returns, n_splits=8)
    assert 0.3 < result.pbo < 0.7


def test_pbo_validates_inputs() -> None:
    with pytest.raises(ValueError):
        pbo({"only": np.zeros(10)})
    with pytest.raises(ValueError):
        pbo({"a": np.zeros(10), "b": np.zeros(10)}, n_splits=3)
    with pytest.raises(ValueError):
        pbo({"a": np.zeros(10), "b": np.zeros(11)})
    with pytest.raises(ValueError):
        pbo({"a": np.zeros(4), "b": np.zeros(4)}, n_splits=16)


def _records(config: str, avg_return: float, avg_sharpe: float, avg_mdd: float = -0.05):
    return [{
        "kind": "auto_scan_summary",
        "config": config,
        "avg_return": avg_return,
        "avg_sharpe": avg_sharpe,
        "avg_max_drawdown": avg_mdd,
    }]


def _reversal_universe(n_configs: int = 16, block_len: int = 12, seed: int = 5) -> dict[str, np.ndarray]:
    n = n_configs * block_len
    out = {}
    for k in range(n_configs):
        rng = np.random.default_rng(seed + k)
        s = np.full(n, -0.002) + rng.normal(0, 0.001, n)
        s[k * block_len:(k + 1) * block_len] = 0.02 + rng.normal(0, 0.001, block_len)
        out[f"cfg{k}"] = s
    out["uniform"] = np.zeros(n)
    return out


def test_review_downgrades_promote_when_pbo_high() -> None:
    universe = _reversal_universe()
    records = []
    for name in universe:
        if name == "cfg0":
            records += _records(name, 0.20, 2.0)
        else:
            records += _records(name, 0.02, 0.2)

    plain = summarize_experiments(records)
    assert dict((c, d.action) for c, _, d in plain)["cfg0"] == "promote"

    guarded = summarize_experiments(records, returns_by_config=universe)
    decisions = dict((c, d) for c, _, d in guarded)
    assert decisions["cfg0"].pbo == 1.0
    assert decisions["cfg0"].action == "keep"
    assert any("PBO" in reason for reason in decisions["cfg0"].reasons)


def test_review_downgrades_promote_when_return_ci_includes_zero() -> None:
    noisy = np.tile([0.02, -0.0195], 200)
    records = _records("noisy", 0.15, 1.5)
    guarded = summarize_experiments(records, returns_by_config={"noisy": noisy})
    decision = guarded[0][2]
    assert decision.action == "keep"
    assert decision.return_ci is not None and decision.return_ci[0] <= 0.0
    assert any("CI" in reason for reason in decision.reasons)


def test_review_unchanged_without_returns() -> None:
    records = _records("solo", 0.15, 1.5)
    results = summarize_experiments(records)
    assert results[0][2].action == "promote"
    assert results[0][2].pbo is None
    assert results[0][2].return_ci is None
