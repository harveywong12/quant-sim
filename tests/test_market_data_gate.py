from __future__ import annotations

from qsim.market import legacy_adapter as adapter


def test_gate_counts_both_date_formats_without_double_counting(tmp_path, monkeypatch):
    import sqlite3
    with sqlite3.connect(tmp_path / 'quant.db') as conn:
        conn.execute('CREATE TABLE stock_bars(ts_code TEXT,date TEXT)')
        conn.execute('CREATE INDEX stock_date ON stock_bars(date)')
        conn.executemany('INSERT INTO stock_bars VALUES(?,?)',
            [('A','20260930'),('A','2026-09-30'),('B','2026-09-30'),('C','20260929')])
        conn.execute('CREATE TABLE stock_universe_history(ts_code TEXT,trade_date TEXT)')
        conn.executemany('INSERT INTO stock_universe_history VALUES(?,?)',[('A','20260930'),('B','20260930')])
        conn.execute('CREATE TABLE bars(symbol TEXT,date TEXT)')
        conn.executemany('INSERT INTO bars VALUES(?,?)',
            [(s,'20260930') for s in ('513100','510300','512100','518880','511380')])
    monkeypatch.setattr(adapter,'DATA_ROOT',tmp_path)
    monkeypatch.setattr(adapter,'latest_sync_status',lambda:{
        'expected_latest_trade_date':'2026-09-30',
        'datasets':{'quant.db':{'status':'success','freshness':'fresh','max_trade_date':'20260930'}}})
    result=adapter.market_data_gate_status()
    assert result['ok']
    assert result['coverage']['stock_bar_count']==2


def test_market_data_gate_rejects_stale_required_etf(monkeypatch) -> None:
    monkeypatch.setattr(adapter, "latest_sync_status", lambda: {
        "expected_latest_trade_date": "2026-09-01",
        "datasets": {"quant.db": {
            "status": "success", "freshness": "fresh", "max_trade_date": "2026-09-01",
        }},
    })

    class Conn:
        def execute(self, sql, params=()):
            if "COUNT(DISTINCT ts_code)" in sql:
                return type("Row", (), {"fetchone": lambda self: (300,)})()
            if "MAX(trade_date)" in sql:
                return type("Row", (), {"fetchone": lambda self: ("20260731",)})()
            if "COUNT(DISTINCT ts_code) FROM stock_universe_history" in sql:
                return type("Row", (), {"fetchone": lambda self: (300,)})()
            if "FROM bars" in sql:
                return type("Row", (), {"fetchone": lambda self: ("20260831",)})()
            raise AssertionError(sql)
        def close(self):
            pass

    monkeypatch.setattr(adapter.sqlite3, "connect", lambda *args, **kwargs: Conn())

    result = adapter.market_data_gate_status()

    assert result["ok"] is False
    assert result["reason"] == "quant_etf_bars_stale"
    assert result["stale_etfs"] == ["510300", "511380", "512100", "513100", "518880"]
