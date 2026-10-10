"""Durable ATTACH publication policy; existing WAL databases are never converted.

The two on-disk rollback databases share one SQLite transaction and version
marker. Filesystem state JSON is only a cache of the committed database record.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

PROTOCOL = 'qsim-rollback-v1'
ROLLBACK_MODES = {'delete', 'truncate', 'persist'}


def require_rollback(conn: sqlite3.Connection, schemas=('main',)) -> None:
    if conn.in_transaction:
        raise RuntimeError('storage policy must be checked before starting a transaction')
    files = {row[1]: row[2] for row in conn.execute('PRAGMA database_list')}
    for schema in schemas:
        if schema not in ('main', 'v2') or not files.get(schema):
            raise RuntimeError('publication requires named on-disk databases')
        mode = str(conn.execute(f'PRAGMA {schema}.journal_mode').fetchone()[0]).lower()
        if mode not in ROLLBACK_MODES:
            raise RuntimeError(f'{schema} journal mode {mode.upper()} cannot publish a durable pair; '
                               'use an authorized isolated rollback copy, never convert an active production database')
        conn.execute(f'PRAGMA {schema}.synchronous=EXTRA')
        if conn.execute(f'PRAGMA {schema}.synchronous').fetchone()[0] != 3:
            raise RuntimeError('durable synchronous policy was not applied')


def marker(conn: sqlite3.Connection, schema='main') -> tuple | None:
    if schema not in ('main', 'v2'):
        raise ValueError('invalid publication schema')
    exists = conn.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name='qsim_market_publication'").fetchone()
    if not exists:
        return None
    row = conn.execute(f'SELECT run_id,session,protocol FROM {schema}.qsim_market_publication WHERE singleton=1').fetchone()
    return tuple(row) if row is not None else None


def mark(conn: sqlite3.Connection, run_id: str, session: str) -> None:
    if not conn.in_transaction or not run_id or not session:
        raise RuntimeError('publication marker belongs inside the data transaction')
    for schema in ('main', 'v2'):
        mode = str(conn.execute(f'PRAGMA {schema}.journal_mode').fetchone()[0]).lower()
        sync = conn.execute(f'PRAGMA {schema}.synchronous').fetchone()[0]
        if mode not in ROLLBACK_MODES or sync != 3:
            raise RuntimeError('durable pair publication policy changed')
        conn.execute(f'''CREATE TABLE IF NOT EXISTS {schema}.qsim_market_publication (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            run_id TEXT NOT NULL, session TEXT NOT NULL, protocol TEXT NOT NULL
        )''')
        conn.execute(f'''INSERT INTO {schema}.qsim_market_publication VALUES(1,?,?,?)
            ON CONFLICT(singleton) DO UPDATE SET run_id=excluded.run_id,
            session=excluded.session,protocol=excluded.protocol''', (run_id, session, PROTOCOL))


def committed_result(root: Path) -> dict | None:
    """Read both markers and their success receipt in one attached read transaction."""
    quant, v2 = root/'data/quant.db', root/'data/v2_market.db'
    if not quant.is_file() or not v2.is_file():
        return None
    conn = sqlite3.connect(quant.resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
    try:
        conn.execute('ATTACH DATABASE ? AS v2', (v2.resolve().as_uri()+'?mode=ro',))
        conn.execute('BEGIN')
        main, other = marker(conn), marker(conn, 'v2')
        if main is None or main != other or main[2] != PROTOCOL:
            return None
        row = conn.execute('SELECT status,result_json FROM qsim_native_ingest_runs WHERE run_id=?', (main[0],)).fetchone()
        if row is None or row[0] != 'success':
            return None
        result = json.loads(row[1])
        if result.get('ok') is not True or result.get('run_id') != main[0] or result.get('session') != main[1]:
            return None
        return result
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        return None
    finally:
        conn.close()
