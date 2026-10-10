"""Read-only quality gates: actual universe membership, held symbols and valid prices."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def check(expected: str, root: Path = ROOT, require_publication: bool = False) -> dict:
    day = expected.replace('-', '')
    issues, warnings, details = [], [], {}
    q = sqlite3.connect(f'file:{(root / "data/quant.db").resolve()}?mode=ro', uri=True)
    v = sqlite3.connect(f'file:{(root / "data/v2_market.db").resolve()}?mode=ro', uri=True)
    try:
        # Pin both read transactions; a rollback publisher cannot commit while
        # these snapshots are read. Compare the receipts before trusting data.
        from qsim.storage.publication import marker, PROTOCOL, ROLLBACK_MODES
        q.execute('BEGIN'); v.execute('BEGIN')
        left, right = marker(q), marker(v)
        if left is not None or right is not None or require_publication:
            durable = all(str(c.execute('PRAGMA journal_mode').fetchone()[0]).lower() in ROLLBACK_MODES for c in (q, v))
            consistent = left is not None and left == right and left[2] == PROTOCOL and left[1] == day and durable
            details['publication_consistent'] = consistent
            if not consistent:
                issues.append('双库发布版本缺失、不一致或未达到预期交易日')
        observed = {r[0] for r in q.execute('SELECT ts_code FROM stock_bars WHERE date=?', (day,))}
        snapshot = q.execute('SELECT MAX(trade_date) FROM stock_universe_history WHERE trade_date<=?', (day,)).fetchone()[0]
        universe = {r[0] for r in q.execute('SELECT ts_code FROM stock_universe_history WHERE trade_date=?', (snapshot,))} if snapshot else set()
        missing = sorted(universe - observed)
        details['universe'] = {'size': len(universe), 'covered': len(universe & observed), 'missing': missing}
        if not universe or len(universe & observed) < math.ceil(len(universe) * .95):
            issues.append('股票池成员覆盖不足 95%（不能用池外股票补足）')
        critical = {r[0] for r in q.execute('SELECT DISTINCT ts_code FROM qsim_positions WHERE shares>0')}
        for row in q.execute("SELECT candidates_json FROM qsim_order_intents WHERE status IN ('pending','frozen')"):
            payload = json.loads(row[0] or '[]')
            for item in payload if isinstance(payload, list) else payload.get('candidates', []):
                critical.add(str(item.get('ts_code') or item.get('code') or '') if isinstance(item, dict) else str(item))
        missing_critical = []
        for code in sorted(critical - {''}):
            table, key = ('stock_bars', 'ts_code') if '.' in code else ('bars', 'symbol')
            if not q.execute(f'SELECT 1 FROM {table} WHERE {key}=? AND date=?', (code, day)).fetchone():
                missing_critical.append(code)
        details['missing_critical'] = missing_critical
        if missing_critical:
            issues.append('持仓或待执行订单缺少当日行情：' + '、'.join(missing_critical[:8]))
        for conn, table, date_col, volume_col in ((q, 'bars', 'date', 'volume'), (q, 'stock_bars', 'date', 'vol'), (v, 'cb_daily_raw', 'trade_date', 'vol')):
            bad = conn.execute(f'''SELECT COUNT(*) FROM {table} WHERE {date_col}=? AND
                (open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL OR
                 open<=0 OR close<=0 OR low<=0 OR high<low OR high+0.00001<MAX(open,close) OR low-0.00001>MIN(open,close) OR {volume_col} IS NULL OR {volume_col}<0 OR ABS(open)>1e15 OR ABS(high)>1e15 OR ABS(low)>1e15 OR ABS(close)>1e15 OR ABS({volume_col})>1e20)''', (day,)).fetchone()[0]
            details[table + '_invalid'] = bad
            if bad:
                issues.append(f'{table} 有 {bad} 条价格或成交量异常')
        factors = {r[0] for r in v.execute('SELECT ts_code FROM adj_factor WHERE trade_date=? AND adj_factor>0', (day,))}
        details['adjustment'] = {'latest': v.execute('SELECT MAX(trade_date) FROM adj_factor').fetchone()[0],
                                 'required': len(observed), 'covered': len(factors & observed)}
        if observed and len(factors & observed) < math.ceil(len(observed) * .95):
            issues.append('当日复权因子覆盖不足 95%')
        if missing:
            warnings.append(f'{len(missing)} 个股票池成员缺行情，可能停牌；持仓缺失仍阻止结算')
        details['convertible'] = {table: v.execute(f'SELECT COUNT(*) FROM {table} WHERE trade_date=?', (day,)).fetchone()[0]
                                  for table in ('cb_daily_raw', 'cb_indicator', 'stk_close_daily')}
        cb = details['convertible']
        if cb['cb_daily_raw'] < 100 or cb['stk_close_daily'] < 100 or cb['cb_indicator'] < math.ceil(cb['cb_daily_raw'] * .9):
            issues.append('可转债行情、正股或指标覆盖不足')
    except (sqlite3.Error, ValueError, TypeError) as exc:
        issues.append('质量检查无法完成：' + str(exc)[:150])
    finally:
        q.close(); v.close()
    return {'ok': not issues, 'session': day, 'issues': issues, 'warnings': warnings, 'details': details}
