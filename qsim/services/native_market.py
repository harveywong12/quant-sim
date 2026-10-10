"""Native daily market collector with rolling corrections and transactional audit."""
from __future__ import annotations

import datetime as dt
import json
import hashlib
import math
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path


from qsim.market import legacy_adapter as market
from qsim.services.mutation_lock import serialized_mutation
from qsim.storage.publication import require_rollback, mark, committed_result

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("qsim.native_market")
AUDIT_SCHEMA = '''CREATE TABLE IF NOT EXISTS qsim_market_revisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, dataset TEXT NOT NULL,
 table_name TEXT NOT NULL, key_json TEXT NOT NULL, old_json TEXT NOT NULL,
 new_json TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_execution_bars (
 ts_code TEXT NOT NULL, date TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
 low REAL NOT NULL, close REAL NOT NULL, vol REAL NOT NULL, amount REAL,
 PRIMARY KEY(ts_code,date)
);
CREATE INDEX IF NOT EXISTS idx_qsim_market_revision_run ON qsim_market_revisions(run_id);
CREATE TABLE IF NOT EXISTS qsim_fundamental_versions (
 ts_code TEXT NOT NULL, end_date TEXT NOT NULL, ann_date TEXT NOT NULL,
 observed_at TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(ts_code,end_date,ann_date,payload_hash)
);
CREATE TABLE IF NOT EXISTS qsim_native_ingest_runs (
 run_id TEXT PRIMARY KEY, session TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL, result_json TEXT
);
'''


def enabled() -> bool:
    value = os.environ.get('QSIM_NATIVE_MARKET')
    if value is not None:
        return value == '1'
    try:
        return json.loads((ROOT / 'config' / 'native-market.json').read_text()).get('enabled') is True
    except (OSError, ValueError):
        return False


def client():
    from qsim.market.secure_tushare import make_client
    path = Path(os.environ.get('QSIM_MARKET_TOKEN_FILE', str(ROOT / '.secrets' / 'tushare-token')))
    try:
        token = path.read_text().strip()
    except OSError:
        token = os.environ.get('TUSHARE_TOKEN', '').strip()
    if not token:
        raise RuntimeError('新系统行情凭证未配置')
    return make_client(token)


def query(pro, endpoint: str, *, max_records: int = 600000, **params) -> list[dict]:
    records, offset = [], 0
    seen_pages = set()
    for _ in range(200):
        for attempt in range(4):
            try:
                frame = pro.query(endpoint, limit=3000, offset=offset, **params)
                if frame is None:
                    raise RuntimeError('empty provider response')
                break
            except Exception:
                if attempt == 3:
                    raise RuntimeError(f'{endpoint} failed after retries') from None
                time.sleep(min(20, 2 ** (attempt + 1)))
        signature = frame.to_json(orient='split', index=False)
        if not frame.empty and signature in seen_pages:
            raise RuntimeError(f'{endpoint} pagination repeated page; no data published')
        seen_pages.add(signature)
        rows = frame.to_dict('records')
        records.extend(rows)
        if len(records) > max_records:
            raise RuntimeError(f'{endpoint} record limit reached; no data published')
        if len(rows) < 3000:
            return records
        offset += len(rows)
    raise RuntimeError(f'{endpoint} pagination limit reached')


def clean(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def upsert(conn, schema: str, table: str, keys: tuple[str, ...], row: dict, run_id: str) -> bool:
    if schema not in ('main', 'v2') or not table.replace('_', '').isalnum():
        raise ValueError('invalid ingestion table')
    columns = list(row)
    values = tuple(clean(row[key]) for key in columns)
    conditions = ' AND '.join(f'{key}=?' for key in keys)
    old = conn.execute(f"SELECT {','.join(columns)} FROM {schema}.{table} WHERE {conditions}", tuple(row[key] for key in keys)).fetchone()
    def same(a, b):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8)
        return a == b
    if old is not None and all(same(a, b) for a, b in zip(old, values)):
        return False
    if old is not None:
        conn.execute('INSERT INTO qsim_market_revisions(run_id,dataset,table_name,key_json,old_json,new_json,observed_at) VALUES(?,?,?,?,?,?,?)',
                     (run_id, 'quant.db' if schema == 'main' else 'v2_market.db', table,
                      json.dumps({key: row[key] for key in keys}, sort_keys=True),
                      json.dumps(dict(zip(columns, old)), sort_keys=True), json.dumps(dict(zip(columns, values)), sort_keys=True),
                      dt.datetime.now(dt.timezone.utc).isoformat()))
    updates = ','.join(f'{key}=excluded.{key}' for key in columns if key not in keys)
    conn.execute(f"INSERT INTO {schema}.{table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}", values)
    return True


def _valid_bar(row):
    values = [clean(row.get(key)) for key in ('open', 'high', 'low', 'close')]
    if any(value is None or not math.isfinite(float(value)) or float(value) <= 0 for value in values):
        raise RuntimeError('供应商返回无效价格')
    op, hi, lo, cl = map(float, values)
    volume = clean(row.get('vol'))
    if volume is None or not math.isfinite(float(volume)) or hi < max(op, cl, lo) - .00001 or lo > min(op, cl) + .00001 or float(volume) < 0:
        raise RuntimeError('供应商返回不一致的 OHLC 或成交量')


def _state(root: Path, payload: dict):
    path = root / 'artifacts' / 'native_market_state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    tmp.replace(path)


def _cache_state(root: Path, payload: dict) -> bool:
    try:
        _state(root, payload)
        return True
    except OSError:
        logger.error('Native state cache unavailable; committed database receipt remains authoritative')
        return False


@serialized_mutation
def run(force: bool = False, root: Path = ROOT, pro=None) -> dict:
    expected = market._expected_latest_trade_date()
    if not expected:
        raise RuntimeError('交易日历不能确认预期交易日')
    day = market._date_key(expected)
    # A file-cache success is not proof that both databases committed. Conversely,
    # a missing cache after a successful commit must not cause a duplicate ingest.
    prior = committed_result(root) or {}
    if not force and prior.get('ok') and prior.get('session') == day:
        from qsim.services.market_quality import check
        quality = check(expected, root, require_publication=True)
        if quality['ok'] and not prior.get('enrichment_failed_codes'):
            return {'ok': True, 'status': 'up_to_date', 'session': day,
                    'run_id': prior['run_id'], 'state_cache_ok': _cache_state(root, prior)}
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run_id = 'native-' + uuid.uuid4().hex
    conn = sqlite3.connect(root / 'data' / 'quant.db', timeout=30)
    try:
        conn.execute('ATTACH DATABASE ? AS v2', (str(root / 'data' / 'v2_market.db'),))
        require_rollback(conn, ('main', 'v2'))
    except BaseException:
        conn.close()
        raise
    conn.executescript(AUDIT_SCHEMA + market.SYNC_SCHEMA)
    for table in ('adj_factor','cb_daily_raw','stk_close_daily','cb_indicator'):
        conn.execute(f'CREATE INDEX IF NOT EXISTS v2.idx_native_{table}_date ON {table}(trade_date,ts_code)')
    conn.commit()
    conn.execute('INSERT INTO qsim_native_ingest_runs VALUES(?,?,?,?,?,?)', (run_id, day, started, None, 'running', None))
    conn.commit()
    published = False
    try:
        pro = pro or client()
        latest = conn.execute('SELECT MAX(date) FROM stock_bars').fetchone()[0]
        factor_latest = conn.execute('SELECT MAX(trade_date) FROM v2.adj_factor').fetchone()[0]
        cb_latest = conn.execute('SELECT MAX(trade_date) FROM v2.cb_indicator').fetchone()[0]
        # Always refresh five sessions, and include the entire missed gap.
        recent = [row[0] for row in conn.execute('SELECT DISTINCT date FROM stock_bars WHERE date<=? ORDER BY date DESC LIMIT 5', (day,))]
        start = min(recent + [market._date_key(value) for value in (latest, factor_latest, cb_latest) if value]) if recent else day
        sessions = sorted({str(row['cal_date']) for row in query(pro, 'trade_cal', exchange='SSE', start_date=start, end_date=day, is_open='1')})
        if not sessions or sessions[-1] != day:
            raise RuntimeError('供应商交易日历未包含预期日期')
        rolling = set(sessions[-5:])
        gap = min(market._date_key(latest), market._date_key(cb_latest))
        market_sessions = [session for session in sessions if session in rolling or session >= gap]
        factor_sessions = [session for session in sessions if session in rolling or session in market_sessions or session >= market._date_key(factor_latest)]
        equity_codes = {row[0] for row in conn.execute('SELECT DISTINCT ts_code FROM stock_bars')}
        stock_codes = set(equity_codes)
        etfs = {row[0] for row in conn.execute('SELECT DISTINCT symbol FROM bars') if len(row[0]) == 6 and row[0][0] in '15'}
        basics = query(pro, 'cb_basic')
        if len(basics) < 100:
            raise RuntimeError('可转债登记数据不完整')
        stock_codes.update(str(row['stk_code']) for row in basics if row.get('stk_code'))
        daily_rows, cb_rows, etf_rows, factors, valuations, weights = [], [], [], [], [], []
        fundamentals = []
        info_rows = query(pro, 'stock_basic', exchange='', list_status='L', fields='ts_code,name,industry,list_date')
        index_codes = {r[0] for r in conn.execute('SELECT DISTINCT index_code FROM stock_universe_history')}
        weight_start = (dt.datetime.strptime(day, '%Y%m%d').date() - dt.timedelta(days=40)).strftime('%Y%m%d')
        for index in sorted(index_codes):
            weights.extend(query(pro, 'index_weight', index_code=index, start_date=weight_start, end_date=day))
        equity_codes.update(str(row['con_code']) for row in weights)
        stock_codes.update(equity_codes)
        current_day = conn.execute('SELECT MAX(trade_date) FROM stock_universe_history WHERE trade_date<=?', (day,)).fetchone()[0]
        universe_codes = {r[0] for r in conn.execute('SELECT ts_code FROM stock_universe_history WHERE trade_date=?', (current_day,))}
        if weights:
            newest = max(str(row['trade_date']) for row in weights)
            universe_codes = {str(row['con_code']) for row in weights if str(row['trade_date']) == newest}
        fundamental_start = (dt.datetime.strptime(day, '%Y%m%d').date() - dt.timedelta(days=370)).strftime('%Y%m%d')
        enrichment_errors = []
        consecutive_failures = 0
        for index, code in enumerate(sorted(universe_codes)):
            try:
                rows = query(pro, 'fina_indicator', ts_code=code, start_date=fundamental_start, end_date=day,
                             fields='ts_code,ann_date,end_date,roe,grossprofit_margin,netprofit_margin,eps,debt_to_assets')
                fundamentals.extend(row for row in rows if row.get('ann_date') and str(row['ann_date']) <= day)
                consecutive_failures = 0
            except RuntimeError:
                enrichment_errors.append(code)
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    enrichment_errors.extend(sorted(universe_codes)[index+1:])
                    break
            # Keep the financial endpoint below its tighter request budget.
            if pro.__class__.__module__.startswith('tushare'):
                time.sleep(.7)
            if (index + 1) % 50 == 0:
                logger.info('financial enrichment %s/%s', index+1, len(universe_codes))
        for session in market_sessions:
            daily_rows.extend(row for row in query(pro, 'daily', trade_date=session) if row.get('ts_code') in stock_codes)
            cb_rows.extend(query(pro, 'cb_daily', trade_date=session))
            valuations.extend(row for row in query(pro, 'daily_basic', trade_date=session) if row.get('ts_code') in stock_codes)
        for symbol in sorted(etfs):
            code = symbol + ('.SH' if symbol[0] == '5' else '.SZ')
            etf_rows.extend({**row, 'symbol': symbol} for row in query(pro, 'fund_daily', ts_code=code, start_date=market_sessions[0], end_date=day))
        for session in factor_sessions:
            factors.extend(row for row in query(pro, 'adj_factor', trade_date=session) if row.get('ts_code') in stock_codes)
        if len({r['ts_code'] for r in daily_rows if str(r['trade_date']) == day}) < 285:
            raise RuntimeError('供应商当日股票覆盖不足')
        if len({r['ts_code'] for r in factors if str(r['trade_date']) == day}) < 285:
            raise RuntimeError('供应商当日复权因子覆盖不足')
        if len(info_rows) < 285:
            raise RuntimeError('股票基础信息不完整')
        for row in daily_rows + etf_rows + [row for row in cb_rows if float(row.get('vol') or 0) > 0]:
            _valid_bar(row)
        for row in factors:
            if not math.isfinite(float(row['adj_factor'])) or float(row['adj_factor']) <= 0:
                raise RuntimeError('供应商返回无效复权因子')
        factor_map = {(row['ts_code'], str(row['trade_date'])): float(row['adj_factor']) for row in factors}
        factor_anchor = {}
        for row in sorted(factors, key=lambda r: str(r['trade_date'])):
            factor_anchor[row['ts_code']] = float(row['adj_factor'])
        # A changed qfq anchor requires verified full raw OHLC and factor
        # history. Never rescale previously rounded adjusted prices, and never
        # publish a rolling splice. All rebuilt rows enter the same transaction.
        rebuilds = {}
        for code, new_anchor in factor_anchor.items():
            if code not in equity_codes:
                continue
            older = conn.execute("SELECT MIN(date) FROM stock_bars WHERE ts_code=? AND date<?",
                                 (code, min(market_sessions))).fetchone()[0]
            if older:
                old_anchor = conn.execute("SELECT adj_factor FROM v2.adj_factor WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
                if old_anchor is None or not math.isclose(float(old_anchor[0]), new_anchor, rel_tol=1e-10):
                    if len(rebuilds) >= 32:
                        raise RuntimeError('前复权基准变化；重建数量超出单次资源上限，禁止滚动拼接')
                    from qsim.market.adjustment_rebuild import full_history
                    rebuilds[code] = full_history(conn, pro, code, day, new_anchor, daily_rows, factors, query, _valid_bar)
        if rebuilds:
            daily_rows = [row for row in daily_rows if row['ts_code'] not in rebuilds]
            factors = [row for row in factors if row['ts_code'] not in rebuilds]
            for raw, full_factors in rebuilds.values():
                daily_rows.extend(raw); factors.extend(full_factors)
            factor_map = {(row['ts_code'], str(row['trade_date'])):float(row['adj_factor']) for row in factors}
        conn.execute('BEGIN IMMEDIATE')
        changed = 0
        for row in info_rows:
            changed += upsert(conn, 'main', 'stock_info', ('ts_code',), {key:row.get(key) for key in ('ts_code','name','industry','list_date')},run_id)
        # Preserve every returned announcement/revision, even if multiple
        # versions share a reporting period. This observed history is not a
        # reconstruction of availability before the first collection.
        for row in sorted(fundamentals, key=lambda r: (str(r['ann_date']), str(r['end_date']))):
            payload = json.dumps({key: clean(value) for key,value in row.items()}, sort_keys=True, allow_nan=False)
            conn.execute('INSERT OR IGNORE INTO qsim_fundamental_versions VALUES(?,?,?,?,?,?)',
                         (row['ts_code'], row['end_date'], row['ann_date'], started,
                          hashlib.sha256(payload.encode()).hexdigest(), payload))
            changed += upsert(conn, 'main', 'stock_fundamental', ('ts_code','end_date'), {key:row.get(key) for key in ('ts_code','ann_date','end_date','roe','grossprofit_margin','netprofit_margin','eps','debt_to_assets')},run_id)

        for row in weights:
            changed += upsert(conn, 'main', 'stock_universe_history', ('index_code', 'trade_date', 'ts_code'),
                              {'index_code': row['index_code'], 'trade_date': str(row['trade_date']), 'ts_code': row['con_code'], 'weight': row.get('weight')}, run_id)
        for row in daily_rows:
            if row['ts_code'] in equity_codes:
                changed += upsert(conn, 'main', 'stock_execution_bars', ('ts_code','date'),
                                  {'ts_code':row['ts_code'],'date':str(row['trade_date']),
                                   **{key:row[key] for key in ('open','high','low','close','vol','amount')}},run_id)
            value = {'ts_code': row['ts_code'], 'date': str(row['trade_date']), **{key: row[key] for key in ('open','high','low','close','vol','amount')}}
            # Match the existing Tushare pro_bar(qfq) contract exactly: two-decimal
            # OHLC and VWAP scaled by adjusted/raw close. v2 retains raw prices.
            if row['ts_code'] in equity_codes:
                factor = factor_map.get((row['ts_code'], str(row['trade_date'])))
                anchor = factor_anchor.get(row['ts_code'])
                if factor is None or not anchor:
                    raise RuntimeError('股票行情缺少配套复权因子，全部修订已回滚')
                for key in ('open', 'high', 'low', 'close'):
                    value[key] = float('%.2f' % (float(row[key]) * factor / anchor))
            value['vwap'] = (float(row['amount']) * 10 / float(row['vol']) * float(value['close']) / float(row['close'])
                             if float(row.get('vol') or 0) > 0 and float(row.get('close') or 0) > 0 else None)
            # Keep the original equity universe; bond underlyings belong in v2 only.
            if row['ts_code'] in equity_codes:
                changed += upsert(conn, 'main', 'stock_bars', ('ts_code','date'), value, run_id)
            changed += upsert(conn, 'v2', 'stk_close_daily', ('ts_code','trade_date'), {'ts_code':row['ts_code'],'trade_date':str(row['trade_date']),'close':row['close']},run_id)
        for row in valuations:
            changed += upsert(conn, 'main', 'stock_valuation', ('ts_code','date'),
                              {'ts_code':row['ts_code'],'date':str(row['trade_date']), **{key:row.get(key) for key in ('pe','pe_ttm','pb','dv_ratio','total_mv','circ_mv','turnover_rate')}},run_id)
        for row in etf_rows:
            changed += upsert(conn,'main','bars',('symbol','date'), {'symbol':row['symbol'],'date':str(row['trade_date']),**{key:row[key] for key in ('open','high','low','close')},'volume':row['vol']},run_id)
        for row in basics:
            changed += upsert(conn,'v2','cb_basic_snapshot',('ts_code',), {'ts_code':row['ts_code'], **{key:clean(row.get(key)) for key in ('stk_code','bond_short_name','list_date','delist_date','maturity_date','conv_price','remain_size','issue_size')}},run_id)
        for row in cb_rows:
            if float(row.get('vol') or 0) <= 0:
                continue
            changed += upsert(conn,'v2','cb_daily_raw',('ts_code','trade_date'), {'ts_code':row['ts_code'],'trade_date':str(row['trade_date']),**{key:row[key] for key in ('open','high','low','close','vol','amount')}},run_id)
        for row in factors:
            changed += upsert(conn,'v2','adj_factor',('ts_code','trade_date'), {key:row[key] for key in ('ts_code','trade_date','adj_factor')},run_id)
        marks = ','.join('?' for _ in market_sessions)
        indicators = conn.execute(f'''SELECT d.ts_code,d.trade_date,d.close,s.close,
            ROUND(100.0/b.conv_price*s.close,4),ROUND((d.close/(100.0/b.conv_price*s.close)-1)*100,4),
            ROUND(d.close+(d.close/(100.0/b.conv_price*s.close)-1)*100,4)
            FROM v2.cb_daily_raw d JOIN v2.cb_basic_snapshot b USING(ts_code)
            JOIN v2.stk_close_daily s ON s.ts_code=b.stk_code AND s.trade_date=d.trade_date
            WHERE d.trade_date IN ({marks}) AND b.conv_price>0 AND s.close>0 AND d.close>0''', market_sessions).fetchall()
        for row in indicators:
            changed += upsert(conn,'v2','cb_indicator',('ts_code','trade_date'),dict(zip(('ts_code','trade_date','close','stk_close','conv_value','premium_rate','dual_low'),row)),run_id)
        # Validate before publishing any price or audit changes.
        universe_day = conn.execute('SELECT MAX(trade_date) FROM stock_universe_history WHERE trade_date<=?',(day,)).fetchone()[0]
        universe = {r[0] for r in conn.execute('SELECT ts_code FROM stock_universe_history WHERE trade_date=?',(universe_day,))}
        observed = {r[0] for r in conn.execute('SELECT ts_code FROM stock_bars WHERE date=?',(day,))}
        factored = {r[0] for r in conn.execute('SELECT ts_code FROM v2.adj_factor WHERE trade_date=?',(day,))}
        cb_count = conn.execute('SELECT COUNT(*) FROM v2.cb_daily_raw WHERE trade_date=?',(day,)).fetchone()[0]
        indicator_count = conn.execute('SELECT COUNT(*) FROM v2.cb_indicator WHERE trade_date=?',(day,)).fetchone()[0]
        if not universe or len(universe & observed)<math.ceil(len(universe)*.95) or len(factored & observed)<math.ceil(len(observed)*.95) or cb_count<100 or indicator_count<math.ceil(cb_count*.9):
            raise RuntimeError('入库覆盖检查失败，全部行情修订已回滚')
        for symbol in etfs:
            if not conn.execute('SELECT 1 FROM bars WHERE symbol=? AND date=?',(symbol,day)).fetchone():
                raise RuntimeError('ETF 当日行情缺失，全部行情修订已回滚')
        finished = dt.datetime.now(dt.timezone.utc).isoformat()
        revisions = conn.execute('SELECT COUNT(*) FROM qsim_market_revisions WHERE run_id=?',(run_id,)).fetchone()[0]
        result = {'ok':True,'status':'updated','run_id':run_id,'session':day,'changed_rows':changed,'revisions':revisions,'fundamental_rows':len(fundamentals),'stock_info_rows':len(info_rows),'enrichment_failed_codes':enrichment_errors,'full_adjustment_rebuild_count':len(rebuilds),
                  'stock_price_basis':'tushare_qfq','execution_price_basis':'tushare_raw','adjustment_as_of':day,'market_sessions':len(market_sessions),'factor_sessions':len(factor_sessions),'finished_at':finished}
        for spec in market.DATASETS:
            schema = 'main' if spec.name=='quant.db' else 'v2'
            watermarks = market._table_watermarks(conn,list(spec.allowed_tables),spec.watermark_columns,schema=schema)
            conn.execute('INSERT INTO qsim_data_sync_runs(run_id,dataset,status,started_at,finished_at,source_table_watermarks,sync_action) VALUES(?,?,?,?,?,?,?)',
                         (run_id,spec.name,'success',started,finished,json.dumps(watermarks,sort_keys=True),'native_upsert'))
        conn.execute('UPDATE qsim_native_ingest_runs SET status=?,finished_at=?,result_json=? WHERE run_id=?',('success',finished,json.dumps(result),run_id))
        mark(conn, run_id, day)
        conn.commit()
        published = True
        result['state_cache_ok'] = _cache_state(root, result)
        return result
    except BaseException as exc:
        if published:
            # A post-commit interruption cannot relabel a committed run as failed.
            raise
        conn.rollback()
        error = {'ok':False,'run_id':run_id,'session':day,'error':str(exc)[:400]}
        conn.execute('UPDATE qsim_native_ingest_runs SET status=?,finished_at=?,result_json=? WHERE run_id=?',('failed',dt.datetime.now(dt.timezone.utc).isoformat(),json.dumps(error),run_id))
        conn.commit()
        _cache_state(root,error)
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument('--force',action='store_true')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run(force=args.force),ensure_ascii=False))
