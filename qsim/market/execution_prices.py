"""Raw-price admission for share/cash paper ledgers. Signals may use qfq."""
import sqlite3


def require_raw_stock_window(conn, start, end):
    try:
        missing = conn.execute('''SELECT EXISTS(
            SELECT 1 FROM stock_bars s LEFT JOIN stock_execution_bars r
            ON r.ts_code=s.ts_code AND r.date=s.date
            WHERE s.date>=? AND s.date<=? AND
            (r.ts_code IS NULL OR r.open IS NULL OR r.close IS NULL OR r.vol IS NULL OR
             r.open<=0 OR r.close<=0 OR r.vol<0 OR ABS(r.open)>1e15 OR ABS(r.close)>1e15 OR
             r.high<MAX(r.open,r.close) OR r.low>MIN(r.open,r.close)))''',(start,end)).fetchone()[0]
        present = conn.execute('SELECT EXISTS(SELECT 1 FROM stock_execution_bars WHERE date>=? AND date<=?)',(start,end)).fetchone()[0]
    except sqlite3.Error:
        raise RuntimeError('原始成交价格表缺失；禁止用前复权价格计算股数、成交和现金') from None
    if missing or not present:
        raise RuntimeError('原始成交价格窗口不完整；账本保留，禁止回退到前复权成交价')
