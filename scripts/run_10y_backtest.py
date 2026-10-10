#!/usr/bin/env python3
"""Run a near-10-year combined portfolio backtest with the formal default config."""

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
START = "20160801"
END = "20251231"
ETF_COST = CostModel(
    commission_rate=0.0001,
    min_commission=0.1,
    stamp_tax=0.0,
    slippage=0.0005,
)


def _load_stock_panels() -> tuple[pd.DataFrame, pd.DataFrame]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        bars = pd.read_sql_query(
            "SELECT ts_code, date, open, close FROM stock_bars", conn
        )
    finally:
        conn.close()
    bars = bars[(bars["date"] >= START) & (bars["date"] <= END)]
    return (
        bars.pivot(index="date", columns="ts_code", values="open"),
        bars.pivot(index="date", columns="ts_code", values="close"),
    )


def main() -> None:
    stock_open, stock_close = _load_stock_panels()
    dates = sorted(stock_close.index)
    rb = []
    last = None
    for d in dates:
        if d[:6] != last:
            rb.append(d)
            last = d[:6]

    print("computing factor scores...")
    factor_scores = compute_factor_scores(DB, rb)
    alpha_scores = compute_alpha158_ic_scores(DB, rb)

    source = SqliteDataSource(DB, table="bars", symbol_column="symbol")
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
        start=START,
        end=END,
        market_symbol="510300",
        assumed_alpha=0.0003,
    )
    elapsed = time.perf_counter() - t0
    m = result["metrics"]
    print("=== Legacy Portfolio Diagnostic: NOT_VERIFIED (not ten-year acceptance) ===")
    print(f"elapsed={elapsed:.2f}s")
    print(f"bars={len(result['equity'])}")
    print(f"trades={len(result['trades'])}")
    print(f"skips={len(result['skips'])}")
    print(f"final={m['final_value']:.2f}")
    print(f"return={m['total_return']:.4%}")
    print(f"annual_return={m['annual_return']:.4%}")
    print(f"mdd={m['max_drawdown']:.4%}")
    print(f"sharpe={m['sharpe']:.4f}")


if __name__ == "__main__":
    main()
