import json
import sqlite3

import pandas as pd
import pytest

from qsim.engine.costs import CostModel
from qsim.engine.cash_distributions import CashDistribution
from qsim.engine.runner import run_daily_executor
from qsim.engine.rotation_runner import run_rotation_backtest
from qsim.services import distributions, paper_daily, paper_orders, ledger_publish
from qsim.storage.paper import SCHEMA, seed_accounts
ZERO=CostModel(commission_rate=0,min_commission=0,slippage=0,stamp_tax=0,transfer_fee_rate=0)
def action(kind='a_share'):
    return CashDistribution('cash1','000001.SZ' if kind=='a_share' else '510300',kind,'20260109','20260112','20260114','1')


class EnterHold:
    def generate(self,history):return ('BUY',1.0) if len(history)==1 else ('HOLD',1.0)


def bars():
    return pd.DataFrame({'date':pd.to_datetime(['2026-01-08','2026-01-09','2026-01-12','2026-01-14']),
                         'open':[10,10,9,9],'close':[10,10,9,9],'volume':[1000]*4})


def test_daily_etf_nav_separates_receivable_from_spendable_cash():
    r=run_daily_executor(bars(),EnterHold(),1000,ZERO,'510300',distributions=[action('mainland_etf')],freeze_last_signal=True,strict_distributions=True)
    e=r['equity'].set_index('date')
    assert e.loc['20260112','cash']==0
    assert e.loc['20260112','position_value']==900
    assert e.loc['20260112','total_value']==1000
    assert e.loc['20260114','cash']==100
    assert e.loc['20260114','total_value']==1000
    assert r['decisions'][-1]['date']=='20260114'


def test_daily_frozen_signal_prevents_revised_past_decision():
    class NeverBuy:
        def generate(self,history):return 'HOLD'
    r=run_daily_executor(bars(),NeverBuy(),1000,ZERO,'510300',frozen_signals={'20260108':{'signal':'BUY','weight':1}},freeze_last_signal=True)
    assert r['trades'][0]['date']=='20260109'
    assert r['shares']==100


def test_zero_volume_blocks_daily_execution():
    frame=bars();frame.loc[1,'volume']=0
    r=run_daily_executor(frame,EnterHold(),1000,ZERO,'510300',strict_distributions=True)
    assert r['trades']==[]
    assert r['cash']==1000


def test_rotation_receives_payment_after_exit_and_preserves_full_warmup():
    dates=['20260108','20260109','20260112','20260114']
    frame=bars();frame['date']=[d[:4]+'-'+d[4:6]+'-'+d[6:] for d in dates]
    class Rotate:
        def generate(self,history):return '510300' if len(history['510300'])<3 else 'CASH'
    r=run_rotation_backtest({'510300':frame},Rotate(),1000,ZERO,distributions=[action('mainland_etf')],strict_distributions=True)
    assert [t['side'] for t in r['trades']]==['BUY','SELL']
    assert r['cash']==1000 and r['shares']==0
    class NeedHistory:
        lengths=[]
        def generate(self,history):self.lengths.append(len(history['510300']));return 'HOLD'
    strategy=NeedHistory()
    run_rotation_backtest({'510300':frame},strategy,1000,ZERO,start='20260112',warmup=True)
    assert strategy.lengths==[3,4]


def conn():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;c.executescript(SCHEMA+ledger_publish.SCHEMA)
    seed_accounts(c,{'s':{'symbol':'510300','initial':1000}})
    return c


def append_stage(stage,day='20260109',cash=1000):
    stage.execute('INSERT INTO qsim_snapshots(date,strategy,total_value,cash,position_value) VALUES(?,?,?,?,?)',(day,'s',cash,cash,0))
    stage.execute('INSERT INTO qsim_distribution_nav VALUES(?,?,?,?)',('s',day,0,0))
    decision={'date':day,'signal':'HOLD','weight':1}
    stage.execute('INSERT INTO qsim_frozen_daily_signals VALUES(?,?,?,?)',('s',day,distributions.encoded(decision),distributions.digest(decision)))
    stage.execute('INSERT OR REPLACE INTO qsim_distribution_checkpoints VALUES(?,?,?)',('s',day,'{}'))
    stage.commit()


def test_atomic_publisher_commits_supplementary_ledger_and_decision():
    live,stage=conn(),conn();append_stage(stage)
    result=ledger_publish.promote(live,stage,['s'],{},paper_daily._copy_rows)
    assert result['appended']=={'snapshots':1,'trades':0}
    assert live.execute('SELECT COUNT(*) FROM qsim_distribution_nav').fetchone()[0]==1
    assert live.execute('SELECT COUNT(*) FROM qsim_frozen_daily_signals').fetchone()[0]==1
    assert live.execute('SELECT date FROM qsim_distribution_checkpoints').fetchone()[0]=='20260109'
    repeat=ledger_publish.promote(live,stage,['s'],{},paper_daily._copy_rows)
    assert repeat['appended']=={'snapshots':0,'trades':0}


def test_publisher_failure_rolls_back_nav_and_signals_with_snapshots():
    live,stage=conn(),conn();append_stage(stage)
    def broken(src,dst,table,where,params):
        paper_daily._copy_rows(src,dst,table,where,params)
        if table=='qsim_frozen_daily_signals':raise RuntimeError('synthetic crash')
    with pytest.raises(RuntimeError,match='synthetic'):
        ledger_publish.promote(live,stage,['s'],{},broken)
    for table in ('qsim_snapshots','qsim_distribution_nav','qsim_frozen_daily_signals','qsim_settlement_runs'):
        assert live.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0


def test_publisher_rejects_changed_action_history_even_if_total_nav_same():
    live,stage=conn(),conn();append_stage(live);append_stage(stage)
    stage.execute('UPDATE qsim_distribution_nav SET cash_receivable=1,dividend_tax_liability=1');stage.commit()
    with pytest.raises(RuntimeError):ledger_publish.promote(live,stage,['s'],{},paper_daily._copy_rows)
    assert live.execute('SELECT cash_receivable FROM qsim_distribution_nav').fetchone()[0]==0


def test_intraday_open_route_disabled_before_any_private_access(monkeypatch):
    monkeypatch.setenv('QSIM_FORMAL_PAPER','1');monkeypatch.setenv('QSIM_PAPER_AUTORUN','1')
    monkeypatch.setattr(paper_orders,'get_conn',lambda *_:pytest.fail('private DB should not be opened'))
    result=paper_orders.execute_order_open(quote_loader=lambda *_:pytest.fail('network should not be called'))
    assert result['ok'] is False and 'daily' in result['error']


def test_revised_or_alias_conflicting_implemented_action_is_not_summed():
    c=sqlite3.connect(':memory:');c.executescript(distributions.SCHEMA)
    c.execute('INSERT INTO qsim_distribution_coverage VALUES(?,?,?,?)',('510300','20260115','x','x'))
    for amount in (1,2):
        row={'ts_code':'510300.SH','record_date':'20260109','ex_date':'20260112','pay_date':'20260114','div_cash':amount}
        c.execute('INSERT INTO qsim_distribution_observations VALUES(?,?,?,?,?)',('510300','fund_div',distributions.digest(row),'x',distributions.encoded(row)))
    with pytest.raises(RuntimeError,match='conflicting'):
        distributions.load(c,['510300'],'20260101','20260115','20260115')
