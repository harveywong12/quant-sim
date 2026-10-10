#!/usr/bin/env python3
"""Compare three trading modes on the near-10-year period.

A: concentrated combined portfolio
B: independent multi-account (old-style)
C: concentrated portfolio with stricter risk control
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pandas as pd

from qsim.allocation.policy import RegimeAllocationPolicy
from qsim.allocation.regime import SimpleRegimeDetector
from qsim.engine.costs import CostModel
from qsim.engine.combined_runner import (
    CrossSleeve,
    RotationSleeve,
    run_combined_portfolio_backtest,
)
from qsim.engine.cross_sectional_runner import (
    CrossSectionalData,
    monthly_rebalance_dates,
    run_cross_sectional_backtest,
)
from qsim.engine.portfolio_runner import Sleeve
from qsim.engine.rotation_runner import run_rotation_backtest
from qsim.engine.runner import run_backtest
from qsim.features.alpha158 import compute_alpha158_ic_scores
from qsim.features.factor import compute_factor_scores
from qsim.market.data_source import SqliteDataSource
from qsim.risk.limits import RiskLimits
from qsim.strategies.cross_sectional import Alpha158Strategy, MultiFactorStrategy
from qsim.strategies.builtin.rsi_boll import RsiBollStrategy
from qsim.strategies.builtin.tsmom import TsmomVolStrategy
from qsim.strategies.rotation import TrendFallback

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

CONCENTRATED = RegimeAllocationPolicy({
    "up": {"tsmom_vol": 0.5, "rsi_boll": 0.2, "alpha158": 0.2, "trend_fallback": 0.1},
    "range": {"tsmom_vol": 0.3, "rsi_boll": 0.3, "alpha158": 0.2, "trend_fallback": 0.2},
    "down": {"tsmom_vol": 0.1, "rsi_boll": 0.2, "alpha158": 0.1, "trend_fallback": 0.6},
})


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
    }


def _combine_equities(equities: list[pd.DataFrame], initial_total: float) -> pd.DataFrame:
    merged = None
    for eq in equities:
        sub = eq[["date", "total_value"]].copy()
        if merged is None:
            merged = sub
        else:
            merged = merged.merge(sub, on="date", how="outer", suffixes=("", "_y"))
            merged["total_value"] = merged["total_value"].fillna(0) + merged["total_value_y"].fillna(0)
            merged = merged.drop(columns=["total_value_y"])
    merged = merged.sort_values("date").ffill().fillna(0)
    return merged


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

    results = {}

    # ---- A: concentrated combined portfolio ----
    print("running A: concentrated combined portfolio...")
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
    t0 = time.perf_counter()
    res_a = run_combined_portfolio_backtest(
        single_sleeves=single_sleeves,
        cross_sleeves=cross_sleeves,
        rotation_sleeves=rotation_sleeves,
        policy=CONCENTRATED,
        data_source=source,
        stock_open=stock_open,
        stock_close=stock_close,
        regime_detector=SimpleRegimeDetector(),
        initial_cash=1_000_000.0,
        cost=ETF_COST,
        start=START,
        end=END,
        market_symbol="510300",
        assumed_alpha=0.001,
    )
    results["A_concentrated"] = _metrics_from_equity(res_a["equity"], 1_000_000.0)
    print(f"A done {time.perf_counter()-t0:.1f}s")

    # ---- B: independent multi-account ----
    print("running B: independent multi-account...")
    acct_capital = 250_000.0
    eq_list = []

    bars_tsmom = source.get_bars("513100", start=START, end=END)
    res_tsmom = run_backtest(bars_tsmom, TsmomVolStrategy(), acct_capital, ETF_COST, "513100")
    eq_list.append(res_tsmom["equity"])

    bars_rsi = source.get_bars("518880", start=START, end=END)
    res_rsi = run_backtest(bars_rsi, RsiBollStrategy(), acct_capital, ETF_COST, "518880")
    eq_list.append(res_rsi["equity"])

    data_alpha = CrossSectionalData(
        dates=dates,
        factor_scores=pd.DataFrame(alpha_scores).T,
        open=stock_open,
        close=stock_close,
    )
    res_alpha = run_cross_sectional_backtest(
        data=data_alpha,
        strategy=Alpha158Strategy(top_n=10),
        initial_cash=acct_capital,
        cost=ETF_COST,
        rebalance="monthly",
    )
    eq_list.append(res_alpha["equity"])

    data_rotation = {
        "518880": source.get_bars("518880", start=START, end=END),
        "511380": source.get_bars("511380", start=START, end=END),
    }
    res_trend = run_rotation_backtest(
        data=data_rotation,
        strategy=TrendFallback(symbol="518880", ma=60, fallback="511380"),
        initial_cash=acct_capital,
        cost=ETF_COST,
    )
    eq_list.append(res_trend["equity"])

    combined_b = _combine_equities(eq_list, 1_000_000.0)
    results["B_independent"] = _metrics_from_equity(combined_b, 1_000_000.0)
    print("B done")

    # ---- C: concentrated + stricter risk control ----
    print("running C: concentrated + risk control...")
    t0 = time.perf_counter()
    res_c = run_combined_portfolio_backtest(
        single_sleeves=single_sleeves,
        cross_sleeves=cross_sleeves,
        rotation_sleeves=rotation_sleeves,
        policy=CONCENTRATED,
        data_source=source,
        stock_open=stock_open,
        stock_close=stock_close,
        regime_detector=SimpleRegimeDetector(),
        initial_cash=1_000_000.0,
        cost=ETF_COST,
        start=START,
        end=END,
        market_symbol="510300",
        assumed_alpha=0.001,
        risk_limits=RiskLimits(max_total_drawdown=-0.15, max_single_strategy_weight=0.6),
    )
    results["C_risk_control"] = _metrics_from_equity(res_c["equity"], 1_000_000.0)
    print(f"C done {time.perf_counter()-t0:.1f}s")

    print("\n=== Comparison ===")
    print(f"{'mode':<22} {'annual':<10} {'total':<10} {'mdd':<10} {'sharpe':<8}")
    for name, m in results.items():
        print(f"{name:<22} {m['annual_return']:<10.4%} {m['total_return']:<10.4%} "
              f"{m['max_drawdown']:<10.4%} {m['sharpe']:<8.4f}")


if __name__ == "__main__":
    main()
