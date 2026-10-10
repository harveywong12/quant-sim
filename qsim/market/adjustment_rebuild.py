"""Verify full provider history before publishing a changed qfq anchor."""
import datetime as dt
import math


def full_history(conn, pro, symbol, day, new_anchor, rolling_rows, rolling_factors, query, valid_bar):
    prefix = '前复权基准变化；完整历史重建验收失败，禁止滚动拼接：'
    stored = {str(row[0]) for row in conn.execute('SELECT date FROM stock_bars WHERE ts_code=?', (symbol,))}
    if not stored:
        raise RuntimeError(prefix+'existing history missing')
    first = min(stored)
    raw = query(pro, 'daily', ts_code=symbol, start_date=first, end_date=day, max_records=20000)
    factors = query(pro, 'adj_factor', ts_code=symbol, start_date=first, end_date=day, max_records=20000)

    def keyed(rows):
        result = {}
        for row in rows:
            date = str(row.get('trade_date') or '')
            try:
                dt.datetime.strptime(date, '%Y%m%d')
            except ValueError:
                raise RuntimeError(prefix+'invalid date') from None
            if row.get('ts_code') != symbol or not first <= date <= day or date in result:
                raise RuntimeError(prefix+'wrong symbol, duplicate or out-of-range date')
            result[date] = row
        return result

    raw_by_date, factors_by_date = keyed(raw), keyed(factors)
    needed = stored | {str(row['trade_date']) for row in rolling_rows if row['ts_code']==symbol}
    if not needed <= raw_by_date.keys() or not raw_by_date.keys() <= factors_by_date.keys():
        raise RuntimeError(prefix+'missing raw prices or factors for stored dates')
    for row in raw:
        valid_bar(row)
    for row in factors:
        factor = float(row['adj_factor'])
        if not math.isfinite(factor) or factor <= 0:
            raise RuntimeError(prefix+'invalid factor')
    if not factors or not math.isclose(float(factors_by_date[max(factors_by_date)]['adj_factor']), new_anchor, rel_tol=1e-10):
        raise RuntimeError(prefix+'provider anchor changed during verification')
    # The two provider requests must agree with this run's rolling snapshot.
    for row in rolling_rows:
        if row['ts_code']==symbol:
            other=raw_by_date[str(row['trade_date'])]
            if any(not math.isclose(float(row[k]),float(other[k]),rel_tol=1e-10,abs_tol=1e-8) for k in ('open','high','low','close','vol','amount')):
                raise RuntimeError(prefix+'provider raw history changed during verification')
    for row in rolling_factors:
        if row['ts_code']==symbol:
            other=factors_by_date.get(str(row['trade_date']))
            if not other or not math.isclose(float(row['adj_factor']),float(other['adj_factor']),rel_tol=1e-10):
                raise RuntimeError(prefix+'provider factor history changed during verification')
    return raw, factors
