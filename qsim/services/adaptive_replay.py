"""Isolated, capital-adaptive replay for comparing paper execution rules.

The official qsim paper ledger remains the migration-gate audit record.  This
module replays the same strategies in an in-memory ledger and publishes only a
JSON artifact, so a retrospective execution-rule change can be inspected from
the simulated-paper start date without altering that evidence.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

import pandas as pd

from qsim.services import paper_daily as paper
from qsim.storage.paper import PAPER_STRATEGIES, QUANT_DB, SCHEMA, seed_accounts

ROOT = Path(__file__).resolve().parents[2]
REPLAY_DIR = ROOT / "artifacts" / "adaptive_replay"
LATEST = REPLAY_DIR / "latest.json"
REPLAY_START = paper.BACKFILL_START


def _market_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{QUANT_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _ledger_conn() -> sqlite3.Connection:
    """A disposable qsim ledger with the normal schema, held only in RAM."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    seed_accounts(conn)
    return conn


def _days(bars: pd.DataFrame, start: str, end: str) -> list[str]:
    return [d for d in paper._trading_days(bars) if start <= d <= end]


def _latest_common_end(source: sqlite3.Connection, requested_end: str | None) -> str:
    today = dt.date.today().strftime("%Y%m%d")
    latest = [today]
    for cfg in PAPER_STRATEGIES.values():
        if cfg.get("symbol"):
            row = source.execute(
                "SELECT MAX(date) FROM bars WHERE symbol=?", (cfg["symbol"],)
            ).fetchone()
            if row and row[0]:
                latest.append(str(row[0]))
        if cfg.get("fallback"):
            row = source.execute(
                "SELECT MAX(date) FROM bars WHERE symbol=?", (cfg["fallback"],)
            ).fetchone()
            if row and row[0]:
                latest.append(str(row[0]))
    row = source.execute("SELECT MAX(date) FROM stock_bars").fetchone()
    if row and row[0]:
        latest.append(str(row[0]))
    end = min(latest)
    return min(end, requested_end) if requested_end else end


def _run_daily(ledger: sqlite3.Connection, name: str, start: str, end: str) -> int:
    cfg = PAPER_STRATEGIES[name]
    bars = paper._load_bars(cfg["symbol"])
    days = _days(bars, start, end)
    if not days:
        return 0
    account = ledger.execute(
        "SELECT id, cash, shares, avg_cost, code_hash FROM qsim_accounts WHERE strategy=?",
        (name,),
    ).fetchone()
    if not account:
        return 0
    asof = {
        "date": str(bars.iloc[0]["date"].strftime("%Y%m%d")),
        "cash": float(account["cash"]), "shares": int(account["shares"]),
        "avg_cost": float(account["avg_cost"]), "pending_signal": "HOLD",
        "pending_weight": 0.0, "pending_date": None,
        "code_hash": account["code_hash"] or "", "prev_close": None,
    }
    trade_before = ledger.execute("SELECT COUNT(*) FROM qsim_trades").fetchone()[0]
    for index, day in enumerate(days):
        if index:
            paper._execute_signal(ledger, name, cfg["symbol"], bars, day, asof, account["id"])
        paper._run_daily_strategy(ledger, name, bars, day, asof)
        paper._snapshot_value(ledger, name, cfg["symbol"], bars, day, asof)
        bar = bars[bars["date"] == pd.to_datetime(day)]
        if not bar.empty:
            asof["prev_close"] = float(bar.iloc[0]["close"])
    return int(ledger.execute("SELECT COUNT(*) FROM qsim_trades").fetchone()[0] - trade_before)


def _run_rotation(ledger: sqlite3.Connection, name: str, start: str, end: str) -> int:
    cfg = PAPER_STRATEGIES[name]
    bars_a = paper._load_bars(cfg["symbol"])
    bars_b = paper._load_bars(cfg["fallback"])
    days = _days(bars_a, start, end)
    if bars_b.empty or not days:
        return 0
    account = ledger.execute("SELECT id FROM qsim_accounts WHERE strategy=?", (name,)).fetchone()
    return paper._run_rotation(
        ledger, name, bars_a, bars_b, cfg["symbol"], cfg["fallback"], days, account["id"],
    ) if account else 0


def _run_cross(ledger: sqlite3.Connection, source: sqlite3.Connection,
               name: str, start: str, end: str) -> int:
    days = [str(row[0]) for row in source.execute(
        "SELECT DISTINCT date FROM stock_bars WHERE date>=? AND date<=? ORDER BY date",
        (start, end),
    )]
    account = ledger.execute("SELECT id FROM qsim_accounts WHERE strategy=?", (name,)).fetchone()
    return paper._run_cross_sectional(ledger, name, days, account["id"], market_conn=source) if account else 0


def _official_rows(source: sqlite3.Connection) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for name, cfg in PAPER_STRATEGIES.items():
        account = source.execute(
            "SELECT initial_capital, cash, status FROM qsim_accounts WHERE strategy=?", (name,)
        ).fetchone()
        snapshot = source.execute(
            "SELECT date, total_value, cash, position_value FROM qsim_snapshots "
            "WHERE strategy=? ORDER BY date DESC LIMIT 1", (name,)
        ).fetchone()
        position_count = source.execute(
            "SELECT COUNT(*) FROM qsim_positions p JOIN qsim_accounts a ON a.id=p.account_id "
            "WHERE a.strategy=? AND p.shares>0", (name,)
        ).fetchone()[0]
        initial = float(account["initial_capital"]) if account else float(cfg["initial"])
        total = float(snapshot["total_value"]) if snapshot else initial
        rows[name] = {
            "as_of": str(snapshot["date"]) if snapshot else None,
            "initial_capital": initial,
            "total_value": round(total, 2),
            "cash": round(float(account["cash"]) if account else initial, 2),
            "position_value": round(float(snapshot["position_value"]) if snapshot else 0.0, 2),
            "holdings_count": int(position_count),
            "total_return": total / initial - 1 if initial else 0.0,
            "status": str(account["status"]) if account else "missing",
        }
    return rows


def _replay_rows(ledger: sqlite3.Connection, official: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for name, cfg in PAPER_STRATEGIES.items():
        account = ledger.execute(
            "SELECT initial_capital, cash FROM qsim_accounts WHERE strategy=?", (name,)
        ).fetchone()
        latest = ledger.execute(
            "SELECT date, total_value, cash, position_value FROM qsim_snapshots "
            "WHERE strategy=? ORDER BY date DESC LIMIT 1", (name,)
        ).fetchone()
        positions = [dict(row) for row in ledger.execute(
            "SELECT p.ts_code, p.shares, p.avg_cost, p.updated_date FROM qsim_positions p "
            "JOIN qsim_accounts a ON a.id=p.account_id WHERE a.strategy=? AND p.shares>0 "
            "ORDER BY p.ts_code", (name,),
        ).fetchall()]
        snapshots = [dict(row) for row in ledger.execute(
            "SELECT date, total_value, cash, position_value FROM qsim_snapshots "
            "WHERE strategy=? ORDER BY date", (name,)
        ).fetchall()]
        trades = [dict(row) for row in ledger.execute(
            "SELECT date, symbol, side, price, shares, amount, fee, reason FROM qsim_trades "
            "WHERE strategy=? ORDER BY date, id", (name,)
        ).fetchall()]
        initial = float(account["initial_capital"])
        total = float(latest["total_value"]) if latest else initial
        adaptive = {
            "as_of": str(latest["date"]) if latest else None,
            "initial_capital": initial,
            "total_value": round(total, 2),
            "cash": round(float(latest["cash"]) if latest else initial, 2),
            "position_value": round(float(latest["position_value"]) if latest else 0.0, 2),
            "holdings_count": len(positions),
            "total_return": total / initial - 1 if initial else 0.0,
            "positions": positions,
            "trades": trades,
            "snapshots": snapshots,
        }
        baseline = official[name]
        rows.append({
            "strategy": name,
            "kind": str(cfg["kind"]),
            "official": baseline,
            "adaptive": adaptive,
            "delta_total_value": round(total - float(baseline["total_value"]), 2),
            "delta_total_return": adaptive["total_return"] - float(baseline["total_return"]),
        })
    return rows


def _write_artifact(payload: dict) -> Path:
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = payload["generated_at"].replace(":", "").replace("-", "")
    target = REPLAY_DIR / f"{stamp}.json"
    for path in (target, LATEST):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    return target


def run_adaptive_replay(start: str = REPLAY_START, end: str | None = None) -> dict:
    """Replay all paper strategies from launch without mutating qsim_* tables."""
    source = _market_conn()
    ledger = _ledger_conn()
    try:
        actual_end = _latest_common_end(source, end)
        if actual_end < start:
            raise ValueError(f"no market data for replay window {start}..{actual_end}")
        official = _official_rows(source)
        trades = 0
        for name in paper.DAILY_STRATEGIES:
            trades += _run_daily(ledger, name, start, actual_end)
        for name in paper.ROTATION_STRATEGIES:
            trades += _run_rotation(ledger, name, start, actual_end)
        for name in sorted(paper.CROSS_STRATEGIES):
            trades += _run_cross(ledger, source, name, start, actual_end)
        market_as_of = source.execute("SELECT MAX(date) FROM stock_bars").fetchone()[0]
        payload = {
            "schema_version": 1,
            "mode": "capital_adaptive_replay",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "start": start,
            "end": actual_end,
            "source_market_as_of": str(market_as_of) if market_as_of else None,
            "official_paper_as_of": max(
                (v["as_of"] for v in official.values() if v.get("as_of")), default=None,
            ),
            "official_records_mutated": False,
            "description": "从模拟实盘启动日重放；横截面策略按实际本金和整手约束弹性收缩可持仓数。",
            "strategies": _replay_rows(ledger, official),
            "trade_count": trades,
        }
        path = _write_artifact(payload)
        return {"artifact": str(path), **payload}
    finally:
        ledger.close()
        source.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run isolated capital-adaptive paper replay")
    parser.add_argument("--start", default=REPLAY_START)
    parser.add_argument("--end")
    args = parser.parse_args()
    result = run_adaptive_replay(args.start, args.end)
    print(json.dumps({k: result[k] for k in (
        "artifact", "start", "end", "source_market_as_of", "official_paper_as_of", "trade_count",
    )}, ensure_ascii=False))
