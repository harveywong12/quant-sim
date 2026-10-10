from __future__ import annotations

import json
import sqlite3
import datetime as dt
from pathlib import Path

from qsim.market import legacy_adapter as adapter


QUANT_SCHEMA = {
    "bars": "CREATE TABLE bars (symbol TEXT, date TEXT, close REAL)",
    "stock_bars": "CREATE TABLE stock_bars (ts_code TEXT, date TEXT, close REAL)",
    "stock_info": "CREATE TABLE stock_info (ts_code TEXT, name TEXT)",
    "process_checkpoints": "CREATE TABLE process_checkpoints (id INTEGER PRIMARY KEY, stage TEXT)",
    "account": "CREATE TABLE account (id INTEGER PRIMARY KEY, marker TEXT)",
}
V2_SCHEMA = {
    "stk_close_daily": "CREATE TABLE stk_close_daily (ts_code TEXT, trade_date TEXT, close REAL)",
    "adj_factor": "CREATE TABLE adj_factor (ts_code TEXT, trade_date TEXT, adj_factor REAL)",
    "corporate_actions": "CREATE TABLE corporate_actions (ts_code TEXT, trade_date TEXT, action TEXT)",
}


def _create_database(path: Path, schemas: dict[str, str], marker: str) -> None:
    conn = sqlite3.connect(path)
    try:
        for table, schema in schemas.items():
            conn.execute(schema)
            columns = {
                "bars": ("513100", marker, 1.0),
                "stock_bars": ("000001.SZ", marker, 2.0),
                "stock_info": ("000001.SZ", marker),
                "process_checkpoints": (1, marker),
                "account": (1, marker),
                "stk_close_daily": ("000001.SZ", marker, 3.0),
                "adj_factor": ("000001.SZ", marker, 1.0),
                "corporate_actions": ("000001.SZ", marker, "none"),
            }[table]
            placeholders = ",".join("?" for _ in columns)
            conn.execute(f"INSERT INTO {table} VALUES ({placeholders})", columns)
        conn.commit()
    finally:
        conn.close()


def _create_target_quant(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        for table, schema in QUANT_SCHEMA.items():
            conn.execute(schema)
        conn.execute("DELETE FROM process_checkpoints")
        conn.execute("INSERT INTO process_checkpoints(id, stage) VALUES (1, 'target')")
        conn.execute("DELETE FROM account")
        conn.execute("INSERT INTO account(id, marker) VALUES (1, 'target')")
        conn.execute("CREATE TABLE qsim_accounts (id INTEGER PRIMARY KEY, strategy TEXT)")
        conn.execute("INSERT INTO qsim_accounts(id, strategy) VALUES (1, 'keep-me')")
        conn.execute("INSERT INTO bars VALUES ('old', 'old', 0.0)")
        conn.execute("INSERT INTO stock_bars VALUES ('old', 'old', 0.0)")
        conn.execute("INSERT INTO stock_info VALUES ('old', 'old')")
        conn.commit()
    finally:
        conn.close()


def _configure_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    legacy_root = tmp_path / "legacy"
    refactor_root = tmp_path / "refactor"
    (legacy_root / "data").mkdir(parents=True)
    (refactor_root / "data").mkdir(parents=True)
    monkeypatch.setattr(adapter, "LEGACY_ROOT", legacy_root)
    monkeypatch.setattr(adapter, "REFACTOR_ROOT", refactor_root)
    monkeypatch.setattr(adapter, "DATA_ROOT", refactor_root / "data")
    monkeypatch.setattr(adapter, "SYNC_LOCK", refactor_root / "data" / ".sync.lock")
    monkeypatch.setattr(adapter, "BACKUP_ROOT", tmp_path / "backups")
    return legacy_root, refactor_root


def test_sync_preserves_qsim_tables_and_records_lineage(monkeypatch, tmp_path: Path) -> None:
    legacy_root, refactor_root = _configure_paths(monkeypatch, tmp_path)
    _create_database(legacy_root / "data" / "quant.db", QUANT_SCHEMA, "legacy")
    _create_database(legacy_root / "data" / "v2_market.db", V2_SCHEMA, "legacy")
    _create_target_quant(refactor_root / "data" / "quant.db")
    _create_database(refactor_root / "data" / "v2_market.db", V2_SCHEMA, "old")

    result = adapter.sync_legacy_market_data()

    assert result["ok"] is True
    assert set(result["datasets"]) == {"quant.db", "v2_market.db"}
    conn = sqlite3.connect(refactor_root / "data" / "quant.db")
    try:
        assert conn.execute("SELECT close FROM bars").fetchone()[0] == 1.0
        assert conn.execute("SELECT stage FROM process_checkpoints").fetchone()[0] == "target"
        assert conn.execute("SELECT marker FROM account").fetchone()[0] == "target"
        assert conn.execute("SELECT strategy FROM qsim_accounts").fetchone()[0] == "keep-me"
        rows = conn.execute(
            "SELECT dataset, status, source_table_counts, source_table_watermarks "
            "FROM qsim_data_sync_runs ORDER BY dataset"
        ).fetchall()
    finally:
        conn.close()
    assert [(row[0], row[1]) for row in rows] == [
        ("quant.db", "success"),
        ("v2_market.db", "success"),
    ]
    assert json.loads(rows[0][2])["bars"] == 1
    assert json.loads(rows[0][3])["bars"]["date"] == "legacy"
    backup_names = {path.name for path in (tmp_path / "backups").iterdir()}
    assert len([name for name in backup_names if name.endswith("-quant.db")]) == 1
    assert len([name for name in backup_names if name.endswith("-v2_market.db")]) == 1
    assert len([name for name in backup_names if name.endswith("-manifest.json")]) == 1


def test_identical_source_is_recorded_without_replacing_target(
    monkeypatch, tmp_path: Path
) -> None:
    legacy_root, refactor_root = _configure_paths(monkeypatch, tmp_path)
    _create_database(legacy_root / "data" / "quant.db", QUANT_SCHEMA, "legacy")
    _create_database(legacy_root / "data" / "v2_market.db", V2_SCHEMA, "legacy")
    _create_target_quant(refactor_root / "data" / "quant.db")
    _create_database(refactor_root / "data" / "v2_market.db", V2_SCHEMA, "old")

    first = adapter.sync_legacy_market_data()
    second = adapter.sync_legacy_market_data()

    assert first["ok"] is True
    assert second["ok"] is True
    assert all(
        dataset["action"] == "skipped_unchanged"
        for dataset in second["datasets"].values()
    )
    conn = sqlite3.connect(refactor_root / "data" / "quant.db")
    try:
        rows = conn.execute(
            "SELECT dataset, status, sync_action FROM qsim_data_sync_runs "
            "WHERE run_id=? ORDER BY dataset",
            (second["run_id"],),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        ("quant.db", "success", "skipped_unchanged"),
        ("v2_market.db", "success", "skipped_unchanged"),
    ]
    assert len(list((tmp_path / "backups").glob("*-quant.db"))) == 1
    assert len(list((tmp_path / "backups").glob("*-v2_market.db"))) == 1
    assert len(list((tmp_path / "backups").glob("*-manifest.json"))) == 2


def test_sync_does_not_promote_when_required_source_table_is_missing(monkeypatch, tmp_path: Path) -> None:
    legacy_root, refactor_root = _configure_paths(monkeypatch, tmp_path)
    _create_database(legacy_root / "data" / "quant.db", QUANT_SCHEMA, "legacy")
    _create_database(
        legacy_root / "data" / "v2_market.db",
        {key: value for key, value in V2_SCHEMA.items() if key != "corporate_actions"},
        "legacy",
    )
    _create_target_quant(refactor_root / "data" / "quant.db")
    _create_database(refactor_root / "data" / "v2_market.db", V2_SCHEMA, "old")

    result = adapter.sync_legacy_market_data()

    assert result["ok"] is False
    conn = sqlite3.connect(refactor_root / "data" / "quant.db")
    try:
        assert conn.execute("SELECT close FROM bars").fetchone()[0] == 0.0
        assert conn.execute("SELECT strategy FROM qsim_accounts").fetchone()[0] == "keep-me"
    finally:
        conn.close()


def test_sync_rolls_back_previous_database_when_second_promotion_fails(
    monkeypatch, tmp_path: Path
) -> None:
    legacy_root, refactor_root = _configure_paths(monkeypatch, tmp_path)
    _create_database(legacy_root / "data" / "quant.db", QUANT_SCHEMA, "legacy")
    _create_database(legacy_root / "data" / "v2_market.db", V2_SCHEMA, "legacy")
    _create_target_quant(refactor_root / "data" / "quant.db")
    _create_database(refactor_root / "data" / "v2_market.db", V2_SCHEMA, "old")

    original = adapter._promote_staging

    def fail_v2(staging, target_path, preserve_qsim, allowed_tables):
        if target_path.name == "v2_market.db":
            raise RuntimeError("injected v2 promotion failure")
        return original(staging, target_path, preserve_qsim, allowed_tables)

    monkeypatch.setattr(adapter, "_promote_staging", fail_v2)
    result = adapter.sync_legacy_market_data()

    assert result["ok"] is False
    conn = sqlite3.connect(refactor_root / "data" / "quant.db")
    try:
        assert conn.execute("SELECT close FROM bars").fetchone()[0] == 0.0
        assert conn.execute("SELECT strategy FROM qsim_accounts").fetchone()[0] == "keep-me"
        statuses = conn.execute(
            "SELECT dataset, status FROM qsim_data_sync_runs "
            "WHERE run_id=? ORDER BY dataset",
            (result["run_id"],),
        ).fetchall()
    finally:
        conn.close()
    assert statuses == [("quant.db", "rolled_back"), ("v2_market.db", "failed")]


def test_expected_latest_trade_date_respects_calendar_and_coverage(
    monkeypatch, tmp_path: Path
) -> None:
    calendar = tmp_path / "trading-calendar.json"
    calendar.write_text(
        json.dumps(
            {
                "valid_from": "2026-01-01",
                "valid_through": "2026-12-31",
                "closed_dates": ["2026-10-01"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(adapter, "CALENDAR_FILE", calendar)

    shanghai = dt.timezone(dt.timedelta(hours=8))
    before_close = dt.datetime(2026, 10, 8, 10, 0, tzinfo=shanghai)
    after_close = dt.datetime(2026, 10, 8, 16, 0, tzinfo=shanghai)
    out_of_range = dt.datetime(2027, 1, 4, 10, 0, tzinfo=shanghai)

    assert adapter._expected_latest_trade_date(before_close) == "2026-10-07"
    assert adapter._expected_latest_trade_date(after_close) == "2026-10-08"
    assert adapter._expected_latest_trade_date(out_of_range) is None


def test_market_data_gate_rejects_stale_quant_watermark(monkeypatch) -> None:
    monkeypatch.setattr(adapter, "latest_sync_status", lambda: {
        "expected_latest_trade_date": "2026-09-01",
        "datasets": {"quant.db": {
            "status": "success", "freshness": "stale",
            "max_trade_date": "2026-08-31", "sync_action": "skipped_unchanged",
        }},
    })

    assert adapter.market_data_gate_status() == {
        "ok": False, "reason": "quant_stock_bars_stale",
        "expected_latest_trade_date": "2026-09-01",
        "source_market_as_of": "2026-08-31", "sync_action": "skipped_unchanged",
    }
