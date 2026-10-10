#!/usr/bin/env python3
"""Quick shadow diff for cb_dual_low only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.runner import run_backtest
from qsim.market.data_source import SqliteDataSource
from qsim.projections.shadow_diff import compare_equity, compare_metrics
from qsim.strategies.builtin.cb_dual_low import CbDualLow

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "quant.db"
LEGACY_ROOT = Path.home() / "quant-sim"
LEGACY_PYTHON = LEGACY_ROOT / ".venv" / "bin" / "python"
START = "20160801"
END = "20251231"
COST = CostModel(
    commission_rate=0.0001,
    min_commission=0.1,
    stamp_tax=0.0,
    slippage=0.0005,
)


def main() -> None:
    source = SqliteDataSource(DB, table="bars", symbol_column="symbol")
    bars = source.get_bars("511380", start=START, end=END)
    qsim = run_backtest(bars, CbDualLow(), 100000.0, COST, "511380")

    code = """
import sqlite3, json
from app import engine, db
from app.strategy_loader import load
conn = sqlite3.connect("file:data/quant.db?mode=ro", uri=True)
bars = db.get_bars(conn, "511380")
bars = bars[(bars["date"] >= "%s") & (bars["date"] <= "%s")].reset_index(drop=True)
cfg = {"initial_capital": 100000, "commission_rate": 0.0001,
       "min_commission": 0.1, "stamp_tax": 0.0, "slippage": 0.0005}
strategy = load("cb_dual_low", {})
res = engine.backtest(bars, cfg, strategy, symbol="511380")
print(json.dumps({"metrics": res["metrics"],
                   "equity": res["equity"].to_dict(orient="records")}, ensure_ascii=False))
conn.close()
""" % (START, END)
    proc = subprocess.run(
        [str(LEGACY_PYTHON), "-c", code],
        cwd=str(LEGACY_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        print("old error:", proc.stderr[-500:])
        raise SystemExit(1)
    old = json.loads(proc.stdout.strip().splitlines()[-1])
    md = compare_metrics(qsim["metrics"], old["metrics"])
    ed = compare_equity(qsim["equity"], pd.DataFrame(old["equity"]))
    print("cb_dual_low metric diff:", md.differences, "ok:", md.ok)
    print("cb_dual_low equity diff:", ed.differences, "ok:", ed.ok)
    if md.details:
        print("metric details:", md.details)
    if ed.details:
        print("equity details:", ed.details)


if __name__ == "__main__":
    main()
