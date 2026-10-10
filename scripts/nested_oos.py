#!/usr/bin/env python3
"""Nested out-of-sample validation.

Strict protocol:
  train segment      -> select candidate configs
  validation segment -> coarse screen selected configs
  final test segment -> run the chosen config exactly once

Also runs parameter sensitivity for the chosen config and reports a
Bonferroni-adjusted p-value for the final Sharpe.
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
    run_combined_portfolio_backtest,
)
from qsim.engine.portfolio_runner import Sleeve
from qsim.features.alpha158 import compute_alpha158_ic_scores
from qsim.features.factor import compute_factor_scores
from qsim.market.data_source import SqliteDataSource
from qsim.research.experiment_store import ExperimentStore
from qsim.risk.limits import RiskLimits
from qsim.research.experiments import multiple_testing_pvalue
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
WARMUP_START = "20190101"
FULL_END = "20251231"
TRAIN = ("20190101", "20221231")
VALIDATION = ("20230101", "20231231")
FINAL_TEST = ("20240101", "20251231")
ALPHA = 0.001
CONCENTRATED_BASE = {
    "up": {"tsmom_vol": 0.5, "rsi_boll": 0.2, "alpha158": 0.2, "trend_fallback": 0.1},
    "range": {"tsmom_vol": 0.3, "rsi_boll": 0.3, "alpha158": 0.2, "trend_fallback": 0.2},
    "down": {"tsmom_vol": 0.1, "rsi_boll": 0.2, "alpha158": 0.1, "trend_fallback": 0.6},
}
WEIGHT_CONFIGS = [
    "concentrated",
    "c_cross_up",
    "c_trend_up",
    "c_balanced",
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
    weights = {regime: dict(row) for regime, row in CONCENTRATED_BASE.items()}
    if name == "c_cross_up":
        for regime in weights:
            weights[regime]["alpha158"] = round(weights[regime].get("alpha158", 0.0) + 0.05, 4)
            weights[regime]["trend_fallback"] = max(0.0, weights[regime].get("trend_fallback", 0.0) - 0.05)
    elif name == "c_trend_up":
        for regime in weights:
            weights[regime]["tsmom_vol"] = round(weights[regime].get("tsmom_vol", 0.0) + 0.05, 4)
            weights[regime]["rsi_boll"] = round(weights[regime].get("rsi_boll", 0.0) + 0.05, 4)
            weights[regime]["alpha158"] = max(0.0, weights[regime].get("alpha158", 0.0) - 0.05)
            weights[regime]["trend_fallback"] = max(0.0, weights[regime].get("trend_fallback", 0.0) - 0.05)
    elif name == "c_balanced":
        for regime in weights:
            row = weights[regime]
            names = [k for k in row if row.get(k, 0.0) > 0]
            if names:
                avg = sum(row[k] for k in names) / len(names)
                for k in names:
                    row[k] = round(avg, 4)
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
        "num_days": n,
    }


def _build_sleeves(factor_scores, alpha_scores):
    source = SqliteDataSource(DB, table="bars", symbol_column="symbol")
    single_sleeves = [
        Sleeve("tsmom_vol", "513100", TsmomVolStrategy()),
        Sleeve("rsi_boll", "518880", RsiBollStrategy()),
    ]
    cross_sleeves = [
        CrossSleeve(
            "alpha158",
            Alpha158Strategy(top_n=10),
            lambda d: alpha_scores.get(d, pd.Series(dtype=float)),
        ),
    ]
    rotation_sleeves = [
        RotationSleeve(
            "trend_fallback",
            TrendFallback(symbol="518880", ma=60, fallback="511380"),
            ("518880", "511380"),
        ),
    ]
    return source, single_sleeves, cross_sleeves, rotation_sleeves


def _run_policy(policy, period_start, period_end, source, single_sleeves,
                cross_sleeves, rotation_sleeves, stock_open, stock_close):
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
        end=period_end,
        market_symbol="510300",
        assumed_alpha=ALPHA,
        risk_limits=RiskLimits(max_total_drawdown=-0.15, max_single_strategy_weight=0.6),
    )
    equity = result["equity"]
    oos = equity[(equity["date"] >= period_start) & (equity["date"] <= period_end)].reset_index(drop=True)
    m = _metrics_from_equity(oos, 1_000_000.0)
    return m, result


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
    store = ExperimentStore(EXPERIMENT_LOG)

    # 1) Train selection
    train_results = []
    for config in WEIGHT_CONFIGS:
        policy = _make_policy(config)
        m, _ = _run_policy(policy, TRAIN[0], TRAIN[1], source, single_sleeves,
                           cross_sleeves, rotation_sleeves, stock_open, stock_close)
        train_results.append((config, m))
        print(f"TRAIN {config}: ret={m['total_return']:.4%} mdd={m['max_drawdown']:.4%} sharpe={m['sharpe']:.4f}")
        store.record({"kind": "nested_train", "config": config, **m})

    train_results.sort(key=lambda x: x[1]["sharpe"], reverse=True)
    selected = [c for c, _ in train_results[:2]]
    print("selected after train:", selected)

    # 2) Validation screening
    val_results = []
    for config in selected:
        policy = _make_policy(config)
        m, _ = _run_policy(policy, VALIDATION[0], VALIDATION[1], source, single_sleeves,
                           cross_sleeves, rotation_sleeves, stock_open, stock_close)
        val_results.append((config, m))
        print(f"VAL {config}: ret={m['total_return']:.4%} mdd={m['max_drawdown']:.4%} sharpe={m['sharpe']:.4f}")
        store.record({"kind": "nested_validation", "config": config, **m})

    val_results.sort(key=lambda x: x[1]["sharpe"], reverse=True)
    final_config = val_results[0][0]
    print("final config:", final_config)

    # 3) Final test (run once)
    final_policy = _make_policy(final_config)
    final_m, final_result = _run_policy(final_policy, FINAL_TEST[0], FINAL_TEST[1], source,
                                        single_sleeves, cross_sleeves, rotation_sleeves,
                                        stock_open, stock_close)
    print(f"FINAL {final_config}: ret={final_m['total_return']:.4%} mdd={final_m['max_drawdown']:.4%} "
          f"sharpe={final_m['sharpe']:.4f} days={final_m['num_days']}")
    store.record({"kind": "nested_final", "config": final_config, **final_m})

    # 4) Sensitivity on final test
    sensitivity = {"more_defensive": 1.15, "less_defensive": 0.85}
    sens_rows = []
    for label, factor in sensitivity.items():
        policy = _make_policy(final_config)
        for regime in policy.regime_weights:
            row = dict(policy.regime_weights[regime])
            for key in ("trend_fallback", "cb_dual_low"):
                if key in row:
                    row[key] = round(row[key] * factor, 4)
            policy.regime_weights[regime] = row  # type: ignore[index]
        m, _ = _run_policy(policy, FINAL_TEST[0], FINAL_TEST[1], source, single_sleeves,
                           cross_sleeves, rotation_sleeves, stock_open, stock_close)
        sens_rows.append((label, m))
        print(f"SENS {label}: ret={m['total_return']:.4%} mdd={m['max_drawdown']:.4%} sharpe={m['sharpe']:.4f}")
        store.record({"kind": "nested_sensitivity", "config": final_config, "sensitivity": label, **m})

    # 5) Multiple testing correction
    num_trials = len(WEIGHT_CONFIGS)
    pvalue = multiple_testing_pvalue(final_m["sharpe"], final_m["num_days"], num_trials)
    print("\n=== Nested OOS Summary ===")
    print(f"final_config={final_config}")
    print(f"final_sharpe={final_m['sharpe']:.4f}")
    print(f"num_trials={num_trials}")
    print(f"bonferroni_p={pvalue if pvalue is None else round(pvalue, 6)}")
    print("sensitivity:")
    for label, m in sens_rows:
        print(f"  {label}: sharpe={m['sharpe']:.4f}")


if __name__ == "__main__":
    main()
