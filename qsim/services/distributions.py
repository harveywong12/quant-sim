"""Versioned provider receipts and cash-action admission, private to the NAS.

This feed is invoked from the existing reconcile/settlement job. It creates no
scheduler. Coverage is a successful API receipt, never inferred from an empty
local table. Conflicting implemented event versions fail closed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, replace

from qsim.engine.cash_distributions import CashDistribution, money, valid_date

SCHEMA = '''
CREATE TABLE IF NOT EXISTS qsim_distribution_observations (
 symbol TEXT NOT NULL, source TEXT NOT NULL, payload_hash TEXT NOT NULL,
 observed_at TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(symbol,source,payload_hash)
);
CREATE TABLE IF NOT EXISTS qsim_distribution_receipts (
 symbol TEXT NOT NULL,as_of TEXT NOT NULL,receipt_hash TEXT NOT NULL,observed_at TEXT NOT NULL,
 PRIMARY KEY(symbol,as_of,receipt_hash,observed_at)
);
CREATE TABLE IF NOT EXISTS qsim_distribution_coverage (
 symbol TEXT NOT NULL, as_of TEXT NOT NULL, receipt_hash TEXT NOT NULL,
 observed_at TEXT NOT NULL, PRIMARY KEY(symbol,as_of)
);
CREATE TABLE IF NOT EXISTS qsim_execution_limits (
 symbol TEXT NOT NULL,date TEXT NOT NULL,lower REAL NOT NULL,upper REAL NOT NULL,
 observed_at TEXT NOT NULL,PRIMARY KEY(symbol,date)
);
CREATE TABLE IF NOT EXISTS qsim_limit_coverage (
 date TEXT PRIMARY KEY, receipt_hash TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qsim_etf_limit_coverage (
 date TEXT PRIMARY KEY, receipt_hash TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qsim_execution_reference (
 symbol TEXT NOT NULL,date TEXT NOT NULL,pre_close REAL NOT NULL,
 observed_at TEXT NOT NULL,PRIMARY KEY(symbol,date)
);
'''
LEDGER_SCHEMA = '''
CREATE TABLE IF NOT EXISTS qsim_distribution_events (
 strategy TEXT NOT NULL,event_key TEXT NOT NULL,date TEXT NOT NULL,
 action_key TEXT NOT NULL,symbol TEXT NOT NULL,kind TEXT NOT NULL,
 cash_delta TEXT NOT NULL,PRIMARY KEY(strategy,event_key)
);
CREATE TABLE IF NOT EXISTS qsim_distribution_nav (
 strategy TEXT NOT NULL,date TEXT NOT NULL,cash_receivable REAL NOT NULL,
 dividend_tax_liability REAL NOT NULL,PRIMARY KEY(strategy,date)
);
CREATE TABLE IF NOT EXISTS qsim_distribution_checkpoints (
 strategy TEXT PRIMARY KEY,date TEXT NOT NULL,state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qsim_frozen_daily_signals (
 strategy TEXT NOT NULL,date TEXT NOT NULL,payload_json TEXT NOT NULL,
 payload_hash TEXT NOT NULL,PRIMARY KEY(strategy,date)
);
'''


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def paged(pro, endpoint, **params):
    from qsim.services.native_market import clean
    records, offset, seen = [], 0, set()
    # dividend endpoint caps pages at 2000; requesting 1000 avoids premature
    # truncation caused by the general collector's 3000-row assumption.
    for _ in range(200):
        for attempt in range(4):
            try:
                frame = pro.query(endpoint, limit=1000, offset=offset, **params)
                if frame is None:
                    raise RuntimeError('provider response missing')
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(min(2 ** attempt, 8))
        rows = [{k: clean(v) for k, v in r.items()} for r in frame.to_dict('records')]
        fingerprint = digest(rows)
        if rows and fingerprint in seen:
            raise RuntimeError('provider repeated a distribution/limit page')
        seen.add(fingerprint)
        records.extend(rows)
        if len(rows) < 1000:
            return records
        offset += len(rows)
        time.sleep(.20)
    raise RuntimeError('distribution/limit pagination exceeded safety cap')


def collect(conn, symbols, as_of, pro=None):
    from qsim.services.native_market import client
    valid_date(as_of)
    conn.executescript(SCHEMA)
    pro = pro or client()
    pending=[]
    for symbol in sorted(set(symbols)):
        prior=conn.execute('SELECT observed_at FROM qsim_distribution_coverage WHERE symbol=? AND as_of=?',(symbol,as_of)).fetchone()
        if prior:
            fresh=True
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='stock_execution_bars'").fetchone():
                last=conn.execute('SELECT MAX(date) FROM stock_execution_bars').fetchone()[0]
                if last == as_of:
                    observed=dt.datetime.fromisoformat(prior[0]).astimezone(dt.timezone(dt.timedelta(hours=8)))
                    fresh=observed.strftime('%Y%m%d')==as_of and observed.hour>=15
            if fresh:
                continue
        source = 'dividend' if '.' in symbol else 'fund_div'
        if source == 'dividend':
            rows = paged(pro, source, ts_code=symbol)
        else:
            rows = []
            for code in (symbol + ('.SH' if symbol.startswith('5') else '.SZ'), symbol + '.OF'):
                rows.extend(paged(pro, source, ts_code=code))
        observed = dt.datetime.now(dt.timezone.utc).isoformat()
        for row in rows:
            if str(row.get('ts_code', '')).split('.')[0] != symbol.split('.')[0]:
                raise RuntimeError('provider distribution identity mismatch')
        pending.append((symbol,source,rows,observed))
        time.sleep(.20)

    if pending:
        try:
            conn.execute('BEGIN IMMEDIATE')
            for symbol,source,rows,observed in pending:
                conn.executemany('INSERT OR IGNORE INTO qsim_distribution_observations VALUES(?,?,?,?,?)',
                    [(symbol,source,digest(row),observed,encoded(row)) for row in rows])
                conn.execute('INSERT INTO qsim_distribution_receipts VALUES(?,?,?,?)',(symbol,as_of,digest(rows),observed))
                conn.execute('INSERT OR REPLACE INTO qsim_distribution_coverage VALUES(?,?,?,?)',
                             (symbol,as_of,digest(rows),observed))
            conn.commit()
        except BaseException:
            conn.rollback(); raise


def load(conn, symbols, start, end, as_of):
    actions = {}
    for symbol in sorted(set(symbols)):
        if not conn.execute('SELECT 1 FROM qsim_distribution_coverage WHERE symbol=? AND as_of=?', (symbol, as_of)).fetchone():
            raise RuntimeError('cash-distribution API coverage missing')
        for row in conn.execute('SELECT source,payload_json FROM qsim_distribution_observations WHERE symbol=?', (symbol,)):
            source, payload = row[0], json.loads(row[1])
            if payload.get('ann_date') and str(payload['ann_date']) > as_of:
                continue
            if source == 'dividend' and payload.get('div_proc') != '实施':
                continue
            record = str(payload.get('record_date') or '')
            ex = str(payload.get('ex_date') or '')
            if record and (record < start or record > end):
                continue
            if not record and not (start <= ex <= end):
                continue
            gross = payload.get('cash_div_tax') if source == 'dividend' else payload.get('div_cash')
            ratio = payload.get('stk_div') or 0
            if source == 'dividend':
                ratio = max(money(ratio), money(payload.get('stk_bo_rate') or 0) + money(payload.get('stk_co_rate') or 0))
            if gross is None:
                raise RuntimeError('gross distribution tax basis unavailable')
            if money(gross) == 0 and money(ratio) == 0:
                continue
            key = f'{symbol}:{record}:{ex}'
            action = CashDistribution(key, symbol, 'a_share' if source == 'dividend' else 'mainland_etf',
                                      record, ex, str(payload.get('pay_date') or ''), str(money(gross).normalize()), str(money(ratio).normalize()))
            if not record or not ex:
                raise RuntimeError('cash-distribution entitlement dates missing')
            valid_date(record); valid_date(ex)
            # A revision is retained as an observation, never silently summed
            # into a second distribution or substituted into settled history.
            if key in actions and actions[key] != action:
                raise RuntimeError('conflicting implemented cash-distribution versions')
            actions[key] = action
    return list(actions.values())


def collect_limits(conn, dates, pro=None, endpoint="stk_limit"):
    if endpoint not in ("stk_limit", "etf_limit"):
        raise ValueError("unsupported limit source")
    coverage = "qsim_limit_coverage" if endpoint == "stk_limit" else "qsim_etf_limit_coverage"
    from qsim.services.native_market import client
    conn.executescript(SCHEMA)
    pro = pro or client()
    pending=[]
    for date in dates:
        if conn.execute(f'SELECT 1 FROM {coverage} WHERE date=?', (date,)).fetchone():
            continue
        rows = paged(pro, endpoint, trade_date=date)
        if not rows:
            raise RuntimeError('official daily stock limits unavailable')
        observed = dt.datetime.now(dt.timezone.utc).isoformat()
        values=[]
        for row in rows:
            if str(row.get('trade_date')) != date:
                raise RuntimeError('daily price-limit date mismatch')
            lower, upper = row.get('down_limit'), row.get('up_limit')
            if lower is None or upper is None:
                continue
            symbol=row['ts_code'] if endpoint=='stk_limit' else row['ts_code'].split('.')[0]
            values.append((symbol,date,float(lower),float(upper),observed))
        pending.append((date,values,digest(rows),observed))
        time.sleep(.20)
    if pending:
        try:
            conn.execute('BEGIN IMMEDIATE')
            for date,values,receipt,observed in pending:
                conn.executemany('INSERT INTO qsim_execution_limits VALUES(?,?,?,?,?)',values)
                conn.execute(f'INSERT INTO {coverage} VALUES(?,?,?)',(date,receipt,observed))
            conn.commit()
        except BaseException:
            conn.rollback(); raise


def stock_cost(conn, cost, start, end):
    limits = {(r[1],r[0]): (r[2],r[3]) for r in conn.execute(
        'SELECT symbol,date,lower,upper FROM qsim_execution_limits WHERE date>=? AND date<=?', (start, end))}
    return replace(cost, daily_limits=limits, require_daily_limits=True)


def frozen(conn, name):
    return {row[0]: json.loads(row[1]) for row in conn.execute(
        'SELECT date,payload_json FROM qsim_frozen_daily_signals WHERE strategy=?', (name,))}


def persist(conn, name, result):
    for row in result['distribution_events']:
        conn.execute('INSERT INTO qsim_distribution_events VALUES(?,?,?,?,?,?,?)',
                     (name, row['event_key'], row['date'], row['action_key'], row['symbol'], row['kind'], row['cash_delta']))
    for row in result['distribution_nav']:
        conn.execute('INSERT INTO qsim_distribution_nav VALUES(?,?,?,?)',
                     (name,row['date'],row['cash_receivable'],row['dividend_tax_liability']))
    state = result['distribution_state']
    conn.execute('INSERT OR REPLACE INTO qsim_distribution_checkpoints VALUES(?,?,?)',
                 (name,state['last_date'],encoded(state)))
    for row in result['decisions']:
        payload = encoded(row)
        conn.execute('INSERT INTO qsim_frozen_daily_signals VALUES(?,?,?,?)', (name,row['date'],payload,digest(row)))
