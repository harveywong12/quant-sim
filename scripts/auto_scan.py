#!/usr/bin/env python3
"""Automated portfolio weight and cost parameter scan with rolling OOS.

Demo grid:
  weight configs: default, momentum_heavy
  assumed_alpha:  0.0003
  OOS years:      2024, 2025

Each configuration is evaluated on each OOS year with a warm-up start.
Results are ranked by average OOS Sharpe.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pandas as pd

from qsim.allocation.policy import RegimeAllocationPolicy, DEFAULT_REGIME_WEIGHTS
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
OOS_YEARS = [2023, 2024, 2025]
ALPHAS = [0.0003]
WEIGHT_CONFIGS = [
    "default",
    "momentum_heavy",
    "defensive_heavy",
    "adaptive_heavy",
    "balanced",
    "defensive_max",
    "trend_heavy",
]
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


def _make_policy(name: str) -> RegimeAllocationPolicy:
    weights = {regime: dict(row) for regime, row in DEFAULT_REGIME_WEIGHTS.items()}
    if name == "momentum_heavy":
        for regime in weights:
            weights[regime]["momentum_rotation"] = min(
                0.30, weights[regime].get("momentum_rotation", 0.0) + 0.10
            )
            weights[regime]["tsmom_vol"] = max(0.0, weights[regime].get("tsmom_vol", 0.0) - 0.05)
            weights[regime]["dual_ma"] = max(0.0, weights[regime].get("dual_ma", 0.0) - 0.05)
    elif name == "defensive_heavy":
        for regime in weights:
            weights[regime]["trend_fallback"] = min(
                0.40, weights[regime].get("trend_fallback", 0.0) + 0.10
            )
            weights[regime]["cb_dual_low"] = min(
                0.40, weights[regime].get("cb_dual_low", 0.0) + 0.10
            )
            weights[regime]["grid_atr"] = max(0.0, weights[regime].get("grid_atr", 0.0) - 0.05)
            weights[regime]["rsi_boll"] = max(0.0, weights[regime].get("rsi_boll", 0.0) - 0.05)
    elif name == "adaptive_heavy":
        for regime in weights:
            weights[regime]["adaptive"] = min(
                0.20, weights[regime].get("adaptive", 0.0) + 0.10
            )
            weights[regime]["tsmom_vol"] = max(0.0, weights[regime].get("tsmom_vol", 0.0) - 0.05)
            weights[regime]["dual_ma"] = max(0.0, weights[regime].get("dual_ma", 0.0) - 0.05)
    elif name == "balanced":
        # Rebalance toward equal weights within each regime family.
        for regime in weights:
            row = weights[regime]
            names = [k for k in row if row.get(k, 0.0) > 0]
            if names:
                avg = sum(row[k] for k in names) / len(names)
                for k in names:
                    row[k] = round(avg, 4)
    elif name == "defensive_max":
        for regime in weights:
            weights[regime]["trend_fallback"] = min(
                0.45, weights[regime].get("trend_fallback", 0.0) + 0.15
            )
            weights[regime]["cb_dual_low"] = min(
                0.45, weights[regime].get("cb_dual_low", 0.0) + 0.15
            )
            weights[regime]["grid_atr"] = max(0.0, weights[regime].get("grid_atr", 0.0) - 0.10)
            weights[regime]["rsi_boll"] = max(0.0, weights[regime].get("rsi_boll", 0.0) - 0.10)
    elif name == "trend_heavy":
        for regime in weights:
            weights[regime]["tsmom_vol"] = min(
                0.30, weights[regime].get("tsmom_vol", 0.0) + 0.10
            )
            weights[regime]["dual_ma"] = min(
                0.25, weights[regime].get("dual_ma", 0.0) + 0.10
            )
            weights[regime]["momentum_rotation"] = min(
                0.30, weights[regime].get("momentum_rotation", 0.0) + 0.10
            )
            weights[regime]["cb_dual_low"] = max(0.0, weights[regime].get("cb_dual_low", 0.0) - 0.05)
    return RegimeAllocationPolicy(weights)


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
    }


def _build_sleeves(factor_scores, alpha_scores):
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
    return source, single_sleeves, cross_sleeves, rotation_sleeves


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
    source, single_sleeves, cross_sleeves, rotation_sleeves = _build_sleeves(
        factor_scores, alpha_scores
    )
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
    store = ExperimentStore(EXPERIMENT_LOG)

    rows: list[dict] = []
    for config in WEIGHT_CONFIGS:
        policy = _make_policy(config)
        for alpha in ALPHAS:
            year_metrics = []
            for year in OOS_YEARS:
                test_start = f"{year}0101"
                test_end = f"{year}1231"
                result = run_combined_portfolio_backtest(
                    single_sleeves=single_sleeves,
                    cross_sleeves=cross_sleeves,
                    rotation_sleeves=rotation_sleeves,
                    policy=policy,
                    data_source=source,
                    stock_open=stock_open,
                    stock_close=stock_close,
                    regime_detector=SimpleRegimeDetector(),
                    initial_cash=1_000_000.0,
                    cost=ETF_COST,
                    start=WARMUP_START,
                    end=test_end,
                    market_symbol="510300",
                    assumed_alpha=alpha,
                    signal_timeline=timeline,
                )
                equity = result["equity"]
                oos = equity[(equity["date"] >= test_start) & (equity["date"] <= test_end)].reset_index(drop=True)
                m = _metrics_from_equity(oos, 1_000_000.0)
                year_metrics.append((year, m))
                if year == OOS_YEARS[-1]:
                    out_dir = ROOT / "artifacts" / "scan_equity"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    equity[["date", "total_value"]].to_csv(
                        out_dir / f"{config}__a{alpha}.csv", index=False
                    )
                store.record({
                    "kind": "auto_scan",
                    "config": config,
                    "assumed_alpha": alpha,
                    "oos_year": year,
                    "return": m["total_return"],
                    "max_drawdown": m["max_drawdown"],
                    "sharpe": m["sharpe"],
                    "trades": len(result["trades"]),
                    "skips": len(result["skips"]),
                })
                print(f"{config} alpha={alpha} OOS {year}: ret={m['total_return']:.4%} "
                      f"mdd={m['max_drawdown']:.4%} sharpe={m['sharpe']:.4f}")

            avg_sharpe = sum(m["sharpe"] for _, m in year_metrics) / len(year_metrics)
            avg_return = sum(m["total_return"] for _, m in year_metrics) / len(year_metrics)
            avg_mdd = sum(m["max_drawdown"] for _, m in year_metrics) / len(year_metrics)
            rows.append({
                "config": config,
                "alpha": alpha,
                "avg_return": avg_return,
                "avg_mdd": avg_mdd,
                "avg_sharpe": avg_sharpe,
            })
            store.record({
                "kind": "auto_scan_summary",
                "config": config,
                "assumed_alpha": alpha,
                "avg_return": avg_return,
                "avg_max_drawdown": avg_mdd,
                "avg_sharpe": avg_sharpe,
                "years": OOS_YEARS,
            })

    print("\n=== Auto Scan Ranking (by avg OOS Sharpe) ===")
    ranked = sorted(rows, key=lambda r: r["avg_sharpe"], reverse=True)
    print(f"{'config':<18} {'alpha':<8} {'avg_return':<12} {'avg_mdd':<12} {'avg_sharpe':<10}")
    for r in ranked:
        print(f"{r['config']:<18} {r['alpha']:<8.4f} {r['avg_return']:<12.4%} "
              f"{r['avg_mdd']:<12.4%} {r['avg_sharpe']:<10.4f}")


if __name__ == "__main__":
    main()
