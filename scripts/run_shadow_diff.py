#!/usr/bin/env python3
"""Run shadow reconciliation between qsim and the legacy system."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import subprocess
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
from qsim.features.factor import compute_factor_ic_scores, compute_factor_scores
from qsim.market.data_source import SqliteDataSource
from qsim.projections.shadow_diff import (
    compare_equity,
    compare_event_stream,
    compare_metrics,
    compare_strategy_outputs,
)
from qsim.strategies.migration import (
    MigrationState,
    active_migration_names,
    migration_for,
)
from qsim.strategies.cross_sectional import Alpha158Strategy, MultiFactorStrategy
from qsim.strategies.builtin.cb_dual_low import CbDualLow
from qsim.strategies.builtin.dual_ma import DualMAStrategy
from qsim.strategies.builtin.grid_atr import GridATRStrategy
from qsim.strategies.builtin.rsi_boll import RsiBollStrategy
from qsim.strategies.builtin.tsmom import TsmomVolStrategy
from qsim.strategies.rotation import TrendFallback

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "quant.db"
LEGACY_ROOT = Path.home() / "quant-sim"
LEGACY_PYTHON = LEGACY_ROOT / ".venv" / "bin" / "python"
START = "20160801"
# Historical parity was certified on a frozen window ending 20251231; the
# daily gate now rolls END forward to the latest date both databases have
# synced, so post-2025 behavioural drift gets reconciled instead of masked.
FALLBACK_END = "20251231"


def resolve_end() -> str:
    """Resolve the window end as the latest common synced market date.

    QSIM_SHADOW_END pins an explicit window for ad-hoc runs.  When neither
    database exposes a max date the certified fallback keeps the gate alive.
    """
    override = os.environ.get("QSIM_SHADOW_END", "").strip()
    if override:
        return override

    def _max_date(path: Path) -> str | None:
        if not path.exists():
            return None
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT MIN(d) FROM ("
                "SELECT MAX(date) AS d FROM bars "
                "UNION ALL SELECT MAX(date) FROM stock_bars)"
            ).fetchone()
            return str(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()

    dates = [
        d
        for d in (_max_date(DB), _max_date(LEGACY_ROOT / "data" / "quant.db"))
        if d
    ]
    return min(dates) if dates else FALLBACK_END


END = resolve_end()

COST = CostModel(
    commission_rate=0.0001,
    min_commission=0.1,
    stamp_tax=0.0,
    slippage=0.0005,
)

# The two cross-sectional legacy backtests use portfolio._stock_fee_cfg()
# when no explicit cost_cfg is supplied.  Keep the single-signal/rotation
# cases on their explicit legacy fixture above, but use the stock defaults for
# these two cases so the comparison is apples-to-apples.
CROSS_SECTIONAL_COST = CostModel(round_fee=False)

CASES = [
    ("tsmom_vol", "513100", "TsmomVol", "app.strategies.tsmom"),
    ("rsi_boll", "518880", "RsiBoll", "app.strategies.rsi_boll"),
    ("dual_ma", "510300", "DualMA", "app.strategies.dual_ma"),
    ("grid_atr", "512100", "GridATR", "app.strategies.grid_atr"),
]


def _run_legacy(symbol: str, class_name: str, module: str) -> dict:
    code = f"""
import sqlite3, json
from app import engine, db
from {module} import {class_name}
conn = sqlite3.connect("file:data/quant.db?mode=ro", uri=True)
cfg = {{"initial_capital": 100000, "commission_rate": 0.0001,
       "min_commission": 0.1, "stamp_tax": 0.0, "slippage": 0.0005}}
bars = db.get_bars(conn, "{symbol}")
bars = bars[(bars["date"] >= "{START}") & (bars["date"] <= "{END}")].reset_index(drop=True)
signals = []
class RecordingStrategy({class_name}):
    def generate(self, history):
        value = super().generate(history)
        if isinstance(value, tuple):
            raw, weight = value
        else:
            raw, weight = value, (0.0 if value == "SELL" else 1.0)
        signals.append({{"date": str(history.iloc[-1]["date"]).replace("-", ""),
                        "signal": str(raw), "weight": float(weight)}})
        return value
res = engine.backtest(bars, cfg, RecordingStrategy(), symbol="{symbol}")
print(json.dumps({{"metrics": res["metrics"],
                   "equity": res["equity"].to_dict(orient="records"),
                   "signals": signals,
                   "trades": [t.to_dict() if hasattr(t, "to_dict") else getattr(t, "__dict__", t)
                              for t in res["trades"]]}}, ensure_ascii=False))
conn.close()
"""
    proc = subprocess.run(
        [str(LEGACY_PYTHON), "-c", code],
        cwd=str(LEGACY_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"legacy backtest failed: {proc.stderr[-500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_legacy_code(code: str) -> dict:
    proc = subprocess.run(
        [str(LEGACY_PYTHON), "-c", code],
        cwd=str(LEGACY_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"legacy backtest failed: {proc.stderr[-500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


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


def _coverage_start() -> str | None:
    """Return the first date covered by the legacy universe history."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT MIN(trade_date) FROM stock_universe_history"
        ).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row and row[0] is not None else None


def _legacy_monthly_rebalance_dates(
    all_dates: list[str], covered_dates: list[str]
) -> list[str]:
    """Match legacy monthly selection: first full-history date per month,
    then intersect it with the requested/covered date range.

    This ordering matters when universe history starts part-way through a
    month: filtering first would shift the rebalance to a different day.
    """
    covered = set(covered_dates)
    out: list[str] = []
    last_month: str | None = None
    for date in sorted(all_dates):
        month = date[:6]
        if month != last_month:
            if date in covered:
                out.append(date)
            last_month = month
    return out


def _cross_case(name: str, scores: pd.DataFrame, strategy, legacy_code: str,
                fallback_scores: pd.DataFrame | None = None) -> dict:
    stock_open, stock_close = _load_stock_panels()
    all_dates = sorted(stock_close.index)
    dates = all_dates
    coverage_start = _coverage_start()
    if coverage_start:
        dates = [date for date in dates if date >= coverage_start]
    rb = _legacy_monthly_rebalance_dates(all_dates, dates)
    data = CrossSectionalData(
        dates=dates,
        factor_scores=scores,
        open=stock_open,
        close=stock_close,
        fallback_scores=fallback_scores,
    )
    qsim = run_cross_sectional_backtest(
        data=data,
        strategy=strategy,
        initial_cash=1_000_000.0,
        cost=CROSS_SECTIONAL_COST,
        rebalance="monthly",
        rebalance_dates=rb,
    )
    legacy = _run_legacy_code(legacy_code)
    return {
        "name": name,
        "canonical_diff": compare_strategy_outputs(qsim, legacy),
        "metric_diff": compare_metrics(qsim["metrics"], legacy["metrics"]),
        "equity_diff": compare_equity(qsim["equity"], pd.DataFrame(legacy["equity"])),
        "qsim_metrics": dict(qsim["metrics"]),
        "old_metrics": dict(legacy["metrics"]),
        "trade_diff": compare_event_stream(
            qsim["trades"], legacy.get("trades", []),
            keys=("date", "symbol", "side", "shares", "price", "fee"),
            numeric_tolerances={"shares": 0.0, "price": 1e-6, "fee": 1e-2},
        ),
    }


def _rotation_case(name: str, symbols: list[str], strategy, legacy_code: str) -> dict:
    source = SqliteDataSource(DB, table="bars", symbol_column="symbol")
    data = {sym: source.get_bars(sym, start=START, end=END) for sym in symbols}
    qsim = run_rotation_backtest(data, strategy, 100000.0, COST)
    legacy = _run_legacy_code(legacy_code)
    return {
        "name": name,
        "canonical_diff": compare_strategy_outputs(qsim, legacy),
        "metric_diff": compare_metrics(qsim["metrics"], legacy["metrics"]),
        "equity_diff": compare_equity(qsim["equity"], pd.DataFrame(legacy["equity"])),
        "qsim_metrics": dict(qsim["metrics"]),
        "old_metrics": dict(legacy["metrics"]),
        "trade_diff": compare_event_stream(
            qsim["trades"], legacy.get("trades", []),
            keys=("date", "symbol", "side", "shares", "price", "fee"),
            numeric_tolerances={"shares": 0.0, "price": 1e-6, "fee": 1e-2},
        ),
    }


ACCEPTED_IMPROVEMENTS = {"alpha158", "multi_factor"}


def _is_improvement_ok(result: dict) -> bool:
    """Accepted-improvement criteria (IMPORTANT_TODO.md): qsim return must not
    trail the legacy system and drawdown must not exceed 1.25x legacy."""
    qsim = result.get("qsim_metrics") or {}
    old = result.get("old_metrics") or {}
    q_total = float(qsim.get("total_return") or 0)
    o_total = float(old.get("total_return") or 0)
    q_dd = float(qsim.get("max_drawdown") or 0)
    o_dd = float(old.get("max_drawdown") or 0)
    return q_total >= o_total - 0.001 and abs(q_dd) <= abs(o_dd) * 1.25 + 0.001


def _case_ok(result: dict) -> bool:
    if result["name"] in ACCEPTED_IMPROVEMENTS:
        return _is_improvement_ok(result)
    return (
        result["canonical_diff"].ok
        and
        result["metric_diff"].ok
        and result["equity_diff"].ok
        and result["trade_diff"].ok
    )


def _report(result: dict) -> None:
    print(f"=== {result['name']} ===")
    print("metric differences:", result["metric_diff"].differences)
    print("equity max diff:", result["equity_diff"].differences)
    print("metric ok:", result["metric_diff"].ok)
    print("equity ok:", result["equity_diff"].ok)
    print("trade differences:", result["trade_diff"].differences)
    print("trade ok:", result["trade_diff"].ok)
    print("canonical ok:", result["canonical_diff"].ok)
    if result["canonical_diff"].details[:3]:
        print("canonical details:", result["canonical_diff"].details[:3])
    if result["trade_diff"].details[:3]:
        print("trade details:", result["trade_diff"].details[:3])
    if result["metric_diff"].details:
        print("metric details:", result["metric_diff"].details)
    if result["equity_diff"].details:
        print("equity details:", result["equity_diff"].details)


def run_all() -> list[dict]:
    stock_open, stock_close = _load_stock_panels()
    all_dates = sorted(stock_close.index)
    dates = all_dates
    coverage_start = _coverage_start()
    if coverage_start:
        dates = [date for date in dates if date >= coverage_start]
    rb = _legacy_monthly_rebalance_dates(all_dates, dates)
    factor_ic_scores = compute_factor_ic_scores(DB, rb)
    alpha_scores = compute_alpha158_ic_scores(DB, rb)
    source = SqliteDataSource(DB, table="bars", symbol_column="symbol")

    results = []
    strategy_map = {
        "tsmom_vol": TsmomVolStrategy,
        "rsi_boll": RsiBollStrategy,
        "dual_ma": DualMAStrategy,
        "grid_atr": GridATRStrategy,
    }
    for name, symbol, class_name, module in CASES:
        if migration_for(name).state is MigrationState.RETIRED:
            continue
        strategy = strategy_map[name]()
        bars = source.get_bars(symbol, start=START, end=END)
        qsim = run_backtest(bars, strategy, 100000.0, COST, symbol)
        legacy = _run_legacy(symbol, class_name, module)
        results.append({
            "name": name,
            "canonical_diff": compare_strategy_outputs(qsim, legacy),
            "metric_diff": compare_metrics(qsim["metrics"], legacy["metrics"]),
            "equity_diff": compare_equity(qsim["equity"], pd.DataFrame(legacy["equity"])),
            "trade_diff": compare_event_stream(
                qsim["trades"], legacy.get("trades", []),
                keys=("date", "symbol", "side", "shares", "price", "fee"),
                numeric_tolerances={"shares": 0.0, "price": 1e-6, "fee": 1e-2},
            ),
        })

    # cb_dual_low
    if migration_for("cb_dual_low").state is not MigrationState.RETIRED:
        bars_cb = source.get_bars("511380", start=START, end=END)
        qsim_cb = run_backtest(bars_cb, CbDualLow(), 100000.0, COST, "511380")
        legacy_cb = _run_legacy_code("""
    import sqlite3, json
    from app import engine, db
    from app.strategy_loader import load
    conn = sqlite3.connect("file:data/quant.db?mode=ro", uri=True)
    bars = db.get_bars(conn, "511380")
    bars = bars[(bars["date"] >= "%s") & (bars["date"] <= "%s")].reset_index(drop=True)
    cfg = {"initial_capital": 100000, "commission_rate": 0.0001,
           "min_commission": 0.1, "stamp_tax": 0.0, "slippage": 0.0005}
    strategy = load("cb_dual_low", {})
    signals = []
    class RecordingStrategy:
        def __init__(self, inner):
            self.inner = inner
        def __getattr__(self, name):
            return getattr(self.inner, name)
        def generate(self, history):
            value = self.inner.generate(history)
            if isinstance(value, tuple):
                raw, weight = value
            else:
                raw, weight = value, (0.0 if value == "SELL" else 1.0)
            signals.append({"date": str(history.iloc[-1]["date"]).replace("-", ""),
                            "signal": str(raw), "weight": float(weight)})
            return value
    res = engine.backtest(bars, cfg, RecordingStrategy(strategy), symbol="511380")
    print(json.dumps({"metrics": res["metrics"],
                       "equity": res["equity"].to_dict(orient="records"),
                       "signals": signals,
                       "trades": [t.to_dict() if hasattr(t, "to_dict") else getattr(t, "__dict__", t)
                                  for t in res["trades"]]}, ensure_ascii=False))
    conn.close()
    """ % (START, END))
        results.append({
            "name": "cb_dual_low",
            "canonical_diff": compare_strategy_outputs(qsim_cb, legacy_cb),
            "metric_diff": compare_metrics(qsim_cb["metrics"], legacy_cb["metrics"]),
            "equity_diff": compare_equity(qsim_cb["equity"], pd.DataFrame(legacy_cb["equity"])),
            "trade_diff": compare_event_stream(
                qsim_cb["trades"], legacy_cb.get("trades", []),
                keys=("date", "symbol", "side", "shares", "price", "fee"),
                numeric_tolerances={"shares": 0.0, "price": 1e-6, "fee": 1e-2},
            ),
        })

    if migration_for("alpha158").state is not MigrationState.RETIRED:
        results.append(_cross_case(
            "alpha158",
            pd.DataFrame(alpha_scores).T,
            Alpha158Strategy(top_n=10),
            """
    import sqlite3, json
    from app import alpha_engine
    from app import portfolio
    conn = sqlite3.connect("file:data/quant.db?mode=ro", uri=True)
    signals = []
    market = alpha_engine.build_panels(conn)
    all_dates = list(market["close"].index)
    signal_by_execution = {}
    for signal_date in %r:
        if signal_date in all_dates:
            index = all_dates.index(signal_date)
            if index + 1 < len(all_dates):
                signal_by_execution[all_dates[index + 1]] = signal_date
    old_rebalance = portfolio._rebalance
    def recording_rebalance(cash, positions, date, targets, *args, **kwargs):
        signal_date = signal_by_execution.get(str(date))
        if signal_date is not None:
            signals.append({"date": signal_date,
                            "signal": json.dumps(list(targets), ensure_ascii=False, separators=(",", ":")),
                            "weight": 1.0})
        return old_rebalance(cash, positions, date, targets, *args, **kwargs)
    portfolio._rebalance = recording_rebalance
    res = alpha_engine.alpha_backtest_ic(conn, top_n=10, capital=1000000.0,
        start="%s", end="%s", factor_kind="alpha158", rebalance="monthly")
    print(json.dumps({"metrics": res["metrics"],
                       "equity": res["equity"].to_dict(orient="records"),
                       "signals": signals,
                       "trades": [t.to_dict() if hasattr(t, "to_dict") else getattr(t, "__dict__", t)
                                  for t in res["trades"]]}, ensure_ascii=False))
    conn.close()
    """ % (rb, START, END),
        ))

    results.append(_cross_case(
        "multi_factor",
        pd.DataFrame(factor_ic_scores).T,
        MultiFactorStrategy(top_n=10),
        """
import sqlite3, json
from app import portfolio
conn = sqlite3.connect("file:data/quant.db?mode=ro", uri=True)
# The legacy implementation leaves equal-score ordering to set iteration.
# Normalize only ties in the shadow reference so the parity result is
# reproducible across the qsim/legacy subprocess boundary.
_old_ic_scores = portfolio._ic_weighted_scores
signals = []
def _stable_ic_scores(*args, **kwargs):
    result = _old_ic_scores(*args, **kwargs)
    normalized = {}
    for date, series in result.items():
        values = series.round(12)
        order = sorted(values.index, key=lambda symbol: (-float(values.loc[symbol]), str(symbol)))
        normalized[date] = values.loc[order]
        signals.append({"date": str(date),
                        "signal": json.dumps(list(normalized[date].head(10).index), ensure_ascii=False, separators=(",", ":")),
                        "weight": 1.0})
    return normalized
portfolio._ic_weighted_scores = _stable_ic_scores
res = portfolio.factor_backtest(conn, top_n=10, capital=1000000.0,
    start="%s", end="%s", weight_mode="ic")
print(json.dumps({"metrics": res["metrics"],
                   "equity": res["equity"].to_dict(orient="records"),
                   "signals": signals,
                   "trades": [t.to_dict() if hasattr(t, "to_dict") else getattr(t, "__dict__", t)
                              for t in res["trades"]]}, ensure_ascii=False))
conn.close()
""" % (START, END),
        # Legacy factor_backtest falls back to equal-weight rank_universe for
        # rebalance dates whose IC labels have not settled yet (the monthly
        # tail under a rolling window); mirror that fallback for parity.
        fallback_scores=compute_factor_scores(DB, rb),
    ))

    results.append(_rotation_case(
        "trend_fallback",
        ["518880", "511380"],
        TrendFallback(symbol="518880", ma=60, fallback="511380"),
        """
import sqlite3, json
from app import engine, db
from app.strategies.trend_fallback import TrendFallback
conn = sqlite3.connect("file:data/quant.db?mode=ro", uri=True)
data = {}
for sym in ["518880", "511380"]:
    bars = db.get_bars(conn, sym)
    bars = bars[(bars["date"] >= "%s") & (bars["date"] <= "%s")].reset_index(drop=True)
    data[sym] = bars
cfg = {"initial_capital": 100000, "commission_rate": 0.0001,
       "min_commission": 0.1, "stamp_tax": 0.0, "slippage": 0.0005}
signals = []
class RecordingRotation(TrendFallback):
    def generate(self, history):
        value = super().generate(history)
        dates = [str(frame["date"].iloc[-1]).replace("-", "")
                 for frame in history.values() if not frame.empty]
        signals.append({"date": max(dates), "signal": str(value), "weight": 1.0})
        return value
res = engine.rotation_backtest(data, cfg, RecordingRotation(symbol="518880", ma=60, fallback="511380"))
print(json.dumps({"metrics": res["metrics"],
                   "equity": res["equity"].to_dict(orient="records"),
                   "signals": signals,
                   "trades": [t.to_dict() if hasattr(t, "to_dict") else getattr(t, "__dict__", t)
                              for t in res["trades"]]}, ensure_ascii=False))
conn.close()
""" % (START, END),
    ))

    return results


def main() -> None:
    results = run_all()
    for result in results:
        _report(result)
    _persist(results)


def _persist(results: list[dict]) -> None:
    """将本轮影子对账结果落盘，供 Step7 '连续 N 日通过' 留痕验证。"""
    out_dir = Path(__file__).resolve().parents[1] / "artifacts" / "shadow"
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y%m%d")
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{run_id}.json"

    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def table_sha256(path: Path, table: str, columns: tuple[str, ...]) -> str:
        """Hash only the source market rows used by the backtests.

        Full database hashes are useful provenance, but qsim has additional
        qsim_* tables so its file hash can never equal the legacy file hash.
        These canonical table hashes make source-data drift an explicit gate.
        """
        digest = hashlib.sha256()
        query_columns = ", ".join(columns)
        order_columns = ", ".join(columns)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cursor = conn.execute(
                f"SELECT {query_columns} FROM {table} ORDER BY {order_columns}"
            )
            while True:
                rows = cursor.fetchmany(10_000)
                if not rows:
                    break
                for row in rows:
                    digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode())
                    digest.update(b"\n")
        finally:
            conn.close()
        return digest.hexdigest()

    def table_coverage(path: Path, table: str) -> tuple[int, str | None]:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            count, max_date = conn.execute(
                f"SELECT COUNT(*), MAX(date) FROM {table}"
            ).fetchone()
        finally:
            conn.close()
        return int(count), str(max_date) if max_date is not None else None

    source_fingerprints: dict[str, dict[str, object]] = {}
    for name, table, columns in (
        ("bars", "bars", ("symbol", "date", "open", "high", "low", "close", "volume")),
        ("stock_bars", "stock_bars", ("ts_code", "date", "open", "high", "low", "close", "vol", "amount", "vwap")),
    ):
        qsim_hash = table_sha256(DB, table, columns)
        legacy_hash = table_sha256(LEGACY_ROOT / "data" / "quant.db", table, columns)
        qsim_count, qsim_max_date = table_coverage(DB, table)
        legacy_count, legacy_max_date = table_coverage(
            LEGACY_ROOT / "data" / "quant.db", table
        )
        source_fingerprints[name] = {
            "qsim_sha256": qsim_hash,
            "legacy_sha256": legacy_hash,
            "match": qsim_hash == legacy_hash,
            "qsim_rows": qsim_count,
            "legacy_rows": legacy_count,
            "qsim_max_date": qsim_max_date,
            "legacy_max_date": legacy_max_date,
        }
    source_data_match = all(
        bool(item["match"]) for item in source_fingerprints.values()
    )
    gate_note = None
    if not source_data_match:
        details = " | ".join(
            f"{name}: qsim={item['qsim_max_date']}/{item['qsim_rows']}行 "
            f"legacy={item['legacy_max_date']}/{item['legacy_rows']}行"
            for name, item in source_fingerprints.items() if not item["match"]
        )
        gate_note = (
            "数据门不一致。若 max_date 仅相差约一个交易日且行数接近，"
            "通常是同步时滞而非代码漂移。" + details
        )
    market_dates = [
        str(item["qsim_max_date"])
        for item in source_fingerprints.values()
        if item["match"] and item["qsim_max_date"] == item["legacy_max_date"]
        and item["qsim_max_date"] is not None
    ]
    source_market_as_of = min(market_dates) if market_dates else None

    payload = {
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "date": day,
        "start": START,
        "end": END,
        "all_ok": all(_case_ok(r) for r in results) and source_data_match,
        "gate_failures": [] if source_data_match else [
            name for name, item in source_fingerprints.items() if not item["match"]
        ],
        "gate_note": gate_note,
        "migration_policy": "initial_strict_parity",
        "source_market_as_of": source_market_as_of,
        "parity_strategies": [
            name for name in active_migration_names()
            if migration_for(name).state is not MigrationState.RETIRED
        ],
        "accepted_improvements": sorted(
            name for name in ACCEPTED_IMPROVEMENTS
            if any(r["name"] == name and not r["canonical_diff"].ok for r in results)
        ),
        "fingerprints": {
            "qsim_database_sha256": sha256(DB),
            "legacy_database_sha256": sha256(LEGACY_ROOT / "data" / "quant.db"),
            "qsim_shadow_script_sha256": sha256(Path(__file__)),
            "source_data_match": source_data_match,
            "source_tables": source_fingerprints,
        },
        "results": [
            {
                "name": r["name"],
                "canonical_ok": r["canonical_diff"].ok,
                "canonical_diffs": r["canonical_diff"].differences,
                "canonical_details": r["canonical_diff"].details[:20],
                "metric_ok": r["metric_diff"].ok,
                "metric_diffs": r["metric_diff"].differences,
                "metric_details": r["metric_diff"].details,
                "equity_ok": r["equity_diff"].ok,
                "equity_diffs": r["equity_diff"].differences,
                "equity_details": r["equity_diff"].details,
                "trade_ok": r["trade_diff"].ok,
                "trade_diffs": r["trade_diff"].differences,
                "trade_details": r["trade_diff"].details[:20],
                "qsim_metrics": r.get("qsim_metrics"),
                "old_metrics": r.get("old_metrics"),
                "accepted": (
                    r["name"] in ACCEPTED_IMPROVEMENTS
                    and _is_improvement_ok(r)
                ),
            }
            for r in results
        ],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    print(f"[shadow-persist] wrote {path} (all_ok={payload['all_ok']})")


if __name__ == "__main__":
    main()
