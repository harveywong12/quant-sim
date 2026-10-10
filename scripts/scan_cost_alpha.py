#!/usr/bin/env python3
"""Cost-aware parameter scan for the combined portfolio.

Scans the assumed rebalancing alpha (cost-gate sensitivity) and reports
out-of-sample 2024-2025 metrics.  Factor scores are computed once and reused.
"""

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
    precompute_signal_timeline,
    run_combined_portfolio_backtest,
)
from qsim.engine.portfolio_runner import Sleeve
from qsim.features.alpha158 import compute_alpha158_ic_scores
from qsim.features.factor import compute_factor_scores
from qsim.market.data_source import SqliteDataSource
from qsim.research.experiment_store import ExperimentStore
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
WARMUP_START = "20200101"
FULL_END = "20251231"
OOS_START = "20240101"
OOS_END = "20251231"
ALPHAS = [0.0002, 0.00025, 0.0003, 0.00035, 0.0004, 0.0005, 0.001]
EXPERIMENT_LOG = ROOT / "artifacts" / "experiments.jsonl"


def _load_stock_panels() -> tuple[pd.DataFrame, pd.DataFrame]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        bars = pd.read_sql_query(
            "SELECT ts_code, date, open, close FROM stock_bars", conn
        )
    finally:
        conn.close()
    bars = bars[(bars["date"] >= WARMUP_START) & (bars["date"] <= FULL_END)]
    return (
        bars.pivot(index="date", columns="ts_code", values="open"),
        bars.pivot(index="date", columns="ts_code", values="close"),
    )


def _metrics_from_equity(equity: pd.DataFrame, initial: float) -> dict:
    if equity.empty:
        return {}
    final = float(equity["total_value"].iloc[-1])
    n = len(equity)
    total_return = final / initial - 1
    annual_return = (final / initial) ** (252 / n) - 1 if n >= 21 else 0.0
    cummax = equity["total_value"].cummax()
    max_drawdown = float((equity["total_value"] / cummax - 1).min())
    ret = equity["total_value"].pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * (252 ** 0.5)) if len(ret) > 1 and ret.std() > 0 else 0.0
    return {
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "num_days": n,
    }


def main() -> None:
    stock_open, stock_close = _load_stock_panels()
    all_dates = sorted(stock_close.index)
    rb = []
    last = None
    for d in all_dates:
        if d[:6] != last:
            rb.append(d)
            last = d[:6]

    print("computing factor scores once...")
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

    store = ExperimentStore(EXPERIMENT_LOG)
    timeline = precompute_signal_timeline(
        single_sleeves=single_sleeves,
        cross_sleeves=cross_sleeves,
        rotation_sleeves=rotation_sleeves,
        data_source=source,
        stock_open=stock_open,
        stock_close=stock_close,
        regime_detector=SimpleRegimeDetector(),
        market_symbol="510300",
        start=WARMUP_START,
        end=FULL_END,
    )

    print(f"{'alpha':<8} {'return':<10} {'mdd':<10} {'sharpe':<8} {'days':<6} {'skips':<8}")
    print("-" * 60)
    for alpha in ALPHAS:
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
            start=WARMUP_START,
            end=FULL_END,
            market_symbol="510300",
            assumed_alpha=alpha,
            signal_timeline=timeline,
        )
        elapsed = time.perf_counter() - t0
        equity = result["equity"]
        oos = equity[(equity["date"] >= OOS_START) & (equity["date"] <= OOS_END)].reset_index(drop=True)
        m = _metrics_from_equity(oos, 1_000_000.0)
        print(f"{alpha:<8.4f} {m['total_return']:<10.4%} {m['max_drawdown']:<10.4%} "
              f"{m['sharpe']:<8.4f} {m['num_days']:<6} {len(result['skips']):<8} ({elapsed:.1f}s)")
        store.record({
            "kind": "cost_scan_summary",
            "assumed_alpha": alpha,
            "oos_start": OOS_START,
            "oos_end": OOS_END,
            "return": m["total_return"],
            "max_drawdown": m["max_drawdown"],
            "sharpe": m["sharpe"],
            "trades": len(result["trades"]),
            "skips": len(result["skips"]),
        })


if __name__ == "__main__":
    main()
