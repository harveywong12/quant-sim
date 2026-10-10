"""Isolated, causal comparisons of trend fallback rules; no account writes."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.matcher import buy, sell, adjust_position
from qsim.engine.metrics import portfolio_metrics
from qsim.engine.rotation_runner import run_rotation_backtest
from qsim.strategies.rotation import TrendFallback
from qsim.services.web.performance import curve_metrics

PRIMARY = "518880"
FALLBACK = "511380"
VARIANTS = {
    "baseline_warm": "原规则 · 保留预热历史",
    "fallback_filter": "备用资产 MA60 过滤 · 双弱转现金",
    "cash_exit": "黄金 MA60 以下转现金",
    "volatility_cap": "双趋势过滤 + 10% 波动率目标",
}
PARAMETERS = {"ma": 60, "vol_days": 20, "target_vol": 0.10,
              "weight_band": 0.05, "max_weight": 1.0}
BASE_COST = CostModel(commission_rate=0.0001, min_commission=0.1,
                      stamp_tax=0.0, slippage=0.0005)


def targets(bars: pd.DataFrame, variant: str) -> pd.DataFrame:
    """All rolling features use only the current/past close; fills are shifted."""
    close = bars.pivot(index="date", columns="symbol", values="close").sort_index()
    ma = close.rolling(PARAMETERS["ma"]).mean()
    vol = close.pct_change(fill_method=None).rolling(PARAMETERS["vol_days"]).std() * math.sqrt(252)
    rows = []
    for date, values in close.iterrows():
        primary_ready = pd.notna(ma.loc[date, PRIMARY])
        primary_good = primary_ready and values[PRIMARY] > ma.loc[date, PRIMARY]
        fallback_ready = pd.notna(ma.loc[date, FALLBACK])
        fallback_good = fallback_ready and values[FALLBACK] > ma.loc[date, FALLBACK]
        if primary_good:
            symbol = PRIMARY
        elif variant == "baseline_warm":
            symbol = FALLBACK
        elif variant == "cash_exit":
            symbol = "CASH"
        else:
            symbol = FALLBACK if fallback_good else "CASH"
        weight = 0.0 if symbol == "CASH" else 1.0
        if variant == "volatility_cap" and symbol != "CASH":
            risk = float(vol.loc[date, symbol])
            if not math.isfinite(risk):
                symbol, weight = "CASH", 0.0
            elif risk > 0:
                weight = min(1.0, PARAMETERS["target_vol"] / risk)
        rows.append({"date": date, "target": symbol, "weight": weight})
    return pd.DataFrame(rows).set_index("date")


def simulate(bars: pd.DataFrame, signals: pd.DataFrame, start: str, end: str,
             cost: CostModel = BASE_COST, initial: float = 100000,
             weighted: bool = False) -> dict:
    """Use a common tradable calendar and yesterday's frozen target at open."""
    close = bars.pivot(index="date", columns="symbol", values="close").sort_index()
    opens = bars.pivot(index="date", columns="symbol", values="open").reindex(close.index)
    complete = close.notna().all(axis=1) & opens.notna().all(axis=1)
    days = [d for d in close.index[complete] if start <= d <= end]
    if not days:
        raise ValueError("empty common trading window")
    cash, shares, cur = float(initial), 0, "CASH"
    pending = None
    last_weight = None
    trades, equity, skips = [], [], []
    prev_close = close.shift(1)
    for date in days:
        if pending is not None:
            target, weight = pending
            if target != cur and shares:
                cash, shares, fill, reason = sell(cash, shares, float(opens.loc[date, cur]),
                    date, cost, cur, prev_close=float(prev_close.loc[date, cur]))
                if fill:
                    trades.append(fill.to_dict())
                elif reason:
                    skips.append({"date": date, "symbol": cur, "reason": reason})
                if shares == 0:
                    cur, last_weight = "CASH", None
            # A failed sale must never orphan the old position or buy a second
            # asset into the same one-position account.
            if target != "CASH" and (cur == "CASH" or cur == target):
                price = float(opens.loc[date, target])
                total = cash + shares * price
                if weighted:
                    if last_weight is None or abs(weight - last_weight) >= PARAMETERS["weight_band"]:
                        cash, shares, fill, reason = adjust_position(cash, shares, price,
                            date, cost, weight, total, target,
                            prev_close=float(prev_close.loc[date, target]))
                        if fill:
                            trades.append(fill.to_dict())
                            cur, last_weight = target if shares else "CASH", weight
                        elif reason:
                            skips.append({"date": date, "symbol": target, "reason": reason})
                elif cur == "CASH":
                    cash, shares, fill, reason = buy(cash, shares, price, date, cost, target,
                        prev_close=float(prev_close.loc[date, target]))
                    if fill:
                        trades.append(fill.to_dict())
                        cur = target
                    elif reason:
                        skips.append({"date": date, "symbol": target, "reason": reason})
        position = shares * float(close.loc[date, cur]) if shares else 0.0
        equity.append({"date": date, "cash": cash, "position_value": position,
                       "total_value": cash + position})
        signal = signals.loc[date]
        pending = str(signal["target"]), float(signal["weight"])
    frame = pd.DataFrame(equity)
    metrics = {**portfolio_metrics(frame, initial, len(trades)),
               **curve_metrics(equity, initial),
               "mean_invested": float((frame.position_value / frame.total_value).mean()),
               "fees": sum(t["fee"] for t in trades),
               "turnover": sum(t["amount"] for t in trades) / initial,
               "days": len(days), "start": days[0], "end": days[-1]}
    return {"equity": frame, "trades": trades, "skips": skips, "metrics": metrics}


def run(db_path: Path, output: Path) -> dict:
    # mode=ro and one read transaction give every variant identical frozen rows.
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        conn.execute("BEGIN")
        bars = pd.read_sql_query("SELECT symbol,date,open,close FROM bars "
            "WHERE symbol IN (?,?) ORDER BY date,symbol", conn, params=(PRIMARY, FALLBACK))
        source_schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='bars'").fetchone()[0]
    pivot = bars.pivot(index="date", columns="symbol", values="close")
    complete = pivot.dropna()
    if len(complete) < 100:
        raise ValueError("insufficient common history")
    # Require sixty prior fallback observations; the ETF begins in April 2020.
    first = complete.index[60]
    last = complete.index[-1]
    windows = {"full": (first, last), "development": (first, "20221230"),
               "historical_holdout": ("20230103", last), "recent": ("20260803", last)}
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    destination = output / run_id
    destination.mkdir(parents=True, exist_ok=False)
    input_csv = bars.to_csv(index=False)
    (destination / "input-bars.csv").write_text(input_csv)
    code_files = [Path(__file__), Path(__file__).parents[1] / "engine" / "matcher.py",
                  Path(__file__).parents[1] / "engine" / "costs.py",
                  Path(__file__).parents[1] / "engine" / "rotation_runner.py",
                  Path(__file__).parents[1] / "strategies" / "rotation.py",
                  Path(__file__).parents[1] / "services" / "web" / "performance.py"]
    payload = {"mode": "trend_fallback_research", "state": "research_only", "run_id": run_id,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "contract": {"database": str(db_path.resolve()), "input_sha256": hashlib.sha256(input_csv.encode()).hexdigest(),
            "code_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in code_files},
            "source_schema": source_schema, "rows": len(bars), "source_end": last,
            "parameters": PARAMETERS, "base_cost": asdict(BASE_COST),
            "execution": "收盘生成目标，下一共同交易日开盘成交；保留完整预热历史；现金收益为0；独立初始10万元",
            "holdout_note": "2023年起为按时间划分的历史对照；规则在2026年提出，不能视为未接触过的样本外验证",
            "price_note": "本地 bars 收盘价格口径；未另计分红总回报，未验证外部数据源；两ETF共同可交易日期",
            "selection": "固定4条规则、固定参数；不寻优、不自动晋升，不修改正式账本",
            "warmup_issue": "原执行器在start处裁掉历史，60日均线在42日近期账本内无法形成；冷启动原规则单独列示"},
        "variants": [], "cold_start": None}
    for variant, label in VARIANTS.items():
        signal = targets(bars, variant)
        metrics = {}
        stress = []
        for window, (start, end) in windows.items():
            result = simulate(bars, signal, start, end, weighted=variant == "volatility_cap")
            metrics[window] = result["metrics"]
            result["equity"].to_csv(destination / f"{variant}_{window}_equity.csv", index=False)
            (destination / f"{variant}_{window}_trades.json").write_text(json.dumps(result["trades"], ensure_ascii=False))
            if window == "full":
                metrics[window]["skipped_orders"] = len(result["skips"])
        for multiplier in (2, 4):
            cost = CostModel(**{**asdict(BASE_COST),
                "commission_rate": BASE_COST.commission_rate * multiplier,
                "min_commission": BASE_COST.min_commission * multiplier,
                "slippage": BASE_COST.slippage * multiplier})
            stress.append({"cost_multiplier": multiplier,
                **simulate(bars, signal, *windows["historical_holdout"], cost=cost,
                           weighted=variant == "volatility_cap")["metrics"]})
        payload["variants"].append({"key": variant, "label": label, "metrics": metrics, "stress": stress})
        print(f"{variant}: full {metrics['full']['total_return']:.4f}, recent {metrics['recent']['total_return']:.4f}", flush=True)
    cold = run_rotation_backtest({sym: bars[bars.symbol == sym] for sym in (PRIMARY, FALLBACK)},
        TrendFallback(PRIMARY), 100000, BASE_COST, start=windows["recent"][0], end=last)
    payload["cold_start"] = {"label": "原执行器冷启动 · 近期账本机制对照", "metrics": cold["metrics"]}
    cold["equity"].to_csv(destination / "cold_start_recent_equity.csv", index=False)
    target = destination / "summary.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "latest.tmp"
    temporary.write_text(target.read_text())
    temporary.replace(output / "latest.json")
    return payload


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=root / "data" / "quant.db")
    parser.add_argument("--output", type=Path, default=root / "artifacts" / "trend_fallback_research")
    args = parser.parse_args()
    result = run(args.database, args.output)
    print(json.dumps({"run_id": result["run_id"], "state": result["state"]}, ensure_ascii=False))
