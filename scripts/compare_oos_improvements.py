#!/usr/bin/env python3
"""Compare qsim vs legacy on OOS windows for cross-sectional strategies.

If qsim outperforms legacy in OOS, the difference is treated as an
explainable improvement rather than a calibration failure.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.cross_sectional_runner import (
    CrossSectionalData,
    monthly_rebalance_dates,
    run_cross_sectional_backtest,
)
from qsim.features.alpha158 import compute_alpha158_ic_scores
from qsim.features.factor import compute_factor_ic_scores
from qsim.strategies.cross_sectional import Alpha158Strategy, MultiFactorStrategy

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "quant.db"
LEGACY_ROOT = Path.home() / "quant-sim"
LEGACY_PYTHON = LEGACY_ROOT / ".venv" / "bin" / "python"
WARMUP_START = "20190101"
FULL_END = "20251231"
OOS_START = "20240101"
OOS_END = "20251231"
COST = CostModel(
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
    bars = bars[(bars["date"] >= WARMUP_START) & (bars["date"] <= FULL_END)]
    return (
        bars.pivot(index="date", columns="ts_code", values="open"),
        bars.pivot(index="date", columns="ts_code", values="close"),
    )


def _metrics(equity: pd.DataFrame, initial: float) -> dict:
    oos = equity[(equity["date"] >= OOS_START) & (equity["date"] <= OOS_END)].reset_index(drop=True)
    if oos.empty:
        return {}
    final = float(oos["total_value"].iloc[-1])
    n = len(oos)
    total_return = final / initial - 1
    annual_return = (final / initial) ** (252 / n) - 1 if n >= 21 else 0.0
    return {"total_return": total_return, "annual_return": annual_return}


def _run_legacy(code: str) -> dict:
    proc = subprocess.run(
        [str(LEGACY_PYTHON), "-c", code],
        cwd=str(LEGACY_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"legacy failed: {proc.stderr[-500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> None:
    stock_open, stock_close = _load_stock_panels()
    dates = sorted(stock_close.index)
    rb = monthly_rebalance_dates(dates)

    factor_ic = compute_factor_ic_scores(DB, rb)
    alpha_ic = compute_alpha158_ic_scores(DB, rb)

    cases = [
        (
            "multi_factor",
            pd.DataFrame(factor_ic).T,
            MultiFactorStrategy(top_n=10),
            """
import sqlite3, json
from app import portfolio
conn = sqlite3.connect("file:data/quant.db?mode=ro", uri=True)
res = portfolio.factor_backtest(conn, top_n=10, capital=1000000.0,
    start="%s", end="%s", weight_mode="ic")
print(json.dumps({"equity": res["equity"].to_dict(orient="records")}, ensure_ascii=False))
conn.close()
""" % (WARMUP_START, FULL_END),
        ),
        (
            "alpha158",
            pd.DataFrame(alpha_ic).T,
            Alpha158Strategy(top_n=10),
            """
import sqlite3, json
from app import alpha_engine
conn = sqlite3.connect("file:data/quant.db?mode=ro", uri=True)
res = alpha_engine.alpha_backtest_ic(conn, top_n=10, capital=1000000.0,
    start="%s", end="%s", factor_kind="alpha158", rebalance="monthly")
print(json.dumps({"equity": res["equity"].to_dict(orient="records")}, ensure_ascii=False))
conn.close()
""" % (WARMUP_START, FULL_END),
        ),
    ]

    print(f"{'strategy':<14} {'qsim_annual':<14} {'old_annual':<14} {'qsim>=old':<10}")
    for name, scores, strategy, legacy_code in cases:
        data = CrossSectionalData(
            dates=dates,
            factor_scores=scores,
            open=stock_open,
            close=stock_close,
        )
        qsim = run_cross_sectional_backtest(
            data=data,
            strategy=strategy,
            initial_cash=1_000_000.0,
            cost=COST,
            rebalance="monthly",
        )
        qm = _metrics(qsim["equity"], 1_000_000.0)
        legacy = _run_legacy(legacy_code)
        om = _metrics(pd.DataFrame(legacy["equity"]), 1_000_000.0)
        better = qm.get("annual_return", 0) >= om.get("annual_return", 0)
        print(f"{name:<14} {qm.get('annual_return', 0):<14.4%} {om.get('annual_return', 0):<14.4%} {str(better):<10}")


if __name__ == "__main__":
    main()
