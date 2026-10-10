"""Append-only publication and immutable provenance for official paper ledgers."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sqlite3
import uuid
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS qsim_settlement_runs (
 run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL,
 metadata_json TEXT NOT NULL, appended_snapshots INTEGER NOT NULL,
 appended_trades INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS qsim_trade_provenance (trade_id INTEGER PRIMARY KEY REFERENCES qsim_trades(id), run_id TEXT NOT NULL);
'''


def provenance(root: Path, live: sqlite3.Connection) -> dict:
    files = {}
    for base in (root / 'qsim', root / 'config', root / 'data' / 'strategies'):
        for path in sorted(base.rglob('*')):
            if path.is_file() and path.suffix in ('.py', '.json', '.yaml', '.yml') and '__pycache__' not in path.parts:
                files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    inputs = {}
    for table in ('bars', 'stock_bars', 'stock_valuation', 'stock_universe_history'):
        column = 'trade_date' if table == 'stock_universe_history' else 'date'
        try:
            inputs[table] = dict(zip(('rows', 'latest'), live.execute(f'SELECT COUNT(*), MAX({column}) FROM {table}').fetchone()))
        except sqlite3.Error:
            pass
    try:
        inputs['producer_runs'] = [tuple(row) for row in live.execute(
            'SELECT dataset, MAX(id) FROM qsim_data_sync_runs GROUP BY dataset')]
        inputs['last_revision'] = live.execute('SELECT MAX(id) FROM qsim_market_revisions').fetchone()[0]
    except sqlite3.Error:
        pass
    database_files = {}
    for name in ('quant.db', 'quant.db-wal', 'v2_market.db', 'v2_market.db-wal'):
        path = root / 'data' / name
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(block)
            database_files[name] = digest.hexdigest()
    return {'database_files': database_files, 'source_files': files, 'code_config_hash': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
            'market_inputs': inputs}


def _equal(a, b) -> bool:
    if isinstance(a, (float, int)) and isinstance(b, (float, int)):
        return math.isfinite(a) and math.isfinite(b) and abs(a - b) <= 1e-7
    return a == b


def historical_differences(live: sqlite3.Connection, stage: sqlite3.Connection, names: list[str]) -> list[dict]:
    differences = []
    for name in names:
        cutoff = live.execute('SELECT MAX(date) FROM qsim_snapshots WHERE strategy=?', (name,)).fetchone()[0]
        if not cutoff:
            continue
        for table, columns in (
            ('qsim_snapshots', 'date,total_value,cash,position_value'),
            ('qsim_trades', 'date,symbol,side,price,shares,amount,fee'),
            ('qsim_distribution_events','date,event_key,action_key,symbol,kind,cash_delta'),
            ('qsim_distribution_nav','date,cash_receivable,dividend_tax_liability'),
            ('qsim_frozen_daily_signals','date,payload_json,payload_hash'),
        ):
            if not all(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() for conn in (live,stage)):
                if table.startswith(('qsim_distribution_', 'qsim_frozen_')):
                    continue
            query = f'SELECT {columns} FROM {table} WHERE strategy=? AND date<=? ORDER BY {columns}'
            before = [tuple(row) for row in live.execute(query, (name, cutoff))]
            after = [tuple(row) for row in stage.execute(query, (name, cutoff))]
            for index in range(max(len(before), len(after))):
                a = before[index] if index < len(before) else None
                b = after[index] if index < len(after) else None
                if a is None or b is None or not all(_equal(x, y) for x, y in zip(a, b)):
                    differences.append({'strategy': name, 'table': table, 'cutoff': cutoff, 'before': a, 'replay': b})
                    if len(differences) >= 20:
                        return differences
    return differences


def promote(live: sqlite3.Connection, stage: sqlite3.Connection, names: list[str], metadata: dict,
            copy_rows) -> dict:
    differences = historical_differences(live, stage, names)
    if differences:
        error = RuntimeError('已结算历史与当前回放不同；账本保留，请查看独立回放差异')
        error.differences = differences
        raise error
    # A live open-order executor may already have recorded today's fills.
    # Match them against replay economics and remove only the matched stage rows.
    for name in names:
        cutoff = live.execute('SELECT MAX(date) FROM qsim_snapshots WHERE strategy=?', (name,)).fetchone()[0] or ''
        fields = 'date,symbol,side,price,shares,amount,fee'
        staged_fills = [(row[0], tuple(row[1:])) for row in stage.execute(f'SELECT id,{fields} FROM qsim_trades WHERE strategy=? AND date>? ORDER BY id', (name, cutoff))]
        for recorded in live.execute(f'SELECT {fields} FROM qsim_trades WHERE strategy=? AND date>? ORDER BY id', (name, cutoff)):
            matched = next((i for i, (_, values) in enumerate(staged_fills) if all(_equal(a,b) for a,b in zip(recorded, values))), None)
            if matched is None:
                error = RuntimeError('已执行开盘成交与回放不同；账本保留，请核对冻结订单')
                error.differences = [{'strategy':name,'table':'qsim_trades','before':tuple(recorded),'replay':None}]
                raise error
            row_id, _ = staged_fills.pop(matched)
            stage.execute('DELETE FROM qsim_trades WHERE id=?', (row_id,))
    counts = {'snapshots': 0, 'trades': 0}
    run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:10]
    prior_trade_id = live.execute('SELECT COALESCE(MAX(id),0) FROM qsim_trades').fetchone()[0]
    live.execute('BEGIN IMMEDIATE')
    try:
        for name in names:
            cutoff = live.execute('SELECT MAX(date) FROM qsim_snapshots WHERE strategy=?', (name,)).fetchone()[0] or ''
            pending = stage.execute('SELECT COUNT(*) FROM qsim_snapshots WHERE strategy=? AND date>?', (name, cutoff)).fetchone()[0]
            if not pending:
                continue
            for table, key in (('qsim_snapshots', 'snapshots'), ('qsim_trades', 'trades')):
                count = stage.execute(f'SELECT COUNT(*) FROM {table} WHERE strategy=? AND date>?', (name, cutoff)).fetchone()[0]
                copy_rows(stage, live, table, 'strategy=? AND date>?', [name, cutoff])
                counts[key] += count
            for table in ('qsim_distribution_events','qsim_distribution_nav','qsim_frozen_daily_signals'):
                if not stage.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
                    continue
                copy_rows(stage, live, table, 'strategy=? AND date>?', [name, cutoff])
            checkpoint = None
            if stage.execute("SELECT 1 FROM sqlite_master WHERE name='qsim_distribution_checkpoints'").fetchone():
                checkpoint = stage.execute('SELECT strategy,date,state_json FROM qsim_distribution_checkpoints WHERE strategy=?',(name,)).fetchone()
            if checkpoint:
                live.execute('INSERT OR REPLACE INTO qsim_distribution_checkpoints VALUES(?,?,?)',checkpoint)
            columns = [row[1] for row in stage.execute('PRAGMA table_info(qsim_accounts)')]
            account = stage.execute(f"SELECT {','.join(columns)} FROM qsim_accounts WHERE strategy=?", (name,)).fetchone()
            updates = ','.join(f'{col}=excluded.{col}' for col in columns if col != 'id')
            live.execute(f"INSERT INTO qsim_accounts ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(id) DO UPDATE SET {updates}", account)
            live.execute('DELETE FROM qsim_positions WHERE account_id=?', (account['id'],))
            copy_rows(stage, live, 'qsim_positions', 'account_id=?', [account['id']])
        live.execute('INSERT INTO qsim_settlement_runs VALUES(?,?,?,?,?,?)',
                     (run_id, dt.datetime.now(dt.timezone.utc).isoformat(), 'append_only', json.dumps(metadata, ensure_ascii=False, sort_keys=True), counts['snapshots'], counts['trades']))
        live.execute('INSERT INTO qsim_trade_provenance SELECT id, ? FROM qsim_trades WHERE id>?', (run_id, prior_trade_id))
        live.commit()
    except BaseException:
        live.rollback()
        raise
    return {'run_id': run_id, 'appended': counts}
