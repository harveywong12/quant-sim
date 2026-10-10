import sqlite3
import pandas as pd
import pytest
from .test_native_market import Provider, market_root
from qsim.services import native_market as native

class FullProvider(Provider):
    missing_raw = False
    missing_factor = False
    inconsistent = False
    def query(self, endpoint, **params):
        if params.get('ts_code')=='000000.SZ' and endpoint in ('daily','adj_factor'):
            dates=['20260901','20260929','20260930']
            if endpoint=='daily':
                if self.missing_raw:dates.remove('20260901')
                return pd.DataFrame([{'ts_code':'000000.SZ','trade_date':day,'open':5,'high':6,'low':4,
                    'close':7 if self.inconsistent and day=='20260930' else 5,'vol':1000,'amount':500} for day in dates])
            if self.missing_factor:dates.remove('20260901')
            return pd.DataFrame([{'ts_code':'000000.SZ','trade_date':day,'adj_factor':2} for day in dates])
        return super().query(endpoint,**params)

def older(root):
    with sqlite3.connect(root/'data/quant.db') as c:
        c.execute("INSERT INTO stock_bars VALUES('000000.SZ','20260901',2.5,3,2,2.5,1000,500,2.5)")

def test_anchor_change_rebuilds_every_stored_date_from_raw_not_rounded_qfq(market_root):
    older(market_root)
    result=native.run(root=market_root,pro=FullProvider())
    assert result['full_adjustment_rebuild_count']==1
    with sqlite3.connect(market_root/'data/quant.db') as c:
        assert c.execute("SELECT date,close,vwap FROM stock_bars WHERE ts_code='000000.SZ' ORDER BY date").fetchall()==[(d,5,5) for d in ['20260901','20260929','20260930']]
        assert c.execute("SELECT close FROM stock_execution_bars WHERE ts_code='000000.SZ' AND date='20260901'").fetchone()==(5,)
        assert c.execute('SELECT COUNT(*) FROM qsim_market_revisions WHERE table_name=? AND key_json LIKE ?',('stock_bars','%20260901%')).fetchone()[0]==1
    assert native.run(root=market_root,pro=object())['status']=='up_to_date'

@pytest.mark.parametrize('failure',['missing_raw','missing_factor','inconsistent'])
def test_incomplete_or_inconsistent_full_history_keeps_publication_unchanged(market_root,failure):
    older(market_root);provider=FullProvider();setattr(provider,failure,True)
    with pytest.raises(RuntimeError,match='前复权基准变化|OHLC'):
        native.run(root=market_root,pro=provider)
    with sqlite3.connect(market_root/'data/quant.db') as c:
        assert c.execute("SELECT close FROM stock_bars WHERE date='20260901'").fetchone()==(2.5,)
        assert c.execute('SELECT MAX(date) FROM stock_bars').fetchone()==('20260929',)
        assert c.execute('SELECT COUNT(*) FROM qsim_market_revisions').fetchone()==(0,)
