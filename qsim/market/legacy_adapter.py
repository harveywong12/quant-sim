"""Controlled, read-only import of legacy market databases into qsim.

The legacy project remains the upstream data producer during migration.  This
module takes a consistent SQLite backup of each legacy database, validates the
staging copy, and imports only non-qsim tables into the qsim database in one
transaction.  qsim-owned ``qsim_*`` tables are never replaced.
"""

from __future__ import annotations
from qsim.services.mutation_lock import serialized_mutation


import datetime as dt
import fcntl
import hashlib
import json
import logging
import math
import os
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-Unix hosts
    fcntl = None

logger = logging.getLogger("qsim.market.legacy_adapter")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROOT = Path(os.environ.get("QSIM_LEGACY_ROOT", "/srv/quant-sim"))
REFACTOR_ROOT = Path(
    os.environ.get("QSIM_REFACTOR_ROOT", str(PROJECT_ROOT))
)
DATA_ROOT = REFACTOR_ROOT / "data"
SYNC_LOCK = DATA_ROOT / ".qsim-data-sync.lock"
SOURCE_WRITE_LOCK = LEGACY_ROOT / "data" / ".quant.db.write.lock"
BACKUP_ROOT = Path(
    os.environ.get("QSIM_SYNC_BACKUP_ROOT", "/srv/qsim-backups/data-sync")
)
CALENDAR_FILE = Path(
    os.environ.get(
        "QSIM_CALENDAR_FILE", str(REFACTOR_ROOT / "config" / "trading-calendar.json")
    )
)
BACKUP_RETENTION = max(1, int(os.environ.get("QSIM_SYNC_BACKUP_RETENTION", "7")))
_THREAD_SYNC_LOCK = threading.Lock()
_THREAD_SOURCE_WRITE_LOCK = threading.Lock()

@dataclass(frozen=True)
class DatasetSpec:
    name: str
    required_tables: tuple[str, ...]
    allowed_tables: tuple[str, ...]
    watermark_columns: tuple[tuple[str, tuple[str, ...]], ...]


DATASETS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        "quant.db",
        required_tables=("bars", "stock_bars", "stock_info"),
        allowed_tables=(
            "bars",
            "stock_bars",
            "stock_info",
            "stock_fundamental",
            "stock_universe",
            "stock_universe_history",
            "stock_valuation",
        ),
        watermark_columns=(
            ("bars", ("date",)),
            ("stock_bars", ("date",)),
            ("stock_fundamental", ("ann_date", "end_date")),
            ("stock_universe", ("trade_date",)),
            ("stock_universe_history", ("trade_date",)),
            ("stock_valuation", ("date",)),
        ),
    ),
    DatasetSpec(
        "v2_market.db",
        required_tables=("stk_close_daily", "adj_factor", "corporate_actions"),
        allowed_tables=(
            "adj_factor",
            "cb_basic_snapshot",
            "cb_daily_raw",
            "cb_indicator",
            "corporate_actions",
            "premium_daily",
            "stk_close_daily",
            "stock_name_history",
        ),
        watermark_columns=(
            ("adj_factor", ("trade_date",)),
            ("cb_basic_snapshot", ("list_date", "delist_date", "maturity_date")),
            ("cb_daily_raw", ("trade_date",)),
            ("cb_indicator", ("trade_date",)),
            ("corporate_actions", ("ex_date",)),
            ("premium_daily", ("date",)),
            ("stk_close_daily", ("trade_date",)),
            ("stock_name_history", ("start_date", "end_date", "ann_date")),
        ),
    ),
)

SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS qsim_data_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    dataset TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    source_size INTEGER,
    source_mtime_ns INTEGER,
    source_sha256 TEXT,
    source_table_counts TEXT,
    source_table_watermarks TEXT,
    sync_action TEXT,
    backup_path TEXT,
    error TEXT,
    UNIQUE(run_id, dataset)
);
CREATE INDEX IF NOT EXISTS idx_qsim_data_sync_runs_dataset
    ON qsim_data_sync_runs(dataset, id DESC);
"""


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def _backup_sqlite(source: Path, destination: Path) -> None:
    source_conn = _connect_readonly(source)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn, pages=1000)
        destination_conn.commit()
    finally:
        destination_conn.close()
        source_conn.close()


def _table_names(conn: sqlite3.Connection, schema: str = "main") -> list[str]:
    rows = conn.execute(
        f"SELECT name FROM {_quote(schema)}.sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _has_table(conn: sqlite3.Connection, table: str, schema: str = "main") -> bool:
    return conn.execute(
        f"SELECT 1 FROM {_quote(schema)}.sqlite_master "
        "WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _table_columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    rows = conn.execute(
        f"PRAGMA {_quote(schema)}.table_info({_quote(table)})"
    ).fetchall()
    return [str(row[1]) for row in rows]


def _table_counts(conn: sqlite3.Connection, tables: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = int(
            conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
        )
    return counts


def _table_watermarks(
    conn: sqlite3.Connection,
    tables: list[str],
    watermark_columns: tuple[tuple[str, tuple[str, ...]], ...],
    schema: str = "main",
) -> dict[str, dict[str, str | None]]:
    """Return per-table MAX values for explicitly declared date columns."""
    configured = dict(watermark_columns)
    result: dict[str, dict[str, str | None]] = {}
    for table in tables:
        columns = set(_table_columns(conn, table, schema))
        values: dict[str, str | None] = {}
        for column in configured.get(table, ()):
            if column not in columns:
                continue
            value = conn.execute(
                f"SELECT MAX({_quote(column)}) FROM {_quote(schema)}.{_quote(table)}"
            ).fetchone()[0]
            values[column] = _normalise_date_value(value)
        if values:
            result[table] = values
    return result


def _normalise_date_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text or None


def _load_calendar_payload() -> dict | None:
    try:
        payload = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        valid_from = dt.date.fromisoformat(str(payload["valid_from"]))
        valid_through = dt.date.fromisoformat(str(payload["valid_through"]))
        closed_dates = {str(value) for value in payload["closed_dates"]}
        if valid_from > valid_through:
            return None
        return {
            "valid_from": valid_from,
            "valid_through": valid_through,
            "closed_dates": closed_dates,
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _expected_latest_trade_date(now: dt.datetime | None = None) -> str | None:
    """Return the latest session whose EOD data should be available now."""
    payload = _load_calendar_payload()
    if not payload:
        return None
    now = now or dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    else:
        now = now.astimezone(dt.timezone(dt.timedelta(hours=8)))
    if not payload["valid_from"] <= now.date() <= payload["valid_through"]:
        return None
    candidate = now.date()
    minutes = now.hour * 60 + now.minute
    if candidate.weekday() >= 5 or candidate.isoformat() in payload["closed_dates"]:
        pass
    elif minutes < 15 * 60 + 30:
        candidate -= dt.timedelta(days=1)
    while candidate >= payload["valid_from"]:
        if (
            candidate.weekday() < 5
            and candidate.isoformat() not in payload["closed_dates"]
        ):
            return candidate.isoformat()
        candidate -= dt.timedelta(days=1)
    return None


def _date_key(value: object | None) -> str:
    """Compare database and calendar dates independent of their separators."""
    return "".join(char for char in str(value or "") if char.isdigit())[:8]


def _validate_staging(
    path: Path,
    required_tables: tuple[str, ...],
    allowed_tables: tuple[str, ...],
) -> dict[str, int]:
    conn = _connect_readonly(path)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise RuntimeError(f"staging integrity check failed: {integrity}")
        tables = set(_table_names(conn))
        missing = [table for table in required_tables if table not in tables]
        if missing:
            raise RuntimeError(f"staging missing required tables: {missing}")
        available_allowed = [table for table in allowed_tables if table in tables]
        counts = _table_counts(conn, available_allowed)
        empty = [table for table in required_tables if counts.get(table, 0) == 0]
        if empty:
            raise RuntimeError(f"required staging tables are empty: {empty}")
        return counts
    finally:
        conn.close()


def _create_missing_tables(
    target: sqlite3.Connection,
    incoming: sqlite3.Connection,
    allowed_tables: set[str],
) -> None:
    target_tables = set(_table_names(target))
    for table in _table_names(incoming, "incoming"):
        if table not in allowed_tables:
            continue
        if table in target_tables:
            continue
        row = incoming.execute(
            "SELECT sql FROM incoming.sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row or not row[0]:
            raise RuntimeError(f"missing CREATE TABLE SQL for {table}")
        target.execute(str(row[0]))
        target_tables.add(table)


def _copy_indexes(
    target: sqlite3.Connection,
    incoming: sqlite3.Connection,
    allowed_tables: set[str],
) -> None:
    existing = {
        str(row[0])
        for row in target.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    rows = incoming.execute(
        "SELECT name, sql, tbl_name FROM incoming.sqlite_master "
        "WHERE type='index' AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    for name, sql, table in rows:
        name = str(name)
        if name in existing or str(table) not in allowed_tables:
            continue
        statement = str(sql)
        if statement.upper().startswith("CREATE UNIQUE INDEX "):
            statement = "CREATE UNIQUE INDEX IF NOT EXISTS " + statement[len("CREATE UNIQUE INDEX "):]
        elif statement.upper().startswith("CREATE INDEX "):
            statement = "CREATE INDEX IF NOT EXISTS " + statement[len("CREATE INDEX "):]
        target.execute(statement)


def _promote_staging(
    staging: Path,
    target_path: Path,
    preserve_qsim: bool,
    allowed_tables: tuple[str, ...],
) -> None:
    allowed = set(allowed_tables)
    target = sqlite3.connect(target_path)
    try:
        target.execute("PRAGMA busy_timeout=30000")
        target.execute("PRAGMA foreign_keys=OFF")
        target.execute("ATTACH DATABASE ? AS incoming", (str(staging),))
        try:
            target.execute("BEGIN IMMEDIATE")
            _create_missing_tables(target, target, allowed)
            incoming_tables = _table_names(target, "incoming")
            for table in incoming_tables:
                if table not in allowed:
                    continue
                if preserve_qsim and table.startswith("qsim_"):
                    continue
                incoming_columns = _table_columns(target, table, "incoming")
                target_columns = _table_columns(target, table)
                if incoming_columns != target_columns:
                    raise RuntimeError(
                        f"schema mismatch for {table}: "
                        f"incoming={incoming_columns}, target={target_columns}"
                    )
                quoted = _quote(table)
                columns = ", ".join(_quote(column) for column in incoming_columns)
                target.execute(f"DELETE FROM main.{quoted}")
                target.execute(
                    f"INSERT INTO main.{quoted} ({columns}) "
                    f"SELECT {columns} FROM incoming.{quoted}"
                )

            if _has_table(target, "sqlite_sequence", "incoming") and allowed:
                placeholders = ",".join("?" for _ in allowed)
                params = tuple(sorted(allowed))
                target.execute(
                    "DELETE FROM main.sqlite_sequence WHERE name IN (" + placeholders + ")",
                    params,
                )
                target.execute(
                    "INSERT INTO main.sqlite_sequence(name, seq) "
                    "SELECT name, seq FROM incoming.sqlite_sequence "
                    "WHERE name IN (" + placeholders + ")",
                    params,
                )
            _copy_indexes(target, target, allowed)
            target.commit()
        except Exception:
            target.rollback()
            raise
        finally:
            target.execute("DETACH DATABASE incoming")
    finally:
        target.close()


def _ensure_sync_schema() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA_ROOT / "quant.db")
    try:
        conn.executescript(SYNC_SCHEMA)
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(qsim_data_sync_runs)")
        }
        migrations = {
            "source_table_watermarks": "TEXT",
            "sync_action": "TEXT",
        }
        for column, declaration in migrations.items():
            if column not in columns:
                conn.execute(
                    f"ALTER TABLE qsim_data_sync_runs ADD COLUMN {_quote(column)} {declaration}"
                )
        conn.commit()
    finally:
        conn.close()


def _record_run(
    *,
    run_id: str,
    dataset: str,
    status: str,
    started_at: str,
    finished_at: str | None = None,
    source: Path | None = None,
    snapshot: Path | None = None,
    source_counts: dict[str, int] | None = None,
    source_watermarks: dict[str, dict[str, str | None]] | None = None,
    source_sha256: str | None = None,
    sync_action: str | None = None,
    backup_path: Path | None = None,
    error: str | None = None,
) -> None:
    conn = sqlite3.connect(DATA_ROOT / "quant.db")
    try:
        conn.execute(
            "INSERT INTO qsim_data_sync_runs "
            "(run_id, dataset, status, started_at, finished_at, source_size, "
            "source_mtime_ns, source_sha256, source_table_counts, "
            "source_table_watermarks, sync_action, backup_path, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, dataset) DO UPDATE SET status=excluded.status, "
            "finished_at=excluded.finished_at, source_size=excluded.source_size, "
            "source_mtime_ns=excluded.source_mtime_ns, source_sha256=excluded.source_sha256, "
            "source_table_counts=excluded.source_table_counts, "
            "source_table_watermarks=excluded.source_table_watermarks, "
            "sync_action=excluded.sync_action, backup_path=excluded.backup_path, "
            "error=excluded.error",
            (
                run_id,
                dataset,
                status,
                started_at,
                finished_at,
                snapshot.stat().st_size if snapshot and snapshot.exists() else None,
                source.stat().st_mtime_ns if source and source.exists() else None,
                source_sha256
                or (_sha256(snapshot) if snapshot and snapshot.exists() else None),
                json.dumps(source_counts, sort_keys=True) if source_counts else None,
                json.dumps(source_watermarks, sort_keys=True)
                if source_watermarks
                else None,
                sync_action,
                str(backup_path) if backup_path else None,
                error,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _sync_guard() -> Iterator[None]:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    with SYNC_LOCK.open("a+", encoding="utf-8") as lock:
        if fcntl is None:
            acquired = _THREAD_SYNC_LOCK.acquire(blocking=False)
            if not acquired:
                raise RuntimeError("market data sync is already running")
            try:
                yield
            finally:
                _THREAD_SYNC_LOCK.release()
            return
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("market data sync is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def _source_write_guard() -> Iterator[None]:
    """Serialize snapshots with legacy writers using the shared sidecar lock."""
    SOURCE_WRITE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    timeout = max(1.0, float(os.environ.get("QSIM_SOURCE_WRITE_LOCK_TIMEOUT", "900")))
    deadline = dt.datetime.now(dt.timezone.utc).timestamp() + timeout
    with SOURCE_WRITE_LOCK.open("a+", encoding="utf-8") as lock:
        try:
            os.chmod(SOURCE_WRITE_LOCK, 0o600)
        except OSError:
            pass
        if fcntl is None:
            acquired = _THREAD_SOURCE_WRITE_LOCK.acquire(timeout=timeout)
            if not acquired:
                raise TimeoutError("legacy database write lock timed out")
            try:
                yield
            finally:
                _THREAD_SOURCE_WRITE_LOCK.release()
            return
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if dt.datetime.now(dt.timezone.utc).timestamp() >= deadline:
                    raise TimeoutError("legacy database write lock timed out") from exc
                import time

                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _new_staging_path(dataset: str) -> Path:
    fd, name = tempfile.mkstemp(
        prefix=f".{dataset}.stage2-", suffix=".db", dir=str(DATA_ROOT)
    )
    os.close(fd)
    path = Path(name)
    path.unlink()
    return path


def _backup_target(target: Path, run_id: str, dataset: str) -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_ROOT / f"{run_id}-{dataset}"
    _backup_sqlite(target, backup)
    return backup


def _latest_success_metadata(dataset: str) -> dict[str, object] | None:
    path = DATA_ROOT / "quant.db"
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT source_size, source_sha256 FROM qsim_data_sync_runs "
            "WHERE dataset=? AND status='success' ORDER BY id DESC LIMIT 1",
            (dataset,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _prune_backup_history() -> None:
    """Keep the newest adapter-owned manifest bundles; never touch other files."""
    if not BACKUP_ROOT.is_dir():
        return
    manifests = sorted(
        BACKUP_ROOT.glob("*-manifest.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for manifest_path in manifests[BACKUP_RETENTION:]:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("status") not in {"committed", "rolled_back"}:
            continue
        prefix = manifest_path.name.removesuffix("-manifest.json")
        for candidate in BACKUP_ROOT.glob(f"{prefix}-*"):
            if candidate.is_file() and candidate.parent == BACKUP_ROOT:
                candidate.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)


def _sync_legacy_market_data_unlocked() -> dict:
    """Synchronize legacy market databases while preserving qsim-owned state."""
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    started_at = _now()
    results: dict[str, dict] = {}
    staging: list[
        tuple[
            DatasetSpec,
            Path,
            Path,
            Path,
            dict[str, int],
            dict[str, dict[str, str | None]],
            str,
            bool,
        ]
    ] = []
    backups: dict[str, Path] = {}
    manifest_path: Path | None = None
    promoted: list[str] = []
    try:
        with _sync_guard():
            _ensure_sync_schema()
            for spec in DATASETS:
                source = LEGACY_ROOT / "data" / spec.name
                target = DATA_ROOT / spec.name
                _record_run(run_id=run_id, dataset=spec.name, status="running", started_at=started_at, source=source)
                try:
                    if not source.is_file():
                        raise FileNotFoundError(f"legacy database not found: {source}")
                    stage = _new_staging_path(spec.name)
                    _backup_sqlite(source, stage)
                    counts = _validate_staging(
                        stage, spec.required_tables, spec.allowed_tables
                    )
                    stage_conn = _connect_readonly(stage)
                    try:
                        watermarks = _table_watermarks(
                            stage_conn,
                            list(counts),
                            spec.watermark_columns,
                        )
                    finally:
                        stage_conn.close()
                    source_sha256 = _sha256(stage)
                    previous = _latest_success_metadata(spec.name)
                    unchanged = bool(
                        target.exists()
                        and previous
                        and previous.get("source_sha256") == source_sha256
                        and previous.get("source_size") == stage.stat().st_size
                    )
                    staging.append(
                        (
                            spec,
                            source,
                            target,
                            stage,
                            counts,
                            watermarks,
                            source_sha256,
                            unchanged,
                        )
                    )
                    results[spec.name] = {
                        "status": "staged",
                        "source_table_counts": counts,
                        "source_table_watermarks": watermarks,
                        "action": "skipped_unchanged" if unchanged else "replace",
                    }
                except Exception as exc:
                    _record_run(
                        run_id=run_id,
                        dataset=spec.name,
                        status="failed",
                        started_at=started_at,
                        finished_at=_now(),
                        source=source,
                        error=str(exc),
                    )
                    results[spec.name] = {"status": "failed", "error": str(exc)}
                    raise

            BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
            manifest_path = BACKUP_ROOT / f"{run_id}-manifest.json"
            manifest = {
                "run_id": run_id,
                "status": "prepared",
                "datasets": {},
            }
            for spec, source, target, stage, counts, watermarks, source_sha256, unchanged in staging:
                backup = (
                    _backup_target(target, run_id, spec.name)
                    if target.exists() and not unchanged
                    else None
                )
                if backup:
                    backups[spec.name] = backup
                manifest["datasets"][spec.name] = {
                    "target": str(target),
                    "stage": str(stage),
                    "backup": str(backup) if backup else None,
                    "allowed_tables": list(spec.allowed_tables),
                    "source_sha256": source_sha256,
                    "source_table_watermarks": watermarks,
                    "action": "skipped_unchanged" if unchanged else "replace",
                }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            manifest["status"] = "promoting"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            for spec, source, target, stage, counts, watermarks, source_sha256, unchanged in staging:
                if unchanged:
                    results[spec.name] = {
                        "status": "success",
                        "action": "skipped_unchanged",
                        "source_table_counts": counts,
                        "source_table_watermarks": watermarks,
                    }
                    continue
                try:
                    _promote_staging(
                        stage,
                        target,
                        preserve_qsim=spec.name == "quant.db",
                        allowed_tables=spec.allowed_tables,
                    )
                    promoted.append(spec.name)
                    results[spec.name] = {
                        "status": "promoted",
                        "source_table_counts": counts,
                        "source_table_watermarks": watermarks,
                    }
                except Exception as exc:
                    results[spec.name] = {"status": "failed", "error": str(exc)}
                    raise
            manifest["status"] = "committed"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            for spec, source, _target, _stage, counts, watermarks, source_sha256, unchanged in staging:
                _record_run(
                    run_id=run_id,
                    dataset=spec.name,
                    status="success",
                    started_at=started_at,
                    finished_at=_now(),
                    source=source,
                    snapshot=_stage,
                    source_counts=counts,
                    source_watermarks=watermarks,
                    source_sha256=source_sha256,
                    sync_action="skipped_unchanged" if unchanged else "replace",
                    backup_path=backups.get(spec.name),
                )
                results[spec.name]["status"] = "success"
            _prune_backup_history()
        return {"ok": True, "run_id": run_id, "datasets": results}
    except Exception as exc:
        rollback_errors: list[str] = []
        for spec, _source, target, _stage, _counts, _watermarks, _source_sha256, unchanged in reversed(staging):
            backup = backups.get(spec.name)
            if unchanged or not backup or spec.name not in promoted:
                continue
            try:
                _promote_staging(
                    backup,
                    target,
                    preserve_qsim=spec.name == "quant.db",
                    allowed_tables=spec.allowed_tables,
                )
                results[spec.name] = {
                    "status": "rolled_back",
                    "error": "sync aborted; restored from pre-run backup",
                }
                _record_run(
                    run_id=run_id,
                    dataset=spec.name,
                    status="rolled_back",
                    started_at=started_at,
                    finished_at=_now(),
                    source=_source,
                    source_counts=_counts,
                    backup_path=backup,
                    error="sync aborted; restored from pre-run backup",
                )
            except Exception as rollback_exc:
                rollback_errors.append(f"{spec.name}: {rollback_exc}")
                results[spec.name] = {
                    "status": "rollback_failed",
                    "error": str(rollback_exc),
                }
        for spec, source, _target, stage, counts, watermarks, source_sha256, _unchanged in staging:
            if results.get(spec.name, {}).get("status") in {"success", "rolled_back"}:
                continue
            current_error = results.get(spec.name, {}).get("error", str(exc))
            if results.get(spec.name, {}).get("status") != "rollback_failed":
                results[spec.name] = {"status": "failed", "error": current_error}
            _record_run(
                run_id=run_id,
                dataset=spec.name,
                status=results.get(spec.name, {}).get("status", "failed"),
                started_at=started_at,
                finished_at=_now(),
                source=source,
                snapshot=stage,
                source_counts=counts,
                source_watermarks=watermarks,
                source_sha256=source_sha256,
                backup_path=backups.get(spec.name),
                error=results.get(spec.name, {}).get("error", str(exc)),
            )
        if manifest_path and manifest_path.exists():
            manifest["status"] = "rollback_failed" if rollback_errors else "rolled_back"
            manifest["error"] = str(exc)
            if rollback_errors:
                manifest["rollback_errors"] = rollback_errors
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        logger.exception("legacy market sync failed: %s", exc)
        return {
            "ok": False,
            "run_id": run_id,
            "error": str(exc),
            "rollback_errors": rollback_errors,
            "datasets": results,
        }
    finally:
        for _spec, _source, _target, stage, _counts, _watermarks, _source_sha256, _unchanged in staging:
            stage.unlink(missing_ok=True)


@serialized_mutation
def sync_legacy_market_data() -> dict:
    """Synchronize legacy market data without racing an upstream writer."""
    from qsim.services import native_market
    if native_market.enabled():
        return native_market.run()
    with _source_write_guard():
        return _sync_legacy_market_data_unlocked()


def latest_sync_status() -> dict:
    """Return the latest persisted sync result per dataset."""
    path = DATA_ROOT / "quant.db"
    if not path.is_file():
        return {"ok": True, "datasets": {}}
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='qsim_data_sync_runs'"
        ).fetchone()
        if not exists:
            return {"ok": True, "datasets": {}}
        rows = conn.execute(
            "SELECT * FROM qsim_data_sync_runs ORDER BY id DESC"
        ).fetchall()
        datasets: dict[str, dict] = {}
        for row in rows:
            dataset = str(row["dataset"])
            if dataset in datasets:
                continue
            try:
                counts = json.loads(row["source_table_counts"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                counts = {}
            try:
                watermark_json = (
                    row["source_table_watermarks"]
                    if "source_table_watermarks" in row.keys()
                    else None
                )
                watermarks = json.loads(watermark_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                watermarks = {}
            primary_table = "stock_bars" if dataset == "quant.db" else "stk_close_daily"
            primary_values = watermarks.get(primary_table, {})
            primary_date = max(primary_values.values(), default=None)
            # Do not let future-looking fields such as convertible maturity
            # dates masquerade as market-data freshness.
            max_trade_date = primary_date
            expected = _expected_latest_trade_date()
            if row["status"] not in {"success", "rolled_back"}:
                freshness = "unknown"
            elif not expected or not primary_date:
                freshness = "unknown"
            else:
                freshness = (
                    "fresh" if _date_key(primary_date) >= _date_key(expected) else "stale"
                )
            datasets[dataset] = {
                "run_id": row["run_id"],
                "status": row["status"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "source_size": row["source_size"],
                "source_mtime_ns": row["source_mtime_ns"],
                "source_sha256": row["source_sha256"],
                "source_table_counts": counts,
                "source_table_watermarks": watermarks,
                "max_trade_date": max_trade_date,
                "expected_latest_trade_date": expected,
                "freshness": freshness,
                "sync_action": row["sync_action"] if "sync_action" in row.keys() else None,
                "backup_path": row["backup_path"],
                "error": row["error"],
            }
        latest_run_id = max(
            (item["run_id"] for item in datasets.values()), default=None
        )
        return {
            "ok": True,
            "expected_latest_trade_date": _expected_latest_trade_date(),
            "latest_run_id": latest_run_id,
            "datasets": datasets,
        }
    finally:
        conn.close()


def market_data_gate_status() -> dict:
    """Return whether cross-sectional paper may consume the latest sync.

    A completed copy is not sufficient: the copied ``quant.db`` must contain
    the expected session's stock bars. When a universe snapshot is available,
    require at least 95% of that snapshot as well.
    """
    status = latest_sync_status()
    expected = status.get("expected_latest_trade_date")
    dataset = (status.get("datasets") or {}).get("quant.db") or {}
    if not expected:
        return {"ok": False, "reason": "expected_trade_date_unknown", "expected_latest_trade_date": None}
    if dataset.get("status") not in {"success", "rolled_back"}:
        return {
            "ok": False, "reason": "quant_sync_not_successful",
            "expected_latest_trade_date": expected, "sync_status": dataset.get("status"),
        }
    if dataset.get("freshness") != "fresh":
        return {
            "ok": False, "reason": "quant_stock_bars_stale",
            "expected_latest_trade_date": expected,
            "source_market_as_of": dataset.get("max_trade_date"),
            "sync_action": dataset.get("sync_action"),
        }

    coverage: dict[str, object] = {
        "stock_bar_count": None, "universe_count": None, "minimum_count": None,
        "required_etf_dates": {},
    }
    expected_key = _date_key(expected)
    try:
        conn = sqlite3.connect(f"file:{(DATA_ROOT / 'quant.db').resolve()}?mode=ro", uri=True)
        try:
            coverage["stock_bar_count"] = int(conn.execute(
                "SELECT COUNT(DISTINCT ts_code) FROM stock_bars "
                "WHERE date IN (?, ?)",
                (expected_key, f"{expected_key[:4]}-{expected_key[4:6]}-{expected_key[6:]}"),
            ).fetchone()[0])
            snapshot = conn.execute(
                "SELECT MAX(trade_date) FROM stock_universe_history "
                "WHERE REPLACE(trade_date, '-', '')<=?", (expected_key,)
            ).fetchone()[0]
            if snapshot:
                universe_count = int(conn.execute(
                    "SELECT COUNT(DISTINCT ts_code) FROM stock_universe_history "
                    "WHERE REPLACE(trade_date, '-', '')=?", (_date_key(snapshot),)
                ).fetchone()[0])
                coverage["universe_count"] = universe_count
                coverage["minimum_count"] = max(1, math.ceil(universe_count * 0.95))
            required_etfs = ("513100", "510300", "512100", "518880", "511380")
            etf_dates = {
                symbol: conn.execute("SELECT MAX(date) FROM bars WHERE symbol=?", (symbol,)).fetchone()[0]
                for symbol in required_etfs
            }
            coverage["required_etf_dates"] = etf_dates
        finally:
            conn.close()
    except sqlite3.Error:
        # Legacy fixtures predate universe-history tables; their watermark is
        # still sufficient for this compatibility path.
        pass
    if coverage["minimum_count"] is not None and (
        coverage["stock_bar_count"] is None or coverage["stock_bar_count"] < coverage["minimum_count"]
    ):
        return {
            "ok": False, "reason": "quant_stock_bar_coverage_insufficient",
            "expected_latest_trade_date": expected,
            "source_market_as_of": dataset.get("max_trade_date"), "coverage": coverage,
        }
    stale_etfs = sorted(
        symbol for symbol, date in dict(coverage["required_etf_dates"]).items()
        if _date_key(date) < _date_key(expected)
    )
    if stale_etfs:
        return {
            "ok": False, "reason": "quant_etf_bars_stale",
            "expected_latest_trade_date": expected,
            "source_market_as_of": dataset.get("max_trade_date"),
            "stale_etfs": stale_etfs, "coverage": coverage,
        }
    return {
        "ok": True, "expected_latest_trade_date": expected,
        "source_market_as_of": dataset.get("max_trade_date"), "coverage": coverage,
    }


if __name__ == "__main__":
    import sys

    result = sync_legacy_market_data()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    sys.exit(0 if result.get("ok") else 1)
