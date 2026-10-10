"""Formal daily-delayed paper preparation under the existing mutation lock.

Historical accounts are replayed only in isolated staging. Decisions already
published at close are reused verbatim. New provider inputs cannot rewrite
settled economic history. No broker, intraday quote feed, or second timer.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from qsim.services import distributions
from qsim.services.mutation_lock import serialized_mutation
from qsim.storage.paper import SCHEMA, get_conn, init_schema


def collect_etf_references(conn, symbols, start, end, pro):
    for symbol in sorted(set(symbols)):
        expected = {r[0] for r in conn.execute('SELECT date FROM bars WHERE symbol=? AND date>=? AND date<=?', (symbol,start,end))}
        existing = {r[0] for r in conn.execute('SELECT date FROM qsim_execution_reference WHERE symbol=? AND date>=? AND date<=?', (symbol,start,end))}
        if expected <= existing:
            continue
        code = symbol + ('.SH' if symbol.startswith('5') else '.SZ')
        rows = distributions.paged(pro,'fund_daily',ts_code=code,start_date=start,end_date=end)
        values = []
        for row in rows:
            date = str(row['trade_date'])
            if str(row['ts_code']) != code or date not in expected:
                continue
            observed=conn.execute('SELECT open,close FROM bars WHERE symbol=? AND date=?',(symbol,date)).fetchone()
            if any(abs(float(observed[i])-float(row[field]))>1e-7 for i,field in enumerate(('open','close'))):
                raise RuntimeError('ETF raw execution price differs from stored history; isolated correction required')
            previous = float(row.get('pre_close') or 0)
            if previous <= 0:
                raise RuntimeError('ETF official previous-close reference missing')
            values.append((symbol,date,previous,dt.datetime.now(dt.timezone.utc).isoformat()))
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany('INSERT OR IGNORE INTO qsim_execution_reference VALUES(?,?,?,?)', values)
            actual = {r[0] for r in conn.execute('SELECT date FROM qsim_execution_reference WHERE symbol=? AND date>=? AND date<=?', (symbol,start,end))}
            if not expected <= actual:
                raise RuntimeError('ETF official previous-close window incomplete')
            conn.commit()
        except BaseException:
            conn.rollback(); raise


def _scratch(live):
    stage = sqlite3.connect(':memory:'); stage.row_factory=sqlite3.Row
    stage.executescript(SCHEMA)
    columns=[r[1] for r in live.execute('PRAGMA table_info(qsim_accounts)')]
    rows=live.execute(f"SELECT {','.join(columns)} FROM qsim_accounts").fetchall()
    stage.executemany(f"INSERT INTO qsim_accounts ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", rows)
    stage.commit()
    return stage


@serialized_mutation
def prepare_market(start, end=None, pro=None):
    from qsim.services import paper_daily as paper
    from qsim.services.native_market import client
    end=min(end or dt.date.today().strftime('%Y%m%d'),dt.date.today().strftime('%Y%m%d'))
    as_of=dt.date.today().strftime('%Y%m%d')
    init_schema()
    live=get_conn()
    probe_token=paper._DISTRIBUTION_PROBE.set(True)
    symbol_token=paper._ACTION_SYMBOLS.set(frozenset())
    frozen_token=paper._FROZEN_SIGNALS.set({})
    artifacts_token=paper._ARTIFACT_WRITES.set(False)
    source_token=paper._MARKET_SOURCE.set(live)
    try:
        live.executescript(distributions.SCHEMA)
        pro=pro or client()
        dates=[r[0] for r in live.execute('SELECT DISTINCT date FROM stock_bars WHERE date>=? AND date<=? ORDER BY date',(start,end))]
        distributions.collect_limits(live,dates,pro)
        distributions.collect_limits(live,dates,pro,endpoint="etf_limit")
        etfs={r[0] for r in live.execute('SELECT DISTINCT symbol FROM bars WHERE date>=? AND date<=?',(start,end))}
        collect_etf_references(live,etfs,start,end,pro)
        paper._FROZEN_SIGNALS.set({name:distributions.frozen(live,name) for name in paper._eligible(None)})
        needed=set(etfs)
        for attempt in range(8):
            distributions.collect(live,needed,as_of,pro)
            covered={r[0] for r in live.execute('SELECT symbol FROM qsim_distribution_coverage WHERE as_of=?',(as_of,))}
            paper._ACTION_SYMBOLS.set(frozenset(covered))
            # First pass finds prospective instruments without guessing a zero
            # distribution; later passes use verified receipts and close the
            # selection/cash-action dependency. A finite cap fails closed.
            paper._DISTRIBUTION_PROBE.set(attempt==0)
            stage=_scratch(live)
            try:
                paper._rebuild_paper(stage,start,end,market_conn=live)
                traded={r[0] for r in stage.execute('SELECT DISTINCT symbol FROM qsim_trades')}
                if attempt>0 and traded <= covered:
                    return {'ok':True,'status':'verified_input_closure','as_of':as_of}
                needed |= traded
            except RuntimeError as exc:
                # Only an explicitly identified new instrument restarts input
                # closure. All accounting/provider/history failures propagate.
                if str(exc) != 'new traded instrument lacks a distribution API receipt':
                    raise
                traded={r[0] for r in stage.execute('SELECT DISTINCT symbol FROM qsim_trades')}
                # Result not yet persisted may contain the new instrument;
                # runner reports it via an attached exception payload.
                needed |= set(getattr(exc,'symbols',())) | traded
            finally:
                stage.close()
        raise RuntimeError('distribution/execution input closure not reached')
    finally:
        paper._DISTRIBUTION_PROBE.reset(probe_token)
        paper._ACTION_SYMBOLS.reset(symbol_token)
        paper._FROZEN_SIGNALS.reset(frozen_token)
        paper._ARTIFACT_WRITES.reset(artifacts_token)
        paper._MARKET_SOURCE.reset(source_token)
        live.close()
