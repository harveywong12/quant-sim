#!/usr/bin/env python3
"""Independent walk-forward study for filtered Dual MA and ATR Grid.

This script deliberately does not import or overwrite the production strategy
registry.  Every candidate is evaluated with the existing T+1 runner and the
same cost model as the displayed baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.runner import run_backtest
from qsim.market.data_source import SqliteDataSource
from qsim.strategies.base import Strategy
from qsim.strategies.builtin.dual_ma import DualMAStrategy
from qsim.strategies.builtin.grid_atr import GridATRStrategy

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "quant.db"
OUT = ROOT / "artifacts" / "experiments" / "dual_grid_walkforward_20260901"
START = "20160815"
END = "20260814"
CAPITAL = 100000.0
COST = CostModel(
    commission_rate=0.0001, min_commission=0.1,
    stamp_tax=0.0, slippage=0.0005,
)


class FilteredDualMA(Strategy):
    """Dual MA with trend, cooldown and ATR breakdown pause."""

    name = "dual_ma_filtered"

    def __init__(self, short: int, long: int, trend: int, slope: int,
                 cooldown: int, breakdown_atr: float) -> None:
        self.short, self.long = short, long
        self.trend, self.slope = trend, slope
        self.cooldown, self.breakdown_atr = cooldown, breakdown_atr
        self._pause_until = -1
        self._previous_target = 0.0

    def reset(self) -> None:
        self._pause_until = -1
        self._previous_target = 0.0

    def generate(self, df: pd.DataFrame):
        n = len(df)
        required = max(self.long, self.trend, self.slope) + 1
        if n < required:
            return "HOLD", 0.0
        close = df["close"]
        fast = close.rolling(self.short).mean().iloc[-1]
        slow = close.rolling(self.long).mean().iloc[-1]
        trend_ma = close.rolling(self.trend).mean()
        trend_now = trend_ma.iloc[-1]
        trend_prev = trend_ma.iloc[-1 - self.slope]
        if any(pd.isna(x) for x in (fast, slow, trend_now, trend_prev)):
            return "HOLD", 0.0
        prev_close = close.shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(20).mean().iloc[-1]
        price = float(close.iloc[-1])
        breakdown = bool(pd.notna(atr) and price < trend_now - self.breakdown_atr * atr)
        trend_ok = price > trend_now and trend_now > trend_prev
        cross_ok = fast > slow
        target = 1.0 if trend_ok and cross_ok and not breakdown else 0.0
        if breakdown:
            self._pause_until = max(self._pause_until, n + self.cooldown)
        if self._previous_target > 0 and target == 0:
            self._pause_until = max(self._pause_until, n + self.cooldown)
        if n < self._pause_until:
            target = 0.0
        self._previous_target = target
        return ("BUY" if target > 0 else "SELL"), target


class FilteredGridATR(Strategy):
    """ATR grid with trend admission, cooldown and breakdown pause."""

    name = "grid_atr_filtered"

    def __init__(self, anchor: int, atr_period: int, k: float, grid_num: int,
                 trend: int, slope: int, cooldown: int,
                 breakdown_atr: float) -> None:
        self.anchor, self.atr_period, self.k, self.grid_num = anchor, atr_period, k, grid_num
        self.trend, self.slope = trend, slope
        self.cooldown, self.breakdown_atr = cooldown, breakdown_atr
        self._pause_until = -1
        self._previous_weight = 0.0

    def reset(self) -> None:
        self._pause_until = -1
        self._previous_weight = 0.0

    def generate(self, df: pd.DataFrame):
        n = len(df)
        required = max(self.anchor, self.atr_period, self.trend, self.slope) + 1
        if n < required:
            return "HOLD", 0.0
        close = df["close"]
        anchor = close.rolling(self.anchor).mean().iloc[-1]
        trend_ma = close.rolling(self.trend).mean()
        trend_now, trend_prev = trend_ma.iloc[-1], trend_ma.iloc[-1 - self.slope]
        prev_close = close.shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(self.atr_period).mean().iloc[-1]
        price = float(close.iloc[-1])
        if any(pd.isna(x) for x in (anchor, trend_now, trend_prev, atr)) or price <= 0:
            return "HOLD", 0.0
        grid_pct = max(0.004, min(self.k * float(atr) / price, 0.04))
        breakdown = price < float(trend_now) - self.breakdown_atr * float(atr)
        trend_ok = price > float(trend_now) and float(trend_now) > float(trend_prev)
        if breakdown:
            self._pause_until = max(self._pause_until, n + self.cooldown)
        offset = (price - float(anchor)) / float(anchor)
        below = -offset / grid_pct
        grid = max(2, min(int(below), self.grid_num))
        target = grid / self.grid_num if trend_ok and not breakdown else 0.0
        if self._previous_weight > 0 and target == 0:
            self._pause_until = max(self._pause_until, n + self.cooldown)
        if n < self._pause_until:
            target = 0.0
        self._previous_weight = target
        return ("BUY" if target > self._previous_weight or target > 0 else "SELL"), target


def _window_metrics(result: dict, start: str, end: str, initial: float | None = None) -> dict:
    equity = result["equity"]
    equity_dates = pd.to_datetime(equity["date"])
    frame = equity[(equity_dates >= pd.to_datetime(start))
                   & (equity_dates <= pd.to_datetime(end))].copy()
    if frame.empty:
        return {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0,
                "sharpe": 0.0, "num_trades": 0, "turnover": 0.0, "cost_drag": 0.0,
                "days": 0}
    base = float(frame["total_value"].iloc[0]) if initial is None else initial
    final = float(frame["total_value"].iloc[-1])
    returns = frame["total_value"].pct_change().dropna()
    peak = frame["total_value"].cummax()
    mdd = float((frame["total_value"] / peak - 1).min())
    sharpe = float(returns.mean() / returns.std() * np.sqrt(252)) if len(returns) > 1 and returns.std() > 0 else 0.0
    trade_rows = [t for t in result.get("trades", [])
                  if start <= str(t.get("date", "")) <= end]
    return {
        "total_return": final / base - 1 if base else 0.0,
        "annual_return": (final / base) ** (252 / len(frame)) - 1 if len(frame) >= 21 and base else 0.0,
        "max_drawdown": mdd,
        "sharpe": sharpe,
        "num_trades": len(trade_rows),
        "turnover": sum(float(t.get("amount") or 0) for t in trade_rows) / base if base else 0.0,
        "cost_drag": sum(float(t.get("fee") or 0) for t in trade_rows) / base if base else 0.0,
        "days": int(len(frame)),
        "start_value": base,
        "final_value": final,
    }


def _specs() -> dict[str, list[tuple[str, dict]]]:
    dual = [
        ("d_5_20_t60_c5_b1", {"short": 5, "long": 20, "trend": 60, "slope": 20, "cooldown": 5, "breakdown_atr": 1.0}),
        ("d_10_40_t60_c10_b1", {"short": 10, "long": 40, "trend": 60, "slope": 20, "cooldown": 10, "breakdown_atr": 1.0}),
        ("d_20_60_t120_c10_b15", {"short": 20, "long": 60, "trend": 120, "slope": 20, "cooldown": 10, "breakdown_atr": 1.5}),
    ]
    grid = [
        ("g_a20_atr20_k1_t60_c5_b1", {"anchor": 20, "atr_period": 20, "k": 1.0, "grid_num": 8, "trend": 60, "slope": 20, "cooldown": 5, "breakdown_atr": 1.0}),
        ("g_a20_atr20_k15_t60_c10_b15", {"anchor": 20, "atr_period": 20, "k": 1.5, "grid_num": 8, "trend": 60, "slope": 20, "cooldown": 10, "breakdown_atr": 1.5}),
        ("g_a60_atr40_k1_t120_c5_b15", {"anchor": 60, "atr_period": 40, "k": 1.0, "grid_num": 8, "trend": 120, "slope": 20, "cooldown": 5, "breakdown_atr": 1.5}),
    ]
    return {"dual_ma": dual, "grid_atr": grid}


def _make(family: str, params: dict) -> Strategy:
    return FilteredDualMA(**params) if family == "dual_ma" else FilteredGridATR(**params)


def _run(data: pd.DataFrame, strategy: Strategy, symbol: str,
         start: str = START, end: str = END) -> dict:
    return run_backtest(data, strategy, CAPITAL, COST, symbol, start=start, end=end)


def _walkforward(data: pd.DataFrame, family: str, symbol: str, candidates: list[tuple[str, dict]]) -> list[dict]:
    rows = []
    # Five one-year OOS slices.  The last slice ends at the declared cutoff.
    for test_year in range(2021, 2026):
        train_start = f"{test_year - 5}-08-15"
        val_start = f"{test_year - 2}-08-15"
        test_start = f"{test_year}-08-15"
        test_end = f"{test_year + 1}-08-14"
        if test_end > END:
            test_end = END
        scored = []
        for key, params in candidates:
            validation = _run(data, _make(family, params), symbol, train_start, test_start)
            metrics = _window_metrics(validation, val_start, test_start)
            scored.append((metrics["sharpe"], metrics["total_return"], metrics["max_drawdown"], key, params, metrics))
        scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        _, _, _, selected_key, selected_params, selected_validation = scored[0]
        selected = _run(data, _make(family, selected_params), symbol, train_start, test_end)
        baseline_cls = DualMAStrategy() if family == "dual_ma" else GridATRStrategy()
        baseline = _run(data, baseline_cls, symbol, train_start, test_end)
        rows.append({
            "test_start": test_start, "test_end": test_end,
            "train_start": train_start, "validation_start": val_start,
            "selected": selected_key, "selected_params": selected_params,
            "validation": selected_validation,
            "oos": _window_metrics(selected, test_start, test_end),
            "baseline_oos": _window_metrics(baseline, test_start, test_end),
            "candidate_validation": {
                key: metrics for _, _, _, key, _, metrics in scored
            },
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="run a short execution smoke only")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    source = SqliteDataSource(DB, table="bars", symbol_column="symbol")
    symbols = {"dual_ma": "510300", "grid_atr": "512100"}
    if args.smoke:
        smoke_rows = {}
        for family, symbol in symbols.items():
            data = source.get_bars(symbol, start="20240101", end=END)
            result = _run(data, _make(family, _specs()[family][0][1]), symbol, start="20240101", end=END)
            smoke_rows[family] = _window_metrics(result, "20240101", END)
        print(json.dumps({"ok": True, "smoke": smoke_rows}, ensure_ascii=False))
        return

    all_results = {}
    for family, symbol in symbols.items():
        data = source.get_bars(symbol, start=START, end=END)
        baseline_cls = DualMAStrategy() if family == "dual_ma" else GridATRStrategy()
        baseline = _run(data, baseline_cls, symbol)
        candidates = _specs()[family]
        full = {}
        for key, params in candidates:
            result = _run(data, _make(family, params), symbol)
            full[key] = _window_metrics(result, START, END)
        all_results[family] = {
            "symbol": symbol,
            "baseline": _window_metrics(baseline, START, END),
            "candidates": full,
            "walkforward": _walkforward(data, family, symbol, candidates),
        }

    manifest = {
        "run_id": "dual_grid_walkforward_20260901",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": str(DB),
        "database_sha256": hashlib.sha256(DB.read_bytes()).hexdigest(),
        "start": START, "end": END, "capital": CAPITAL,
        "cost_model": {"commission_rate": COST.commission_rate, "min_commission": COST.min_commission, "stamp_tax": COST.stamp_tax, "slippage": COST.slippage},
        "execution": "existing run_backtest; signal at close, next trading-day open execution",
        "walkforward": "5 rolling OOS slices; each uses preceding 3 years for training context and preceding 1 year validation; selection by validation Sharpe, then return, then drawdown",
        "mechanisms": ["trend_filter", "cooldown", "breakdown_pause", "limited_parameter_grid"],
        "production_changed": False,
    }
    payload = {"manifest": manifest, "results": all_results}
    (OUT / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "metrics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Dual MA / ATR Grid 改进回测与 Walk-forward", "", f"窗口：{START} 至 {END}；成本与 T+1 执行规则同基线。", ""]
    for family, result in all_results.items():
        lines.append(f"## {family} · {result['symbol']}")
        for label, metrics in [("基线", result["baseline"]), *result["candidates"].items()]:
            lines.append(f"- {label}：累计 {metrics['total_return']:.2%}；年化 {metrics['annual_return']:.2%}；MDD {metrics['max_drawdown']:.2%}；Sharpe {metrics['sharpe']:.2f}；交易 {metrics['num_trades']} 次；换手 {metrics['turnover']:.2f}x")
        lines.append("- OOS：" + "；".join(f"{row['test_start']} 选 {row['selected']}，改进 {row['oos']['total_return']:.2%} vs 基线 {row['baseline_oos']['total_return']:.2%}，Sharpe {row['oos']['sharpe']:.2f} vs {row['baseline_oos']['sharpe']:.2f}" for row in result["walkforward"]))
        lines.append("")
    (OUT / "metrics.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(OUT), "families": list(all_results)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
