#!/usr/bin/env python3
"""Read-only v3 STAR-board eligibility ablation on the frozen 10-year DB.

Compares the production v3 selection contract with an otherwise identical
variant that excludes STAR-board symbols (688*/689*) *before* the runner's
affordability backfill.  It does not modify paper/live configuration or data.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.cross_sectional_runner import (
    CrossSectionalData,
    monthly_rebalance_dates,
    run_cross_sectional_backtest,
)
from qsim.features.factor import compute_factor_ic_scores
from qsim.strategies.cross_sectional import CrossSectionContext, MultiFactorStrategy
from qsim.strategies.market_gate import multi_factor_v3_allocations


ROOT = Path(__file__).resolve().parents[1]
FROZEN_DB = Path("/srv/quant-sim/artifacts/experiments/ml_vs_ic_walkforward_20160815_20260815/quant.db")
OUT = ROOT / "artifacts" / "experiments" / "mf_v3_no_star_20260902"
START, END, OOS_START, CAPITAL = "20160815", "20260814", "20230103", 100000.0
COST = CostModel(round_fee=False)
STAR_PREFIXES = ("688", "689")


def _is_star(symbol: str) -> bool:
    return str(symbol).split(".", 1)[0].startswith(STAR_PREFIXES)


class NoStarMultiFactorStrategy(MultiFactorStrategy):
    """Production rank order, with STAR symbols removed before feasibility."""

    name = "multi_factor_v3_no_star"

    def rank(self, context: CrossSectionContext, limit: int | None = None) -> list[str]:
        ranked = super().rank(context, limit=None)
        eligible = [symbol for symbol in ranked if not _is_star(symbol)]
        return eligible[:limit] if limit is not None else eligible


def _metrics(equity: pd.DataFrame, initial: float, trades: list[dict]) -> dict:
    final = float(equity.total_value.iloc[-1])
    daily = equity.total_value.pct_change().dropna()
    drawdown = equity.total_value / equity.total_value.cummax() - 1
    exposure = equity.position_value / equity.total_value.replace(0, float("nan"))
    days = len(equity)
    return {
        "initial": round(initial, 2),
        "final_value": round(final, 2),
        "total_return": final / initial - 1,
        "annual_return": (final / initial) ** (252 / days) - 1 if days >= 21 else 0.0,
        "max_drawdown": float(drawdown.min()),
        "sharpe": float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0.0,
        "turnover": sum(float(t.get("amount") or 0.0) for t in trades) / initial,
        "cost_drag": sum(float(t.get("fee") or 0.0) for t in trades) / initial,
        "num_trades": len(trades),
        "mean_invested": float(exposure.mean()),
        "days": days,
        "window_start": str(equity.date.iloc[0]),
        "window_end": str(equity.date.iloc[-1]),
    }


def _slice(case: dict, start: str) -> dict:
    equity = case["equity"].loc[case["equity"].date >= start].copy()
    trades = [trade for trade in case["trades"] if str(trade.get("date")) >= start]
    return _metrics(equity, float(equity.total_value.iloc[0]), trades)


def _star_diagnostics(result: dict) -> dict:
    signal_slots = [symbol for signal in result["signals"] for symbol in json.loads(signal["signal"])]
    buy_trades = [t for t in result["trades"] if t.get("side") == "BUY"]
    star_buys = [t for t in buy_trades if _is_star(str(t.get("symbol", "")))]
    return {
        "signal_slots": len(signal_slots),
        "star_signal_slots": sum(_is_star(symbol) for symbol in signal_slots),
        "star_signal_slot_rate": (sum(_is_star(symbol) for symbol in signal_slots) / len(signal_slots)) if signal_slots else 0.0,
        "buy_trades": len(buy_trades),
        "star_buy_trades": len(star_buys),
        "star_buy_amount": round(sum(float(t.get("amount") or 0.0) for t in star_buys), 2),
    }


def _write_trades(path: Path, trades: list[dict]) -> None:
    columns = sorted({column for trade in trades for column in trade}) or ["none"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(trades)


def _write_case(key: str, result: dict) -> dict:
    result["equity"].to_csv(OUT / f"{key}_equity.csv", index=False)
    _write_trades(OUT / f"{key}_trades.csv", result["trades"])
    return {
        "key": key,
        "full": _metrics(result["equity"], CAPITAL, result["trades"]),
        "oos_20230103_20260814": _slice(result, OOS_START),
        "star_diagnostics": _star_diagnostics(result),
    }


def _delta(a: dict, b: dict) -> dict:
    """B minus A for return-style metrics; no-STAR minus baseline."""
    return {
        key: b["full"][key] - a["full"][key]
        for key in ("total_return", "annual_return", "max_drawdown", "sharpe", "turnover", "cost_drag", "mean_invested")
    }


def main() -> None:
    if not FROZEN_DB.is_file():
        raise FileNotFoundError(FROZEN_DB)
    OUT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{FROZEN_DB}?mode=ro", uri=True)
    try:
        bars = pd.read_sql_query(
            "SELECT ts_code,date,open,close FROM stock_bars WHERE date>=? AND date<=? ORDER BY date,ts_code",
            conn, params=[START, END],
        )
        stock_open = bars.pivot(index="date", columns="ts_code", values="open")
        stock_close = bars.pivot(index="date", columns="ts_code", values="close")
        dates = sorted(stock_close.index.astype(str))
        rebalance_dates = monthly_rebalance_dates(dates)
        allocations, gate_records = multi_factor_v3_allocations(conn, dates, rebalance_dates)
    finally:
        conn.close()

    # `as_of=END` keeps IC calibration constrained to observations settled by
    # the frozen cutoff; the runner still executes each monthly close signal at
    # the next available open.
    factor_scores = pd.DataFrame(compute_factor_ic_scores(FROZEN_DB, rebalance_dates, as_of=END)).T
    data = CrossSectionalData(dates, factor_scores, stock_open, stock_close)
    baseline = run_cross_sectional_backtest(
        data, MultiFactorStrategy(top_n=10), CAPITAL, COST,
        rebalance_dates=rebalance_dates, allocation_by_signal_date=allocations,
        sizing="live_affordable",
    )
    no_star = run_cross_sectional_backtest(
        data, NoStarMultiFactorStrategy(top_n=10), CAPITAL, COST,
        rebalance_dates=rebalance_dates, allocation_by_signal_date=allocations,
        sizing="live_affordable",
    )
    baseline_case = _write_case("v3_baseline", baseline)
    no_star_case = _write_case("v3_excluding_star", no_star)
    summary = {
        "run_id": "mf_v3_no_star_20260902",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "run_contract": {
            "frozen_db": str(FROZEN_DB),
            "frozen_db_sha256": hashlib.sha256(FROZEN_DB.read_bytes()).hexdigest(),
            "window": [START, END], "oos_window": [OOS_START, END], "capital": CAPITAL,
            "execution": "monthly close signal -> next trading-day open; shared v3 market gate, affordability backfill, board lots, costs and slippage",
            "only_change": "exclude symbols with 688*/689* code prefix before the identical ranked candidate set reaches affordability backfill",
            "future_data_used": False,
        },
        "gate_records": gate_records,
        "cases": [baseline_case, no_star_case],
        "no_star_minus_baseline": _delta(baseline_case, no_star_case),
    }
    (OUT / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# multi_factor v3：科创板可交易性消融", "",
        "同一冻结数据、同一 v3 门禁、月初收盘信号→下一交易日开盘成交、成本与 A 股整手/可买回填规则；唯一变化是候选排名中是否允许 688*/689*。", "",
        "| 版本 | 累计收益 | 年化 | 最大回撤 | 夏普 | 换手 | 平均仓位 |", "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for case in (baseline_case, no_star_case):
        m = case["full"]
        lines.append(f"| {case['key']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['max_drawdown']:.2%} | {m['sharpe']:.2f} | {m['turnover']:.2f}x | {m['mean_invested']:.2%} |")
    d = summary["no_star_minus_baseline"]
    lines.extend([
        "",
        f"剔除科创板 − 基线：累计 {d['total_return']:+.2%}，年化 {d['annual_return']:+.2%}，最大回撤 {d['max_drawdown']:+.2%}，夏普 {d['sharpe']:+.2f}，换手 {d['turnover']:+.2f}x。",
        "", "`metrics.json`、两组权益曲线和逐笔交易均已保留；本实验不更改实盘/模拟实盘策略配置。",
    ])
    (OUT / "metrics.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(OUT), "cases": [baseline_case, no_star_case], "delta": summary["no_star_minus_baseline"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
