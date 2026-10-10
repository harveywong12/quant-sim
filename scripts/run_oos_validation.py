#!/usr/bin/env python3
"""Out-of-sample validation for the combined portfolio with adaptive strategy."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pandas as pd

from qsim.allocation.policy import default_policy
from qsim.allocation.regime import SimpleRegimeDetector
from qsim.engine.costs import CostModel
from qsim.engine.combined_runner import (
    CrossSleeve,
    RotationSleeve,
    run_combined_portfolio_backtest,
)
from qsim.engine.portfolio_runner import Sleeve
from qsim.features.alpha158 import compute_alpha158_ic_scores
from qsim.features.factor import compute_factor_scores
from qsim.market.data_source import SqliteDataSource
from qsim.strategies.cross_sectional import Alpha158Strategy, MultiFactorStrategy
from qsim.strategies.builtin.adaptive import AdaptiveRotator
from qsim.strategies.builtin.dual_ma import DualMAStrategy
from qsim.strategies.builtin.grid_atr import GridATRStrategy
from qsim.strategies.builtin.rsi_boll import RsiBollStrategy
from qsim.strategies.builtin.tsmom import TsmomVolStrategy
from qsim.strategies.rotation import MomentumRotation, TrendFallback

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "quant.db"
ETF_COST = CostModel(
    commission_rate=0.0001,
    min_commission=0.1,
    stamp_tax=0.0,
    slippage=0.0005,
)


def _load_stock_panels(start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        bars = pd.read_sql_query(
            "SELECT ts_code, date, open, close FROM stock_bars", conn
        )
    finally:
        conn.close()
    bars = bars[(bars["date"] >= start) & (bars["date"] <= end)]
    return (
        bars.pivot(index="date", columns="ts_code", values="open"),
        bars.pivot(index="date", columns="ts_code", values="close"),
    )


def _run(start: str, end: str, label: str) -> dict:
    source = SqliteDataSource(DB, table="bars", symbol_column="symbol")
    stock_open, stock_close = _load_stock_panels(start, end)
    dates = sorted(stock_close.index)
    rb = []
    last = None
    for d in dates:
        if d[:6] != last:
            rb.append(d)
            last = d[:6]

    print(f"computing scores for {label}...")
    factor_scores = compute_factor_scores(DB, rb)
    alpha_scores = compute_alpha158_ic_scores(DB, rb)

    single_sleeves = [
        Sleeve("tsmom_vol", "513100", TsmomVolStrategy()),
        Sleeve("dual_ma", "510300", DualMAStrategy()),
        Sleeve("grid_atr", "512100", GridATRStrategy()),
        Sleeve("rsi_boll", "518880", RsiBollStrategy()),
        Sleeve("adaptive", "510500", AdaptiveRotator()),
    ]
    cross_sleeves = [
        CrossSleeve(
            "multi_factor",
            MultiFactorStrategy(top_n=10),
            lambda d: factor_scores.loc[d] if d in factor_scores.index else pd.Series(dtype=float),
        ),
        CrossSleeve(
            "alpha158",
            Alpha158Strategy(top_n=10),
            lambda d: alpha_scores.get(d, pd.Series(dtype=float)),
        ),
    ]
    rotation_sleeves = [
        RotationSleeve(
            "momentum_rotation",
            MomentumRotation(lookback=20),
            ("510300", "510500", "512100", "518880", "511380"),
        ),
        RotationSleeve(
            "trend_fallback",
            TrendFallback(symbol="518880", ma=60, fallback="511380"),
            ("518880", "511380"),
        ),
    ]

    t0 = time.perf_counter()
    result = run_combined_portfolio_backtest(
        single_sleeves=single_sleeves,
        cross_sleeves=cross_sleeves,
        rotation_sleeves=rotation_sleeves,
        policy=default_policy(),
        data_source=source,
        stock_open=stock_open,
        stock_close=stock_close,
        regime_detector=SimpleRegimeDetector(),
        initial_cash=1_000_000.0,
        cost=ETF_COST,
        start=start,
        end=end,
        market_symbol="510300",
        assumed_alpha=0.02,
    )
    elapsed = time.perf_counter() - t0
    m = result["metrics"]
    print(f"=== {label} ===")
    print(f"elapsed={elapsed:.2f}s bars={len(result['equity'])} trades={len(result['trades'])}")
    print(f"final={m['final_value']:.2f} return={m['total_return']:.4%} "
          f"mdd={m['max_drawdown']:.4%} sharpe={m['sharpe']:.4f}")
    return m


def main() -> None:
    is_metrics = _run("20210101", "20231231", "in-sample 2021-2023")
    oos_metrics = _run("20240101", "20251231", "out-of-sample 2024-2025")
    print("\n=== Validation Summary ===")
    print(f"IS  return={is_metrics['total_return']:.4%} mdd={is_metrics['max_drawdown']:.4%} sharpe={is_metrics['sharpe']:.4f}")
    print(f"OOS return={oos_metrics['total_return']:.4%} mdd={oos_metrics['max_drawdown']:.4%} sharpe={oos_metrics['sharpe']:.4f}")


if __name__ == "__main__":
    main()
