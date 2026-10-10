#!/usr/bin/env python3
"""Aggressive weight scan: reduce defensive weights, boost trend/cross."""

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
START = "20160801"
END = "20251231"
ETF_COST = CostModel(
    commission_rate=0.0001,
    min_commission=0.1,
    stamp_tax=0.0,
    slippage=0.0005,
)
EXPERIMENT_LOG = ROOT / "artifacts" / "experiments.jsonl"
CONFIGS = [
    "defensive_heavy",
    "aggressive_cross",
    "aggressive_trend",
    "no_defensive",
    "high_equity",
]


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


def _make_policy(name: str) -> RegimeAllocationPolicy:
    weights = {regime: dict(row) for regime, row in DEFAULT_REGIME_WEIGHTS.items()}

    def bump(regime, key, amount):
        weights[regime][key] = max(0.0, weights[regime].get(key, 0.0) + amount)

    def cut(regime, key, amount):
        weights[regime][key] = max(0.0, weights[regime].get(key, 0.0) - amount)

    if name == "aggressive_cross":
        for regime in weights:
            bump(regime, "multi_factor", 0.10)
            bump(regime, "alpha158", 0.10)
            bump(regime, "tsmom_vol", 0.05)
            bump(regime, "dual_ma", 0.05)
            cut(regime, "trend_fallback", 0.10)
            cut(regime, "cb_dual_low", 0.10)
            cut(regime, "grid_atr", 0.05)
            cut(regime, "rsi_boll", 0.05)
    elif name == "aggressive_trend":
        for regime in weights:
            bump(regime, "tsmom_vol", 0.10)
            bump(regime, "dual_ma", 0.10)
            bump(regime, "momentum_rotation", 0.10)
            bump(regime, "adaptive", 0.05)
            cut(regime, "trend_fallback", 0.10)
            cut(regime, "cb_dual_low", 0.10)
            cut(regime, "grid_atr", 0.05)
            cut(regime, "rsi_boll", 0.05)
    elif name == "no_defensive":
        for regime in weights:
            weights[regime]["trend_fallback"] = 0.0
            weights[regime]["cb_dual_low"] = 0.0
            bump(regime, "tsmom_vol", 0.10)
            bump(regime, "dual_ma", 0.10)
            bump(regime, "multi_factor", 0.10)
            bump(regime, "alpha158", 0.10)
    elif name == "high_equity":
        for regime in weights:
            weights[regime]["trend_fallback"] = 0.0
            weights[regime]["cb_dual_low"] = 0.0
            weights[regime]["tsmom_vol"] = 0.25
            weights[regime]["dual_ma"] = 0.20
            weights[regime]["momentum_rotation"] = 0.20
            weights[regime]["multi_factor"] = 0.20
            weights[regime]["alpha158"] = 0.15
            weights[regime]["adaptive"] = 0.00
            weights[regime]["grid_atr"] = 0.00
            weights[regime]["rsi_boll"] = 0.00
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
        "annual_return": annual_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "num_days": n,
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
    dates = sorted(stock_close.index)
    rb = []
    last = None
    for d in dates:
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
        start=START,
        end=END,
    )
    store = ExperimentStore(EXPERIMENT_LOG)

    rows = []
    for config in CONFIGS:
        policy = _make_policy(config)
        t0 = time.perf_counter()
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
            start=START,
            end=END,
            market_symbol="510300",
            assumed_alpha=0.0003,
            signal_timeline=timeline,
        )
        elapsed = time.perf_counter() - t0
        m = _metrics_from_equity(result["equity"], 1_000_000.0)
        rows.append((config, m))
        store.record({
            "kind": "aggressive_scan",
            "config": config,
            "start": START,
            "end": END,
            **m,
            "elapsed": round(elapsed, 2),
        })
        print(f"{config}: ret={m['total_return']:.4%} ann={m['annual_return']:.4%} "
              f"mdd={m['max_drawdown']:.4%} sharpe={m['sharpe']:.4f} ({elapsed:.1f}s)")

    print("\n=== Aggressive Scan Ranking (by annual return) ===")
    for config, m in sorted(rows, key=lambda x: x[1]["annual_return"], reverse=True):
        print(f"{config:<18} ann={m['annual_return']:.4%} ret={m['total_return']:.4%} "
              f"mdd={m['max_drawdown']:.4%} sharpe={m['sharpe']:.4f}")


if __name__ == "__main__":
    main()
