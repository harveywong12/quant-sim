#!/usr/bin/env python3
"""Run real-data cross-sectional backtests with multi-factor and Alpha158 scores."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.cross_sectional_runner import (
    CrossSectionalData,
    monthly_rebalance_dates,
    run_cross_sectional_backtest,
)
from qsim.features.alpha158 import compute_alpha158_ic_scores
from qsim.features.factor import compute_factor_scores
from qsim.strategies.cross_sectional import Alpha158Strategy, MultiFactorStrategy

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "quant.db"
START = "20210101"
END = "20251231"
TOP_N = 10


def _load_price_panels() -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        bars = pd.read_sql_query(
            "SELECT ts_code, date, open, close FROM stock_bars", conn
        )
    finally:
        conn.close()
    bars = bars[(bars["date"] >= START) & (bars["date"] <= END)]
    open_p = bars.pivot(index="date", columns="ts_code", values="open")
    close = bars.pivot(index="date", columns="ts_code", values="close")
    dates = sorted(close.index)
    return open_p, close, dates


def main() -> None:
    open_p, close, dates = _load_price_panels()
    rebalance_dates = monthly_rebalance_dates(dates)
    print(f"dates={len(dates)} rebalance_dates={len(rebalance_dates)}")

    for name, scores, strategy in [
        ("multi_factor", None, MultiFactorStrategy(top_n=TOP_N)),
        ("alpha158", None, Alpha158Strategy(top_n=TOP_N)),
    ]:
        t0 = time.perf_counter()
        if name == "multi_factor":
            scores = compute_factor_scores(DB, rebalance_dates)
        else:
            scores = pd.DataFrame(compute_alpha158_ic_scores(DB, rebalance_dates)).T
        compute_time = time.perf_counter() - t0

        data = CrossSectionalData(
            dates=dates,
            factor_scores=scores,
            open=open_p,
            close=close,
        )
        t1 = time.perf_counter()
        result = run_cross_sectional_backtest(
            data=data,
            strategy=strategy,
            initial_cash=1_000_000.0,
            cost=CostModel(),
            rebalance="monthly",
        )
        run_time = time.perf_counter() - t1
        m = result["metrics"]
        print(f"=== {name} ===")
        print(f"compute_time={compute_time:.2f}s run_time={run_time:.2f}s")
        print(f"trades={len(result['trades'])} final={m['final_value']:.2f} "
              f"ret={m['total_return']:.4%} mdd={m['max_drawdown']:.4%} sharpe={m['sharpe']:.4f}")


if __name__ == "__main__":
    main()
