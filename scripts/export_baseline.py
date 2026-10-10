#!/usr/bin/env python3
"""Export a read-only behavioral baseline from the legacy quant-sim tree.

The script intentionally does NOT import app modules.  It reads SQLite and files
from the old repo and writes JSON artifacts under a baseline directory, so the
new hybrid core can later be verified against the same inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, table: str, limit: int | None = None) -> list[dict[str, Any]]:
    sql = f"SELECT * FROM {table}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def _export_sqlite(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    conn = _connect_ro(path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        out["tables"] = tables
        out["counts"] = {}
        for table in tables:
            try:
                out["counts"][table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out["counts"][table] = None
    finally:
        conn.close()
    return out


def _row_sets(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    selected = {
        "account": None,
        "snapshots": None,
        "trades": None,
        "factor_account": None,
        "factor_snapshots": None,
        "factor_trades": None,
        "alpha_accounts": None,
        "alpha_snapshots": None,
        "alpha_trades": None,
        "strategy_accounts": None,
        "race_snapshots": None,
        "race_trades": None,
        "meta_nav": None,
        "meta_state": None,
        "engine_registry": None,
        "research_registry": None,
        "pilot_directive": None,
        "daily_run_log": 50,
        "event_log": 200,
        "process_checkpoints": 50,
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for table, limit in selected.items():
        try:
            out[table] = _rows(conn, table, limit)
        except sqlite3.Error as exc:
            out[table] = []
            print(f"skip {table}: {exc}")
    return out


def _strategy_hashes(source: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    strategy_dir = source / "data" / "strategies"
    if not strategy_dir.is_dir():
        return out
    for path in sorted(strategy_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        out[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="legacy quant-sim repo root")
    parser.add_argument("--out", type=Path, required=True, help="baseline output directory")
    args = parser.parse_args()

    source: Path = args.source.resolve()
    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    # config snapshot
    src_cfg = source / "config.yaml"
    if src_cfg.is_file():
        shutil.copy2(src_cfg, out / "config.yaml")

    # database manifest + schema
    db_manifest: dict[str, Any] = {}
    schema: dict[str, Any] = {}
    db_names = ["quant.db", "v2_market.db", "v2_events.db", "v2_shadow.db"]
    for name in db_names:
        db_path = source / "data" / name
        if not db_path.is_file():
            continue
        try:
            digest = _sha256(db_path)
        except OSError as exc:
            print(f"cannot hash {name}: {exc}")
            continue
        db_manifest[name] = {
            "path": str(db_path),
            "size": db_path.stat().st_size,
            "sha256": digest,
        }
        try:
            schema[name] = _export_sqlite(db_path)
        except sqlite3.Error as exc:
            print(f"cannot read schema {name}: {exc}")

    (out / "db_manifest.json").write_text(
        json.dumps(db_manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (out / "schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # production state
    quant_db = source / "data" / "quant.db"
    if quant_db.is_file():
        conn = _connect_ro(quant_db)
        try:
            row_sets = _row_sets(conn)
        finally:
            conn.close()
        (out / "accounts.json").write_text(
            json.dumps(row_sets, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # hot strategy hashes
    hashes = _strategy_hashes(source)
    (out / "strategy_hashes.json").write_text(
        json.dumps(hashes, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print(f"baseline written to {out}")
    print("db_manifest:", list(db_manifest))
    print("accounts tables:", list(row_sets.keys()) if quant_db.is_file() else [])
    print("strategy_hashes:", len(hashes))


if __name__ == "__main__":
    main()
