"""Daily, non-authoritative paper ledger for migration-pending strategies.

It uses the production execution engine but writes only a dated JSON artifact.
This keeps daily returns current while the formal qsim_* ledger remains reserved
for strategies whose migration state is ``paper_enabled``.
"""

from __future__ import annotations
from qsim.services.mutation_lock import serialized_mutation


import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pandas as pd

from qsim.services import paper_daily as paper
from qsim.storage.paper import PAPER_STRATEGIES, QUANT_DB, SCHEMA, seed_accounts
from qsim.strategies.migration import paper_enabled

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "artifacts" / "provisional_paper"
LATEST = OUT_DIR / "latest.json"


def _market_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{QUANT_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    pending = {name: cfg for name, cfg in PAPER_STRATEGIES.items() if not paper_enabled(name)}
    seed_accounts(conn, strategies=pending)
    return conn


def _window_days(bars: pd.DataFrame, start: str, end: str) -> list[str]:
    return [date for date in paper._trading_days(bars) if start <= date <= end]


def _last_common_date(source: sqlite3.Connection, names: list[str], requested_end: str | None) -> str:
    end_dates = [dt.date.today().strftime("%Y%m%d")]
    for name in names:
        cfg = PAPER_STRATEGIES[name]
        for symbol in (cfg.get("symbol"), cfg.get("fallback")):
            if not symbol:
                continue
            row = source.execute("SELECT MAX(date) FROM bars WHERE symbol=?", (symbol,)).fetchone()
            if not row or not row[0]:
                raise ValueError(f"missing market data for {name}:{symbol}")
            end_dates.append(str(row[0]))
    result = min(end_dates)
    return min(result, requested_end) if requested_end else result


def _run_daily(ledger: sqlite3.Connection, name: str, start: str, end: str) -> None:
    cfg = PAPER_STRATEGIES[name]
    bars = paper._load_bars(cfg["symbol"])
    days = _window_days(bars, start, end)
    if not days:
        return
    account = ledger.execute(
        "SELECT id FROM qsim_accounts WHERE strategy=?", (name,)
    ).fetchone()
    if not account:
        return
    paper._run_daily(ledger, name, bars, days, account["id"])


def _run_rotation(ledger: sqlite3.Connection, name: str, start: str, end: str) -> None:
    cfg = PAPER_STRATEGIES[name]
    bars_a = paper._load_bars(cfg["symbol"])
    bars_b = paper._load_bars(cfg["fallback"])
    days = _window_days(bars_a, start, end)
    account = ledger.execute("SELECT id FROM qsim_accounts WHERE strategy=?", (name,)).fetchone()
    if account and not bars_b.empty and days:
        paper._run_rotation(ledger, name, bars_a, bars_b, cfg["symbol"], cfg["fallback"], days, account["id"])


def _serialize(ledger: sqlite3.Connection, names: list[str]) -> list[dict]:
    rows = []
    for name in names:
        account = ledger.execute(
            "SELECT id, initial_capital FROM qsim_accounts WHERE strategy=?", (name,)
        ).fetchone()
        if not account:
            continue
        snapshots = [dict(row) for row in ledger.execute(
            "SELECT date, total_value, cash, position_value FROM qsim_snapshots "
            "WHERE strategy=? ORDER BY date", (name,)
        ).fetchall()]
        latest = snapshots[-1] if snapshots else {}
        positions = [dict(row) for row in ledger.execute(
            "SELECT ts_code, shares, avg_cost, updated_date FROM qsim_positions "
            "WHERE account_id=? AND shares>0 ORDER BY ts_code", (account["id"],)
        ).fetchall()]
        trades = [dict(row) for row in ledger.execute(
            "SELECT date, symbol, side, price, shares, amount, fee, reason FROM qsim_trades "
            "WHERE strategy=? ORDER BY id", (name,)
        ).fetchall()]
        initial = float(account["initial_capital"])
        total = float(latest.get("total_value") or initial)
        rows.append({
            "strategy": name,
            "ledger": "provisional",
            "as_of": str(latest.get("date")) if latest.get("date") else None,
            "initial_capital": initial,
            "total_value": round(total, 2),
            "cash": round(float(latest.get("cash") or initial), 2),
            "position_value": round(float(latest.get("position_value") or 0), 2),
            "total_return": total / initial - 1 if initial else 0.0,
            "day_return": (
                total / float(snapshots[-2]["total_value"]) - 1
                if len(snapshots) >= 2 and float(snapshots[-2]["total_value"]) else None
            ),
            "holdings_count": len(positions),
            "signal": "持" if positions else "空",
            "latest_trade_date": str(trades[-1]["date"]) if trades else None,
            "snapshots": snapshots,
            "positions": positions,
            "trades": trades,
        })
    return rows


def _write(payload: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = payload["generated_at"].replace(":", "").replace("-", "")
    dated = OUT_DIR / f"{stamp}.json"
    for path in (dated, LATEST):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    return dated


def _generation_fingerprint(source, names, start, end):
    digest = hashlib.sha256(json.dumps([names, start, end]).encode())
    for base in (ROOT / "qsim", ROOT / "config", ROOT / "data" / "strategies"):
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".json", ".yaml", ".yml") and "__pycache__" not in path.parts:
                digest.update(str(path.relative_to(ROOT)).encode())
                digest.update(path.read_bytes())
    # These strategies consume ETF bars only. Hash their actual input history,
    # including revisions, rather than invalidating on unrelated ledger writes.
    for row in source.execute("SELECT * FROM bars WHERE date<=? ORDER BY symbol,date", (end or "",)):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
    return digest.hexdigest()


@serialized_mutation
def run_provisional_paper(start: str = paper.BACKFILL_START, end: str | None = None) -> dict:
    """Rebuild pending strategies in a disposable ledger through the latest market date."""
    names = [name for name in PAPER_STRATEGIES if not paper_enabled(name)]
    source = _market_conn()
    ledger = None
    try:
        actual_end = _last_common_date(source, names, end) if names else None
        fingerprint = _generation_fingerprint(source, names, start, actual_end)
        try:
            prior = json.loads(LATEST.read_text())
        except (OSError, ValueError):
            prior = {}
        if prior.get("mode") == "provisional_paper" and prior.get("input_fingerprint") == fingerprint:
            return {"artifact": str(LATEST), **prior, "status": "up_to_date"}
        ledger = _ledger()
        if actual_end and actual_end >= start:
            for name in paper.DAILY_STRATEGIES:
                if name in names:
                    _run_daily(ledger, name, start, actual_end)
            for name in paper.ROTATION_STRATEGIES:
                if name in names:
                    _run_rotation(ledger, name, start, actual_end)
        payload = {
            "schema_version": 1,
            "input_fingerprint": fingerprint,
            "mode": "provisional_paper",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "start": start,
            "end": actual_end,
            "source_market_as_of": actual_end,
            "official_records_mutated": False,
            "description": "待对账策略的独立每日结算账本；用于显示当日收益，不构成正式迁移证据。",
            "strategies": _serialize(ledger, names),
        }
        path = _write(payload)
        return {"artifact": str(path), **payload}
    finally:
        if ledger is not None:
            ledger.close()
        source.close()


if __name__ == "__main__":
    result = run_provisional_paper()
    print(json.dumps({"artifact": result["artifact"], "end": result["end"],
                      "strategies": len(result["strategies"])}, ensure_ascii=False))
