#!/usr/bin/env python3
"""Generate comparable ten-year equity curves for the nine displayed strategies.

The output is a presentation artifact, not a paper account.  All input tables
are opened read-only and all curves use the same initial capital and explicit
execution date range.  The v3 curve uses the canonical IC scorer plus its
point-in-time trend/breadth/volatility gate.
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
from qsim.engine.rotation_runner import run_rotation_backtest
from qsim.engine.runner import run_backtest
from qsim.features.alpha158 import compute_alpha158_ic_scores
from qsim.features.factor import compute_factor_ic_scores
from qsim.market.data_source import SqliteDataSource
from qsim.strategies.builtin.cb_dual_low import CbDualLow
from qsim.strategies.builtin.dual_ma import DualMAStrategy
from qsim.strategies.builtin.grid_atr import GridATRStrategy
from qsim.strategies.builtin.rsi_boll import RsiBollStrategy
from qsim.strategies.builtin.tsmom import TsmomVolStrategy
from qsim.strategies.cross_sectional import Alpha158Strategy, MultiFactorStrategy, TopNScoreStrategy
from qsim.strategies.market_gate import multi_factor_v3_allocations
from qsim.strategies.rotation import TrendFallback

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "quant.db"
OUT = ROOT / "artifacts" / "experiments" / "strategy_curves_20160815_20260814"
START = "20160815"
END = "20260814"
CAPITAL = 100000.0
BENCHMARK = "510300"

SINGLE_COST = CostModel(
    commission_rate=0.0001, min_commission=0.1,
    stamp_tax=0.0, slippage=0.0005,
)
CROSS_COST = CostModel(round_fee=False)


def _load_stock_panels() -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        bars = pd.read_sql_query(
            "SELECT ts_code,date,open,close FROM stock_bars "
            "WHERE date>=? AND date<=? ORDER BY date,ts_code",
            conn, params=[START, END],
        )
    finally:
        conn.close()
    open_panel = bars.pivot(index="date", columns="ts_code", values="open")
    close_panel = bars.pivot(index="date", columns="ts_code", values="close")
    return open_panel, close_panel, sorted(close_panel.index.astype(str))


MOM_UNIVERSE = ["510300", "510500", "512100", "518880",
                "511010", "513100", "513030", "159915"]
CASH_PROXY = "511880"  # 货币 ETF：动量全负时的停靠资产


def _load_etf_panels() -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    symbols = MOM_UNIVERSE + [CASH_PROXY]
    marks = ",".join("?" for _ in symbols)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        bars = pd.read_sql_query(
            f"SELECT symbol,date,open,close FROM bars WHERE symbol IN ({marks}) "
            "AND date>=? AND date<=? ORDER BY date,symbol",
            conn, params=[*symbols, START, END],
        )
    finally:
        conn.close()
    open_panel = bars.pivot(index="date", columns="symbol", values="open")
    close_panel = bars.pivot(index="date", columns="symbol", values="close")
    return open_panel, close_panel, sorted(close_panel.index.astype(str))


def _build_momentum_scores(close: pd.DataFrame, dates: list[str],
                           lookbacks: tuple[int, ...] = (63, 252)) -> pd.DataFrame:
    mom = sum(close / close.shift(lb) - 1 for lb in lookbacks) / len(lookbacks)
    rows = {d: mom.loc[d].dropna() for d in dates if d in mom.index}
    df = pd.DataFrame(rows).T
    # 货币 ETF 计 0 分：仅当正动量资产不足 top_n 时自然入选
    df[CASH_PROXY] = 0.0
    return df


def _universe_members(conn: sqlite3.Connection, date: str) -> set[str]:
    rows = conn.execute(
        "SELECT ts_code FROM stock_universe_history WHERE index_code='000300.SH' "
        "AND trade_date=(SELECT MAX(trade_date) FROM stock_universe_history "
        "WHERE index_code='000300.SH' AND trade_date<=?)",
        (date,),
    ).fetchall()
    return {r[0] for r in rows}


def _build_dividend_lowvol_scores(conn: sqlite3.Connection, dates: list[str],
                                  close: pd.DataFrame) -> pd.DataFrame:
    from qsim.features.factor import _winsorize, _zscore

    vol = close.pct_change(fill_method=None).rolling(60).std() * math.sqrt(252)
    rows: dict[str, pd.Series] = {}
    for d in dates:
        dv = pd.read_sql_query(
            "SELECT v.ts_code, v.dv_ratio FROM stock_valuation v "
            "WHERE v.date=(SELECT MAX(date) FROM stock_valuation WHERE date<=?)",
            conn, params=(d,),
        ).set_index("ts_code")["dv_ratio"]
        dv = dv[dv.index.isin(_universe_members(conn, d))].dropna()
        if dv.empty or d not in vol.index:
            continue
        v60 = vol.loc[d].reindex(dv.index).dropna()
        common = dv.index.intersection(v60.index)
        if len(common) < 40:
            continue
        score = _zscore(_winsorize(dv.loc[common])) - _zscore(_winsorize(v60.loc[common]))
        rows[d] = score.dropna()
    return pd.DataFrame(rows).T


def _trading_stats(result: dict, initial: float) -> dict:
    metrics = dict(result.get("metrics") or {})
    trades = result.get("trades") or []
    metrics["num_trades"] = len(trades)
    metrics["turnover"] = sum(float(t.get("amount") or 0) for t in trades) / initial
    metrics["cost_drag"] = sum(float(t.get("fee") or 0) for t in trades) / initial
    equity = result.get("equity")
    if equity is not None and not equity.empty:
        metrics["window_start"] = str(equity["date"].iloc[0]).replace("-", "")
        metrics["window_end"] = str(equity["date"].iloc[-1]).replace("-", "")
        metrics["days"] = int(len(equity))
        metrics["mean_invested"] = float((equity["position_value"] / equity["total_value"]).mean())
    return metrics


def _case_payload(key: str, name: str, result: dict, source: str, cost_name: str) -> dict:
    equity = result["equity"]
    metrics = _trading_stats(result, CAPITAL)
    return {
        "key": key, "name": name, "source": source, "cost_model": cost_name,
        "metrics": metrics,
        "equity": equity[["date", "cash", "position_value", "total_value"]].to_dict(orient="records"),
    }


def _load_benchmark_close() -> pd.Series:
    """基准：沪深300ETF（510300）收盘价，YYYYMMDD 索引。"""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(
            "SELECT date, close FROM bars WHERE symbol=? AND date>=? AND date<=? ORDER BY date",
            conn, params=(BENCHMARK, START, END),
        )
    finally:
        conn.close()
    if df.empty:
        return pd.Series(dtype=float)
    return df.set_index(df["date"].astype(str))["close"].astype(float)


def _relative_metrics(equity: list[dict], bench: pd.Series) -> dict:
    """相对基准指标：超额收益、信息比率、Calmar、跑赢基准的月度胜率。"""
    if not equity or bench.empty:
        return {}
    tv = pd.Series(
        {str(r["date"]): float(r["total_value"]) for r in equity}
    ).sort_index()
    common = tv.index.intersection(bench.index)
    if len(common) < 60:
        return {}
    tv = tv.loc[common]
    bc = bench.loc[common]
    s_ret = tv.pct_change().dropna()
    b_ret = bc.pct_change().dropna()
    excess_daily = s_ret - b_ret
    ir = float(excess_daily.mean() / excess_daily.std() * math.sqrt(252)) if excess_daily.std() > 0 else 0.0
    total = float(tv.iloc[-1] / tv.iloc[0] - 1)
    bench_total = float(bc.iloc[-1] / bc.iloc[0] - 1)
    nav = tv / tv.iloc[0]
    mdd = float(((nav.cummax() - nav) / nav.cummax()).max())
    days = max((pd.to_datetime(common[-1]) - pd.to_datetime(common[0])).days, 1)
    annual = float((1 + total) ** (365.25 / days) - 1) if total > -1 else -1.0
    calmar = annual / mdd if mdd > 0 else 0.0
    tv_m = tv.copy()
    tv_m.index = pd.to_datetime(tv_m.index)
    bc_m = bc.copy()
    bc_m.index = pd.to_datetime(bc_m.index)
    s_monthly = tv_m.resample("ME").last().pct_change().dropna()
    b_monthly = bc_m.resample("ME").last().pct_change().dropna()
    common_m = s_monthly.index.intersection(b_monthly.index)
    win = float((s_monthly.loc[common_m] > b_monthly.loc[common_m]).mean()) if len(common_m) else 0.0
    return {
        "benchmark": BENCHMARK,
        "benchmark_total_return": bench_total,
        "excess_total_return": total - bench_total,
        "information_ratio": ir,
        "calmar": float(calmar),
        "monthly_win_rate": win,
    }


def _refresh_relative_metrics() -> None:
    """不重跑回测，仅从冻结 equity CSV 重算相对指标并合并回 summary。"""
    bench = _load_benchmark_close()
    summary_path = OUT / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    for entry in summary["strategies"]:
        csv_path = OUT / f"{entry['key']}_equity.csv"
        if not csv_path.exists():
            continue
        with csv_path.open(newline="", encoding="utf-8") as handle:
            equity = list(csv.DictReader(handle))
        entry["metrics"].update(_relative_metrics(equity, bench))
    summary["manifest"]["benchmark"] = BENCHMARK
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "metrics.json").write_text(
        json.dumps({"manifest": summary["manifest"], "strategies": summary["strategies"]},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "metrics.md").write_text(_metrics_md(summary["strategies"]), encoding="utf-8")
    print(json.dumps({"ok": True, "refreshed": [s["key"] for s in summary["strategies"]],
                      "benchmark": BENCHMARK}, ensure_ascii=False))


def _build_cases(only: str | None = None) -> list[dict]:
    """构建回测 case；only 指定时只算该 case（用于单独补跑，不动其他曲线）。"""
    cases: list[dict] = []
    if only is None:
        source = SqliteDataSource(DB, table="bars", symbol_column="symbol")
        singles = [
            ("tsmom_vol", "时序动量 · 波动率", "513100", TsmomVolStrategy()),
            ("dual_ma", "双均线", "510300", DualMAStrategy()),
            ("grid_atr", "ATR 网格", "512100", GridATRStrategy()),
            ("rsi_boll", "RSI · 布林", "518880", RsiBollStrategy()),
            ("cb_dual_low", "可转债双低", "511380", CbDualLow()),
        ]
        for key, name, symbol, strategy in singles:
            bars = source.get_bars(symbol, start=START, end=END)
            result = run_backtest(bars, strategy, CAPITAL, SINGLE_COST, symbol, start=START, end=END)
            cases.append(_case_payload(key, name, result, f"bars:{symbol}", "single_etf_10bp_plus_5bp_slippage"))

        rotation_data = {
            symbol: source.get_bars(symbol, start=START, end=END)
            for symbol in ("518880", "511380")
        }
        rotation = run_rotation_backtest(
            rotation_data, TrendFallback(symbol="518880", ma=60, fallback="511380"),
            CAPITAL, SINGLE_COST, start=START, end=END,
        )
        cases.append(_case_payload(
            "trend_fallback", "趋势 · 防御切换", rotation,
            "bars:518880+511380", "single_etf_10bp_plus_5bp_slippage",
        ))

    stock_open, stock_close, dates = _load_stock_panels()
    rebalance_dates = monthly_rebalance_dates(dates)
    # Both score panels are materialized against the declared data cutoff.
    # The scores on any date can therefore use only bars/universe snapshots
    # available then and labels already settled by that date.
    if only in (None, "multi_asset_mom"):
        etf_open, etf_close, etf_dates = _load_etf_panels()
        etf_rb = monthly_rebalance_dates(etf_dates)
        mom_scores = _build_momentum_scores(etf_close, etf_rb)
        mom_data = CrossSectionalData(
            dates=etf_dates, factor_scores=mom_scores, open=etf_open, close=etf_close,
        )
        mom = run_cross_sectional_backtest(
            mom_data, TopNScoreStrategy(top_n=3), CAPITAL, SINGLE_COST,
            rebalance="monthly", rebalance_dates=etf_rb,
        )
        cases.append(_case_payload(
            "multi_asset_mom", "多资产ETF动量", mom,
            "bars:9ETF+511880货币代理", "single_etf_10bp_plus_5bp_slippage",
        ))

    if only in (None, "multi_factor", "multi_factor_v3"):
        factor_scores = compute_factor_ic_scores(DB, rebalance_dates, as_of=END)
        cross_data_factor = CrossSectionalData(
            dates=dates, factor_scores=pd.DataFrame(factor_scores).T,
            open=stock_open, close=stock_close,
        )
        if only in (None, "multi_factor"):
            multi = run_cross_sectional_backtest(
                cross_data_factor, MultiFactorStrategy(top_n=10), CAPITAL, CROSS_COST,
                rebalance="monthly", rebalance_dates=rebalance_dates,
            )
            cases.append(_case_payload("multi_factor", "多因子", multi, "stock_bars:IC", "stock_default_cost"))
        if only in (None, "multi_factor_v3"):
            conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            try:
                allocations, gate_records = multi_factor_v3_allocations(conn, dates, rebalance_dates)
            finally:
                conn.close()
            v3 = run_cross_sectional_backtest(
                cross_data_factor, MultiFactorStrategy(top_n=10), CAPITAL, CROSS_COST,
                rebalance="monthly", rebalance_dates=rebalance_dates,
                allocation_by_signal_date=allocations, sizing="live_affordable",
            )
            v3_payload = _case_payload("multi_factor_v3", "多因子 · v3", v3, "stock_bars:IC+PIT_gate", "stock_default_cost")
            v3_payload["gate_records"] = gate_records
            cases.append(v3_payload)

    if only in (None, "alpha158"):
        alpha_scores = compute_alpha158_ic_scores(DB, rebalance_dates, as_of=END)
        cross_data_alpha = CrossSectionalData(
            dates=dates, factor_scores=pd.DataFrame(alpha_scores).T,
            open=stock_open, close=stock_close,
        )
        alpha = run_cross_sectional_backtest(
            cross_data_alpha, Alpha158Strategy(top_n=10), CAPITAL, CROSS_COST,
            rebalance="monthly", rebalance_dates=rebalance_dates,
        )
        cases.append(_case_payload("alpha158", "Alpha158", alpha, "stock_bars:IC", "stock_default_cost"))

    if only in (None, "dividend_lowvol"):
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            dv_scores = _build_dividend_lowvol_scores(conn, rebalance_dates, stock_close)
        finally:
            conn.close()
        dv_data = CrossSectionalData(
            dates=dates, factor_scores=dv_scores, open=stock_open, close=stock_close,
        )
        dv = run_cross_sectional_backtest(
            dv_data, TopNScoreStrategy(top_n=20), CAPITAL, CROSS_COST,
            rebalance="monthly", rebalance_dates=rebalance_dates,
        )
        cases.append(_case_payload(
            "dividend_lowvol", "红利低波", dv,
            "stock_bars:dv_ratio+60d低波", "stock_default_cost",
        ))
    return cases


def _annotate_sample(item: dict) -> None:
    metrics = item["metrics"]
    actual_start = str(metrics.get("window_start") or START)
    actual_end = str(metrics.get("window_end") or END)
    short_sample = actual_start > START or actual_end < END
    item["sample"] = {
        "requested_start": START,
        "requested_end": END,
        "actual_start": actual_start,
        "actual_end": actual_end,
        "is_full_window": not short_sample,
        "note": (
            "非完整十年样本；不应与完整窗口策略直接横向比较。"
            if short_sample else "完整请求窗口。"
        ),
    }


def _write_case_csv(item: dict) -> None:
    csv_name = f"{item['key']}_equity.csv"
    with (OUT / csv_name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "cash", "position_value", "total_value"])
        writer.writeheader()
        writer.writerows(item["equity"])


def _metrics_md(cases: list[dict]) -> str:
    return "\n".join([
        f"# {len(cases)} 策略回测曲线",
        "",
        f"窗口：{START} 至 {END}；初始资金：{CAPITAL:.0f}；评分：严格时点；未来数据：否。",
        "",
        *[f"- {item['name']}：累计 {float(item['metrics'].get('total_return', 0)):.2%}；年化 {float(item['metrics'].get('annual_return', 0)):.2%}；回撤 {float(item['metrics'].get('max_drawdown', 0)):.2%}；Sharpe {float(item['metrics'].get('sharpe', 0)):.2f}；交易 {int(item['metrics'].get('num_trades', 0))} 次；{'完整窗口' if item['sample']['is_full_window'] else '短样本：' + item['sample']['actual_start'] + ' 起'}" for item in cases],
    ]) + "\n"


def _merge_cases(cases: list[dict]) -> None:
    """把 --only 跑出的 case 合并进现有 summary/metrics，不重写其他曲线。"""
    summary_path = OUT / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    by_key = {s["key"]: s for s in summary["strategies"]}
    order = [s["key"] for s in summary["strategies"]]
    for item in cases:
        _write_case_csv(item)
        by_key[item["key"]] = {k: v for k, v in item.items() if k not in {"equity", "gate_records"}}
        if item["key"] not in order:
            order.append(item["key"])
    summary["strategies"] = [by_key[k] for k in order]
    summary["manifest"]["strategies"] = order
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "metrics.json").write_text(
        json.dumps({"manifest": summary["manifest"], "strategies": summary["strategies"]},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "metrics.md").write_text(_metrics_md(summary["strategies"]), encoding="utf-8")
    print(json.dumps({"ok": True, "merged": [c["key"] for c in cases], "output": str(OUT)}, ensure_ascii=False))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="只重跑指定 case 并合并进现有 summary")
    parser.add_argument("--metrics-only", action="store_true",
                        help="不重跑回测，仅从冻结 CSV 重算基准相对指标")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.metrics_only:
        _refresh_relative_metrics()
        return
    cases = _build_cases(only=args.only)
    for item in cases:
        _annotate_sample(item)
    bench = _load_benchmark_close()
    for item in cases:
        item["metrics"].update(_relative_metrics(item["equity"], bench))
    if args.only:
        _merge_cases(cases)
        return

    manifest = {
        "run_id": "strategy_curves_20160815_20260814_strict_pit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "start": START, "end": END, "capital": CAPITAL,
        "database": str(DB),
        "database_sha256": hashlib.sha256(DB.read_bytes()).hexdigest(),
        "strategies": [item["key"] for item in cases],
        "benchmark": BENCHMARK,
        "rule": "single strategies use shared signal runner; cross-sectional strategies use monthly IC top-10 with next-open execution; v3 adds PIT trend+breadth+20d volatility gate",
        "factor_score_mode": "strict_point_in_time_as_of_end",
        "alpha158_score_mode": "strict_point_in_time_as_of_end",
        "future_data_used": False,
    }
    summary = {"manifest": manifest, "strategies": []}
    for item in cases:
        _write_case_csv(item)
        summary["strategies"].append({k: v for k, v in item.items() if k not in {"equity", "gate_records"}})
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "metrics.json").write_text(json.dumps({"manifest": manifest, "strategies": summary["strategies"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "metrics.md").write_text(_metrics_md(cases), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(OUT), "strategies": [{"key": item["key"], "points": len(item["equity"]), "total_return": item["metrics"].get("total_return"), "max_drawdown": item["metrics"].get("max_drawdown"), "sharpe": item["metrics"].get("sharpe")} for item in cases]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
