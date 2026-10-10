"""Backtest-overfitting statistics: CSCV-PBO and percentile bootstrap CIs.

Pure numpy implementation of the Combinatorially Symmetric Cross-Validation
(CSCV) procedure from Bailey & Lopez de Prado, "The Probability of Backtest
Overfitting" (2014), plus percentile bootstrap confidence intervals for
common performance metrics.  No scipy/sklearn dependency.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable, Mapping

import numpy as np

TRADING_DAYS = 252


@dataclass(frozen=True, slots=True)
class PBOResult:
    pbo: float
    mean_logit: float
    n_combinations: int
    n_configs: int
    n_splits: int
    oos_ranks: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    metric: str
    point: float
    lower: float
    upper: float
    n_boot: int
    confidence: float


def sharpe(returns: np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    if r.size < 2:
        return 0.0
    std = r.std()
    if std <= 0:
        return 0.0
    return float(r.mean() / std * np.sqrt(TRADING_DAYS))


def total_return(returns: np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    if r.size == 0:
        return 0.0
    return float(np.prod(1.0 + r) - 1.0)


def max_drawdown(returns: np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    if r.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    return float((equity / peak - 1.0).min())


METRICS: Mapping[str, Callable[[np.ndarray], float]] = {
    "sharpe": sharpe,
    "total_return": total_return,
    "max_drawdown": max_drawdown,
}


def _annualized_sharpe_from_moments(total: np.ndarray, sq: np.ndarray, n: int) -> np.ndarray:
    mean = total / n
    var = np.maximum(sq / n - mean**2, 0.0)
    return mean / np.sqrt(var + 1e-20) * np.sqrt(TRADING_DAYS)


def pbo(
    returns_by_config: Mapping[str, np.ndarray],
    n_splits: int = 16,
) -> PBOResult:
    """Probability of backtest overfitting via CSCV.

    Each config's daily (or otherwise IID-ish periodic) returns are split
    into ``n_splits`` consecutive blocks.  For every choice of half the
    blocks as in-sample (IS), the config with the best IS Sharpe is
    "selected" and its out-of-sample (OOS) rank ``r`` (0 = worst,
    1 = best) is recorded.  PBO is the fraction of combinations with
    ``r < 0.5``, i.e. the selected config landed below the OOS median.
    PBO near 0.5 means selection has no skill; near 0 means robust skill.

    Block sums / sums of squares are precomputed per split so each
    combination is evaluated in O(1) per config regardless of block length.
    """
    names = list(returns_by_config)
    if len(names) < 2:
        raise ValueError("PBO requires at least 2 configs")
    if n_splits < 2 or n_splits % 2 != 0:
        raise ValueError("n_splits must be an even integer >= 2")
    try:
        matrix = np.asarray(
            [np.asarray(returns_by_config[name], dtype=float) for name in names]
        )
    except ValueError as exc:
        raise ValueError("all configs must have equal-length return series") from exc
    if matrix.ndim != 2 or matrix.shape[0] != len(names):
        raise ValueError("all configs must have equal-length return series")

    n_configs, n_periods = matrix.shape
    block_len = n_periods // n_splits
    if block_len < 1:
        raise ValueError(
            f"need at least {n_splits} periods, got {n_periods}"
        )
    matrix = matrix[:, : block_len * n_splits]
    blocks = matrix.reshape(n_configs, n_splits, block_len)
    block_sum = blocks.sum(axis=2)
    block_sq = (blocks**2).sum(axis=2)

    half = n_splits // 2
    n_is = block_len * half
    combos = np.fromiter(
        itertools.chain.from_iterable(itertools.combinations(range(n_splits), half)),
        dtype=int,
    ).reshape(-1, half)
    is_sum = block_sum[:, combos].sum(axis=2)
    is_sq = block_sq[:, combos].sum(axis=2)
    oos_sum = block_sum.sum(axis=1, keepdims=True) - is_sum
    oos_sq = block_sq.sum(axis=1, keepdims=True) - is_sq

    is_sharpe = _annualized_sharpe_from_moments(is_sum, is_sq, n_is)
    oos_sharpe = _annualized_sharpe_from_moments(oos_sum, oos_sq, n_periods - n_is)

    selected = np.argmax(is_sharpe, axis=0)
    col = np.arange(combos.shape[0])
    selected_oos = oos_sharpe[selected, col]
    below = (oos_sharpe < selected_oos).sum(axis=0)
    ranks = below / (n_configs - 1)

    pbo_value = float((ranks < 0.5).mean())
    with np.errstate(divide="ignore"):
        logits = np.log(ranks / (1.0 - ranks))
    finite = np.isfinite(logits)
    mean_logit = float(logits[finite].mean()) if finite.any() else 0.0
    return PBOResult(
        pbo=pbo_value,
        mean_logit=mean_logit,
        n_combinations=int(combos.shape[0]),
        n_configs=n_configs,
        n_splits=n_splits,
        oos_ranks=tuple(float(r) for r in ranks),
    )


def bootstrap_ci(
    returns: np.ndarray,
    metric: str = "sharpe",
    n_boot: int = 1000,
    confidence: float = 0.95,
    seed: int | None = None,
) -> BootstrapResult:
    """Percentile bootstrap CI of a performance metric for a return series."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    if r.size < 2:
        raise ValueError("need at least 2 return observations")
    rng = np.random.default_rng(seed)
    fn = METRICS[metric]
    boots = np.empty(n_boot)
    for b in range(n_boot):
        boots[b] = fn(r[rng.integers(0, r.size, r.size)])
    alpha = (1.0 - confidence) / 2.0
    lower, upper = np.quantile(boots, [alpha, 1.0 - alpha])
    return BootstrapResult(
        metric=metric,
        point=fn(r),
        lower=float(lower),
        upper=float(upper),
        n_boot=n_boot,
        confidence=confidence,
    )
