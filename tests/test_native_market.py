import sqlite3
import json

import pandas as pd
import pytest

from qsim.services import native_market as native
from qsim.services import market_quality
from qsim.storage.paper import SCHEMA


@pytest.fixture
def market_root(tmp_path, monkeypatch):
    (tmp_path/'data').mkdir()
    q=sqlite3.connect(tmp_path/'data/quant.db')
    q.executescript(SCHEMA + '''
    CREATE TABLE stock_info(ts_code TEXT PRIMARY KEY,name TEXT,industry TEXT,list_date TEXT);
    CREATE TABLE stock_fundamental(ts_code TEXT,ann_date TEXT,end_date TEXT,roe REAL,grossprofit_margin REAL,netprofit_margin REAL,eps REAL,debt_to_assets REAL,PRIMARY KEY(ts_code,end_date));
    CREATE TABLE bars(symbol TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(symbol,date));
    CREATE TABLE stock_bars(ts_code TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,vol REAL,amount REAL,vwap REAL,PRIMARY KEY(ts_code,date));
    CREATE TABLE stock_universe_history(index_code TEXT,trade_date TEXT,ts_code TEXT,weight REAL,PRIMARY KEY(index_code,trade_date,ts_code));
    CREATE TABLE stock_valuation(ts_code TEXT,date TEXT,pe REAL,pe_ttm REAL,pb REAL,dv_ratio REAL,total_mv REAL,circ_mv REAL,turnover_rate REAL,PRIMARY KEY(ts_code,date));
    ''')
    codes=[f'{i:06}.SZ' for i in range(300)]
    q.executemany('INSERT INTO stock_bars VALUES(?,?,?,?,?,?,?,?,?)',[(c,'20260929',5,6,4,5,1000,500,5) for c in codes])
    q.executemany('INSERT INTO stock_universe_history VALUES(?,?,?,?)',[('000300.SH','20260929',c,1) for c in codes])
    q.execute("INSERT INTO bars VALUES('513100','20260929',5,6,4,5,1000)")
    q.commit();q.close()
    v=sqlite3.connect(tmp_path/'data/v2_market.db')
    v.executescript('''
    CREATE TABLE adj_factor(ts_code TEXT,trade_date TEXT,adj_factor REAL,PRIMARY KEY(ts_code,trade_date));
    CREATE TABLE cb_basic_snapshot(ts_code TEXT PRIMARY KEY,stk_code TEXT,bond_short_name TEXT,list_date TEXT,delist_date TEXT,maturity_date TEXT,conv_price REAL,remain_size REAL,issue_size REAL);
    CREATE TABLE cb_daily_raw(ts_code TEXT,trade_date TEXT,open REAL,high REAL,low REAL,close REAL,vol REAL,amount REAL,PRIMARY KEY(ts_code,trade_date));
    CREATE TABLE stk_close_daily(ts_code TEXT,trade_date TEXT,close REAL,PRIMARY KEY(ts_code,trade_date));
    CREATE TABLE cb_indicator(ts_code TEXT,trade_date TEXT,close REAL,stk_close REAL,conv_value REAL,premium_rate REAL,dual_low REAL,PRIMARY KEY(ts_code,trade_date));
    CREATE TABLE corporate_actions(id INTEGER PRIMARY KEY,ex_date TEXT);
    ''')
    v.executemany('INSERT INTO adj_factor VALUES(?,?,?)',[(c,'20260929',1) for c in codes])
    v.execute("INSERT INTO cb_indicator VALUES('B0','20260929',100,5,50,100,200)")
    v.commit();v.close()
    monkeypatch.setenv('QSIM_MUTATION_LOCK',str(tmp_path/'lock'))
    monkeypatch.setattr(native.market,'_expected_latest_trade_date',lambda: '2026-09-30')
    return tmp_path


class Provider:
    fail=False
    def query(self,endpoint,**params):
        day=params.get('trade_date','20260930')
        bar={'trade_date':day,'open':5,'high':6,'low':4,'close':5,'vol':1000,'amount':500}
        if endpoint=='trade_cal': rows=[{'cal_date':d} for d in ['20260929','20260930']]
        elif endpoint=='stock_basic': rows=[{'ts_code':f'{i:06}.SZ','name':'stock'} for i in range(300)]
        elif endpoint=='fina_indicator': rows=[{'ts_code':params['ts_code'],'ann_date':'20260929','end_date':'20260630','roe':8}]
        elif endpoint=='daily': rows=[{**bar,'ts_code':f'{i:06}.SZ'} for i in range(300)]
        elif endpoint=='adj_factor': rows=[{'ts_code':f'{i:06}.SZ','trade_date':day,'adj_factor':2} for i in range(300)]
        elif endpoint=='cb_basic': rows=[{'ts_code':f'B{i}','stk_code':f'{i:06}.SZ','conv_price':10} for i in range(100)]
        elif endpoint=='cb_daily': rows=[{**bar,'ts_code':f'B{i}'} for i in range(100)]
        elif endpoint=='fund_daily': rows=[{**bar,'ts_code':'513100.SH'}]
        else: rows=[]
        if self.fail and endpoint=='adj_factor': rows=[]
        return pd.DataFrame(rows)


def test_native_updates_factors_and_records_supplier_corrections(market_root):
    result=native.run(root=market_root,pro=Provider())
    assert result['status']=='updated'
    with sqlite3.connect(market_root/'data/v2_market.db') as c:
        assert c.execute('SELECT MAX(trade_date) FROM adj_factor').fetchone()[0]=='20260930'
        assert c.execute("SELECT adj_factor FROM adj_factor WHERE trade_date='20260929' LIMIT 1").fetchone()[0]==2
    with sqlite3.connect(market_root/'data/quant.db') as c:
        assert c.execute('SELECT COUNT(*) FROM qsim_market_revisions WHERE table_name="adj_factor"').fetchone()[0]==300
        assert c.execute('SELECT COUNT(*) FROM qsim_data_sync_runs WHERE status="success"').fetchone()[0]==2
    assert market_quality.check('2026-09-30',market_root)['ok']
    assert native.run(root=market_root,pro=object())['status']=='up_to_date'


def test_failed_collection_keeps_market_and_audit_unchanged(market_root):
    provider=Provider();provider.fail=True
    with pytest.raises(RuntimeError,match='复权'):
        native.run(root=market_root,pro=provider)
    with sqlite3.connect(market_root/'data/quant.db') as c:
        assert c.execute('SELECT MAX(date) FROM stock_bars').fetchone()[0]=='20260929'
        assert c.execute('SELECT COUNT(*) FROM qsim_market_revisions').fetchone()[0]==0


def test_pool_membership_cannot_be_replaced_by_unrelated_rows(market_root):
    native.run(root=market_root,pro=Provider())
    with sqlite3.connect(market_root/'data/quant.db') as c:
        c.execute("UPDATE stock_universe_history SET ts_code='outsider-'||ts_code WHERE trade_date='20260929'")
    quality=market_quality.check('20260930',market_root)
    assert not quality['ok']
    assert any('池外' in item for item in quality['issues'])


def test_held_symbol_missing_blocks_settlement(market_root):
    native.run(root=market_root,pro=Provider())
    with sqlite3.connect(market_root/'data/quant.db') as c:
        c.execute("INSERT INTO qsim_positions(account_id,ts_code,shares,updated_date) VALUES(1,'999999.SZ',100,'20260929')")
    quality=market_quality.check('20260930',market_root)
    assert quality['details']['missing_critical']==['999999.SZ']
    assert not quality['ok']


def test_equity_prices_and_vwap_preserve_qfq_basis(market_root):
    class SplitProvider(Provider):
        def query(self, endpoint, **params):
            frame=super().query(endpoint, **params)
            if endpoint=='adj_factor' and params['trade_date']=='20260929':
                frame['adj_factor']=1
            return frame
    native.run(root=market_root,pro=SplitProvider())
    with sqlite3.connect(market_root/'data/quant.db') as conn:
        row=conn.execute("SELECT close,vwap FROM stock_bars WHERE ts_code='000000.SZ' AND date='20260929'").fetchone()
        assert row==(2.5,2.5)
        assert conn.execute("SELECT open,close FROM stock_execution_bars WHERE ts_code='000000.SZ' AND date='20260929'").fetchone()==(5,5)
    with sqlite3.connect(market_root/'data/v2_market.db') as conn:
        assert conn.execute("SELECT close FROM stk_close_daily WHERE ts_code='000000.SZ' AND trade_date='20260929'").fetchone()[0]==5


def test_qfq_anchor_change_cannot_splice_older_history(market_root):
    """A five-day refresh must not publish old anchor + new anchor in one series."""
    with sqlite3.connect(market_root/'data/quant.db') as c:
        c.execute("INSERT INTO stock_bars VALUES('000000.SZ','20260901',5,6,4,5,1000,500,5)")
    class Rolling(Provider):
        def query(self,endpoint,**params):
            if endpoint=='trade_cal':return pd.DataFrame({'cal_date':['20260929','20260930']})
            return super().query(endpoint,**params)
    with pytest.raises(RuntimeError,match='前复权基准'):
        native.run(root=market_root,pro=Rolling())
    with sqlite3.connect(market_root/'data/quant.db') as c:
        assert c.execute("SELECT close FROM stock_bars WHERE ts_code='000000.SZ' AND date='20260901'").fetchone()==(5,)
        assert c.execute('SELECT MAX(date) FROM stock_bars').fetchone()==('20260929',)
        assert c.execute('SELECT COUNT(*) FROM qsim_market_revisions').fetchone()==(0,)


def test_financial_revisions_preserved_and_current_view_is_latest(market_root):
    class Revisions(Provider):
        def query(self,endpoint,**params):
            if endpoint=='fina_indicator':
                return pd.DataFrame([{'ts_code':params['ts_code'],'ann_date':day,'end_date':'20260630','roe':roe} for day,roe in [('20260929',12),('20260831',8)]])
            return super().query(endpoint,**params)
    native.run(root=market_root,pro=Revisions())
    with sqlite3.connect(market_root/'data/quant.db') as c:
        assert c.execute("SELECT ann_date,roe FROM stock_fundamental WHERE ts_code='000000.SZ'").fetchone()==('20260929',12)
        assert c.execute("SELECT COUNT(*) FROM qsim_fundamental_versions WHERE ts_code='000000.SZ'").fetchone()==(2,)


def test_wal_attached_databases_cannot_claim_atomic_publication(market_root):
    with sqlite3.connect(market_root/'data/quant.db') as c:c.execute('PRAGMA journal_mode=WAL')
    with pytest.raises(RuntimeError,match='WAL'):
        native.run(root=market_root,pro=Provider())
    with sqlite3.connect(market_root/'data/quant.db') as c:
        assert c.execute('SELECT MAX(date) FROM stock_bars').fetchone()==('20260929',)
