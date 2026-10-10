import sqlite3
import pytest
from qsim.market.execution_prices import require_raw_stock_window


def source():
    c=sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE stock_bars(ts_code,date,open,close);
        INSERT INTO stock_bars VALUES('000001.SZ','20260929',5,5);
        INSERT INTO stock_bars VALUES('000001.SZ','20260930',10,10);
        CREATE TABLE stock_execution_bars(ts_code,date,open,close,high,low,vol);
        INSERT INTO stock_execution_bars VALUES('000001.SZ','20260929',10,10,11,9,1000);
        INSERT INTO stock_execution_bars VALUES('000001.SZ','20260930',10,10,11,9,1000);''')
    return c


def test_raw_window_may_differ_from_adjusted_signal_panel():
    require_raw_stock_window(source(),'20260929','20260930')


def test_partial_raw_window_cannot_be_accepted_as_full_replay():
    c=source();c.execute("DELETE FROM stock_execution_bars WHERE date='20260929'")
    with pytest.raises(RuntimeError,match='窗口不完整'):require_raw_stock_window(c,'20260929','20260930')


def test_missing_raw_table_never_falls_back_to_qfq():
    c=source();c.execute('DROP TABLE stock_execution_bars')
    with pytest.raises(RuntimeError,match='前复权'):require_raw_stock_window(c,'20260929','20260930')


def test_paper_fills_shares_and_nav_use_raw_prices_while_scores_use_qfq(monkeypatch):
    import pandas as pd
    from qsim.services import paper_daily as paper
    from qsim.storage.paper import SCHEMA
    from qsim.strategies.cross_sectional import MultiFactorStrategy
    from qsim.features import factor
    c=source();c.row_factory=sqlite3.Row;c.executescript(SCHEMA)
    c.execute("INSERT INTO qsim_accounts(strategy,initial_capital,cash,shares,avg_cost,created_date) VALUES('multi_factor',10000,10000,0,0,'20260901')")
    c.execute("UPDATE stock_bars SET date=CASE date WHEN '20260929' THEN '20260901' ELSE '20260902' END")
    c.execute("UPDATE stock_execution_bars SET date=CASE date WHEN '20260929' THEN '20260901' ELSE '20260902' END")
    c.execute('UPDATE stock_bars SET open=5,close=5')
    c.execute('UPDATE stock_execution_bars SET open=10.1,close=10.1')
    monkeypatch.setattr(factor,'compute_factor_ic_scores_asof',lambda *a,**kw:{'20260901':pd.Series({'000001.SZ':1.})})
    monkeypatch.setitem(paper.CROSS_STRATEGIES,'multi_factor',MultiFactorStrategy(top_n=1))
    paper._run_cross_sectional(c,'multi_factor',['20260901','20260902'],1,market_conn=c)
    trade=c.execute('SELECT price,shares FROM qsim_trades').fetchone()
    assert trade['price']==10.11
    assert trade['shares']==900
    final=c.execute("SELECT position_value FROM qsim_snapshots WHERE date='20260902'").fetchone()[0]
    assert final==9090


def test_blocked_paper_cannot_freeze_or_execute_or_write_ledger(monkeypatch,tmp_path):
    from qsim.services import paper_daily,paper_orders
    monkeypatch.setenv('QSIM_PAPER_AUTORUN','0')
    monkeypatch.setenv('QSIM_MUTATION_LOCK',str(tmp_path/'lock'))
    monkeypatch.setattr(paper_orders,'get_conn',lambda *a:pytest.fail('blocked execution accessed ledger'))
    monkeypatch.setattr(paper_daily,'init_schema',lambda *a:pytest.fail('blocked settlement accessed ledger'))
    assert paper_orders.freeze_order_intent()['ok'] is False
    assert paper_orders.execute_order_open()['ok'] is False
    with pytest.raises(RuntimeError,match='账本保留'):paper_daily.run_daily_settlement()
