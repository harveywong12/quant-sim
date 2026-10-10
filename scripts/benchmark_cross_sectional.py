#!/usr/bin/env python3
"""Benchmark the generic cross-sectional backtest runner on synthetic panels."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.cross_sectional_runner import (
    CrossSectionalData,
    run_cross_sectional_backtest,
)
from qsim.strategies.cross_sectional import TopNScoreStrategy


def generate_data(
    n_dates: int = 1000,
    n_stocks: int = 500,
    seed: int = 42,
) -> CrossSectionalData:
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2020-01-01")
    dates = [(start + pd.Timedelta(days=i)).strftime("%Y%m%d") for i in range(n_dates)]

    stocks = [f"S{i:04d}" for i in range(n_stocks)]
    scores = pd.DataFrame(
        rng.normal(size=(n_dates, n_stocks)),
        index=dates,
        columns=stocks,
    )
    open_prices = pd.DataFrame(
        10.0 + rng.random((n_dates, n_stocks)),
        index=dates,
        columns=stocks,
    )
    close_prices = open_prices * (1.0 + rng.normal(0, 0.01, size=(n_dates, n_stocks)))
    return CrossSectionalData(
        dates=dates,
        factor_scores=scores,
        open=open_prices,
        close=close_prices,
    )


def main() -> None:
    data = generate_data()
    strategy = TopNScoreStrategy(top_n=20)

    start = time.perf_counter()
    result = run_cross_sectional_backtest(
        data=data,
        strategy=strategy,
        initial_cash=1_000_000.0,
        cost=CostModel(),
        rebalance="monthly",
    )
    elapsed = time.perf_counter() - start

    metrics = result["metrics"]
    print("=== Cross-Sectional Backtest Benchmark ===")
    print(f"dates: {len(data.dates)}")
    print(f"stocks: {len(data.factor_scores.columns)}")
    print(f"trades: {len(result['trades'])}")
    print(f"elapsed: {elapsed:.4f}s")
    print(f"final_value: {metrics['final_value']:.2f}")
    print(f"total_return: {metrics['total_return']:.4%}")
    print(f"max_drawdown: {metrics['max_drawdown']:.4%}")


if __name__ == "__main__":
    main()
