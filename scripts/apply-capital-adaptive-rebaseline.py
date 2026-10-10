#!/usr/bin/env python3
"""Promote the operator-authorized stock-pool replay into qsim paper records."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from qsim.services.paper_daily import run_paper
from qsim.storage.paper import QUANT_DB
from qsim.strategies.migration import paper_enabled

ROOT = Path(__file__).resolve().parents[1]
REPLAY = ROOT / "artifacts" / "adaptive_replay" / "latest.json"
OUT_DIR = ROOT / "artifacts" / "rebaseline"
TARGETS = ("multi_factor", "alpha158")


def _load_replay() -> dict:
    payload = json.loads(REPLAY.read_text(encoding="utf-8"))
    if payload.get("mode") != "capital_adaptive_replay":
        raise ValueError("latest replay is not capital_adaptive_replay")
    if payload.get("official_records_mutated") is not False:
        raise ValueError("replay must be an isolated pre-promotion artifact")
    found = {str(item.get("strategy")): item for item in payload.get("strategies", [])}
    missing = set(TARGETS) - set(found)
    if missing:
        raise ValueError(f"replay misses targets: {sorted(missing)}")
    return payload


def _state(conn: sqlite3.Connection, name: str) -> dict:
    account = conn.execute(
        "SELECT id, initial_capital, cash FROM qsim_accounts WHERE strategy=?", (name,)
    ).fetchone()
    if not account:
        raise ValueError(f"paper account missing: {name}")
    snapshot = conn.execute(
        "SELECT date, total_value, cash, position_value FROM qsim_snapshots "
        "WHERE strategy=? ORDER BY date DESC LIMIT 1", (name,)
    ).fetchone()
    positions = [dict(row) for row in conn.execute(
        "SELECT p.ts_code, p.shares, p.avg_cost, p.updated_date FROM qsim_positions p "
        "WHERE p.account_id=? AND p.shares>0 ORDER BY p.ts_code", (account["id"],)
    ).fetchall()]
    return {
        "snapshot": dict(snapshot) if snapshot else None,
        "cash": float(account["cash"]),
        "positions": positions,
        "snapshot_history": [dict(row) for row in conn.execute(
            "SELECT date, total_value, cash, position_value, strategy_version "
            "FROM qsim_snapshots WHERE strategy=? ORDER BY date", (name,)
        ).fetchall()],
        "trade_history": [dict(row) for row in conn.execute(
            "SELECT date, symbol, side, price, shares, amount, fee, reason, code_hash "
            "FROM qsim_trades WHERE strategy=? ORDER BY id", (name,)
        ).fetchall()],
        "snapshot_count": int(conn.execute(
            "SELECT COUNT(*) FROM qsim_snapshots WHERE strategy=?", (name,)
        ).fetchone()[0]),
        "trade_count": int(conn.execute(
            "SELECT COUNT(*) FROM qsim_trades WHERE strategy=?", (name,)
        ).fetchone()[0]),
    }


def _validate(state: dict, expected: dict, name: str) -> None:
    snapshot = state.get("snapshot") or {}
    for key in ("as_of", "total_value", "cash", "position_value"):
        actual = snapshot.get("date") if key == "as_of" else snapshot.get(key)
        wanted = expected.get(key)
        if key == "as_of":
            if str(actual) != str(wanted):
                raise RuntimeError(f"{name}: date mismatch {actual} != {wanted}")
        elif round(float(actual or 0), 2) != round(float(wanted or 0), 2):
            raise RuntimeError(f"{name}: {key} mismatch {actual} != {wanted}")
    actual_positions = [(p["ts_code"], int(p["shares"])) for p in state["positions"]]
    expected_positions = sorted((str(p["ts_code"]), int(p["shares"])) for p in expected.get("positions", []))
    if actual_positions != expected_positions:
        raise RuntimeError(f"{name}: position mismatch")
    if state["snapshot_count"] != len(expected.get("snapshots", [])):
        raise RuntimeError(f"{name}: snapshot count mismatch")
    if state["trade_count"] != len(expected.get("trades", [])):
        raise RuntimeError(f"{name}: trade count mismatch")


def main() -> None:
    if not all(paper_enabled(name) for name in TARGETS):
        raise RuntimeError("targets must be explicitly paper_enabled by the operator rebaseline manifest")
    replay = _load_replay()
    if replay.get("end") != replay.get("source_market_as_of"):
        raise RuntimeError("replay does not reach its source market watermark")
    conn = sqlite3.connect(QUANT_DB)
    conn.row_factory = sqlite3.Row
    try:
        before = {name: _state(conn, name) for name in TARGETS}
    finally:
        conn.close()
    stats = run_paper(start=str(replay["start"]), end=str(replay["end"]))
    expected = {item["strategy"]: item["adaptive"] for item in replay["strategies"]}
    conn = sqlite3.connect(QUANT_DB)
    conn.row_factory = sqlite3.Row
    try:
        after = {name: _state(conn, name) for name in TARGETS}
    finally:
        conn.close()
    for name in TARGETS:
        _validate(after[name], expected[name], name)
    now = dt.datetime.now(dt.timezone.utc)
    record = {
        "kind": "operator_capital_adaptive_rebaseline",
        "at": now.isoformat(timespec="seconds"),
        "targets": list(TARGETS),
        "replay": str(REPLAY.relative_to(ROOT)),
        "replay_sha256": hashlib.sha256(REPLAY.read_bytes()).hexdigest(),
        "paper_stats": stats,
        "before": before,
        "after": after,
        "validated": True,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    print(json.dumps({"ok": True, "record": str(path), "stats": stats}, ensure_ascii=False))


if __name__ == "__main__":
    main()
