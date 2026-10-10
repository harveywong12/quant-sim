#!/usr/bin/env python3
"""Pre-registered, read-only three-version v3/Alpha sleeve experiment."""

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
from qsim.engine.cross_sectional_runner import CrossSectionalData, monthly_rebalance_dates, run_cross_sectional_backtest
from qsim.features.alpha158 import compute_alpha158_ic_scores
from qsim.features.factor import compute_factor_ic_scores
from qsim.strategies.cross_sectional import Alpha158Strategy, MultiFactorStrategy
from qsim.strategies.market_gate import multi_factor_v3_allocations

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "quant.db"
OUT = ROOT / "artifacts" / "experiments" / "mf_v3_alpha_sleeves_20260901"
START, END, CAPITAL = "20160815", "20260814", 100000.0
COST = CostModel(round_fee=False)


def _metrics(equity: pd.DataFrame, initial: float, trades: list[dict]) -> dict:
    final = float(equity.total_value.iloc[-1])
    n = len(equity)
    ret = equity.total_value.pct_change().dropna()
    drawdown = equity.total_value / equity.total_value.cummax() - 1
    return {
        "initial": initial, "final_value": round(final, 2),
        "total_return": final / initial - 1,
        "annual_return": (final / initial) ** (252 / n) - 1 if n >= 21 else 0.0,
        "max_drawdown": float(drawdown.min()),
        "sharpe": float(ret.mean() / ret.std() * math.sqrt(252)) if len(ret) > 1 and ret.std() > 0 else 0.0,
        "num_trades": len(trades),
        "turnover": sum(float(t.get("amount") or 0.0) for t in trades) / initial,
        "cost_drag": sum(float(t.get("fee") or 0.0) for t in trades) / initial,
        "days": n,
    }


def _combine(name: str, left: dict, right: dict | None = None) -> dict:
    frames = [left["equity"].set_index("date")]
    if right is not None:
        frames.append(right["equity"].set_index("date"))
    joined = pd.concat(frames, axis=1, keys=range(len(frames))).sort_index()
    equity = pd.DataFrame({
        "date": joined.index,
        "cash": sum(joined[key]["cash"] for key in range(len(frames))),
        "position_value": sum(joined[key]["position_value"] for key in range(len(frames))),
        "total_value": sum(joined[key]["total_value"] for key in range(len(frames))),
    })
    trades: list[dict] = []
    for sleeve, result in (("v3", left), ("alpha158", right)):
        if result is None:
            continue
        trades.extend({**trade, "sleeve": sleeve} for trade in result["trades"])
    return {"name": name, "equity": equity, "trades": trades, "metrics": _metrics(equity, CAPITAL, trades)}


def _yearly_oos(equity: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    for year in range(2020, 2026):
        frame = equity[equity.date.str[:4] == str(year)].copy()
        if len(frame) < 21:
            continue
        initial = float(frame.total_value.iloc[0])
        rows.append({"year": year, **_metrics(frame, initial, [])})
    return rows


def _alpha_quality(alpha_scores: pd.DataFrame, close: pd.DataFrame, signal_dates: list[str]) -> tuple[dict[str, bool], list[dict]]:
    """At each date, evaluate only composite-score ICs whose 20d labels settled."""
    positions = {date: i for i, date in enumerate(close.index.astype(str))}
    realized: dict[str, float] = {}
    for signal in signal_dates:
        pos = positions[signal]
        if pos + 20 >= len(close.index):
            continue
        label = close.iloc[pos + 20] / close.iloc[pos] - 1
        score = alpha_scores.loc[signal].dropna() if signal in alpha_scores.index else pd.Series(dtype=float)
        ic = score.corr(label.reindex(score.index), method="spearman")
        if pd.notna(ic):
            realized[signal] = float(ic)
    qualified: dict[str, bool] = {}
    evidence: list[dict] = []
    for signal in signal_dates:
        current_pos = positions[signal]
        settled = [s for s in signal_dates if s in realized and positions[s] + 20 <= current_pos]
        sample = [realized[s] for s in settled[-12:]]
        mean_ic = sum(sample) / len(sample) if sample else None
        ok = len(sample) >= 6 and mean_ic is not None and mean_ic > 0.0
        qualified[signal] = ok
        evidence.append({"signal_date": signal, "settled_observations": len(sample), "mean_composite_ic_20d": mean_ic, "alpha_quality_ok": ok})
    return qualified, evidence


def _prefix_check(rebalance_dates: list[str], full_scores: dict[str, pd.Series]) -> list[dict]:
    checks: list[dict] = []
    for cutoff, signal in (("20211231", "20210802"), ("20231229", "20230801")):
        prefix = compute_alpha158_ic_scores(DB, [d for d in rebalance_dates if d <= cutoff], as_of=cutoff)
        a, b = prefix.get(signal), full_scores.get(signal)
        common = a.index.intersection(b.index) if a is not None and b is not None else []
        max_abs = max((abs(float(a[x]) - float(b[x])) for x in common), default=float("inf"))
        checks.append({"cutoff": cutoff, "signal_date": signal, "common_symbols": len(common), "max_abs_score_delta": max_abs, "ok": bool(len(common) >= 30 and max_abs < 1e-12)})
    return checks


def _write_case(case: dict) -> dict:
    key = case["key"]
    case["equity"].to_csv(OUT / f"{key}_equity.csv", index=False)
    with (OUT / f"{key}_trades.csv").open("w", newline="", encoding="utf-8") as handle:
        rows = case["trades"]
        writer = csv.DictWriter(handle, fieldnames=sorted({k for row in rows for k in row}) or ["none"])
        writer.writeheader()
        writer.writerows(rows)
    return {k: v for k, v in case.items() if k not in {"equity", "trades"}}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        bars = pd.read_sql_query("SELECT ts_code,date,open,close FROM stock_bars WHERE date>=? AND date<=? ORDER BY date,ts_code", conn, params=[START, END])
        stock_open = bars.pivot(index="date", columns="ts_code", values="open")
        stock_close = bars.pivot(index="date", columns="ts_code", values="close")
        dates = sorted(stock_close.index.astype(str))
        rebalance_dates = monthly_rebalance_dates(dates)
        allocations, gate_records = multi_factor_v3_allocations(conn, dates, rebalance_dates)
    finally:
        conn.close()

    factor_dict = compute_factor_ic_scores(DB, rebalance_dates, as_of=END)
    alpha_dict = compute_alpha158_ic_scores(DB, rebalance_dates, as_of=END)
    prefix_checks = _prefix_check(rebalance_dates, alpha_dict)
    if not all(item["ok"] for item in prefix_checks):
        raise RuntimeError(f"Alpha PIT prefix check failed: {prefix_checks}")
    factor_data = CrossSectionalData(dates, pd.DataFrame(factor_dict).T, stock_open, stock_close)
    alpha_panel = pd.DataFrame(alpha_dict).T
    alpha_data = CrossSectionalData(dates, alpha_panel, stock_open, stock_close)
    alpha_ok, alpha_evidence = _alpha_quality(alpha_panel, stock_close, rebalance_dates)

    base_v3 = run_cross_sectional_backtest(factor_data, MultiFactorStrategy(top_n=10), CAPITAL, COST, rebalance_dates=rebalance_dates, allocation_by_signal_date=allocations, sizing="live_affordable")
    common_v3 = run_cross_sectional_backtest(factor_data, MultiFactorStrategy(top_n=10), CAPITAL * .75, COST, rebalance_dates=rebalance_dates, allocation_by_signal_date=allocations, sizing="live_affordable")
    common_alpha = run_cross_sectional_backtest(alpha_data, Alpha158Strategy(top_n=10), CAPITAL * .25, COST, rebalance_dates=rebalance_dates, allocation_by_signal_date=allocations, sizing="live_affordable")
    contrarian_alloc = {date: (allocation if allocation > 0 else (1.0 if alpha_ok.get(date, False) else 0.0)) for date, allocation in allocations.items()}
    contra_v3 = run_cross_sectional_backtest(factor_data, MultiFactorStrategy(top_n=10), CAPITAL * .75, COST, rebalance_dates=rebalance_dates, allocation_by_signal_date=allocations, sizing="live_affordable")
    contra_alpha = run_cross_sectional_backtest(alpha_data, Alpha158Strategy(top_n=10), CAPITAL * .25, COST, rebalance_dates=rebalance_dates, allocation_by_signal_date=contrarian_alloc, sizing="live_affordable")

    cases = [
        {"key": "v3_baseline", **_combine("A. multi_factor_v3 基线", base_v3), "rule": "100% v3，PIT 趋势+宽度+波动率门禁"},
        {"key": "common_gate_75_25", **_combine("B. 共同门禁 75/25", common_v3, common_alpha), "rule": "75% v3 + 25% Alpha158；两个袖套同用 v3 门禁"},
        {"key": "contrarian_75_25", **_combine("C. 有条件逆势 75/25", contra_v3, contra_alpha), "rule": "B 的规则；仅门禁为零且最近已结算 6--12 个 Alpha 综合分数 20 日 IC 均值>0 时，Alpha 袖套保持 25%"},
    ]
    summary_cases = []
    for case in cases:
        case["oos_calendar_2020_2025"] = _yearly_oos(case["equity"])
        summary_cases.append(_write_case(case))
    manifest = {
        "run_id": "mf_v3_alpha_sleeves_20260901", "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": START, "end": END, "capital": CAPITAL, "database": str(DB),
        "database_sha256": hashlib.sha256(DB.read_bytes()).hexdigest(), "future_data_used": False,
        "execution": "monthly close signal -> next trading-day open; board lots and affordability from shared cross-sectional runner",
        "pre_registered_versions": [case["rule"] for case in cases],
        "alpha_contrarian_quality": "at each signal date: mean of last <=12 settled, composite Alpha score cross-sectional Spearman ICs for 20-trading-day returns; require >=6 and mean>0",
    }
    payload = {"manifest": manifest, "prefix_checks": prefix_checks, "gate_records": gate_records, "alpha_quality_evidence": alpha_evidence, "cases": summary_cases}
    (OUT / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# multi_factor v3 × Alpha158：三版本冻结实验", "", f"窗口：{START} 至 {END}；初始资金：{CAPITAL:,.0f}；未来数据：否。", "", "| 版本 | 累计 | 年化 | 最大回撤 | 夏普 | 换手 | 费用 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for case in summary_cases:
        m = case["metrics"]
        lines.append(f"| {case['name']} | {m['total_return']:.2%} | {m['annual_return']:.2%} | {m['max_drawdown']:.2%} | {m['sharpe']:.2f} | {m['turnover']:.2f}x | {m['cost_drag']:.2%} |")
    lines.extend(["", "日历 OOS 为不调参的逐年切片；策略每月仅使用当日以前已结算的因子 IC 与市场数据。", ""])
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(OUT), "cases": [{"key": x["key"], **x["metrics"]} for x in summary_cases], "prefix_checks": prefix_checks}, ensure_ascii=False))


if __name__ == "__main__":
    main()
