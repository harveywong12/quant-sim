"""Synthetic, independently checkable defects found by the 2026-10-05 audit."""
import datetime as dt
import math
import sqlite3
import pandas as pd
import pytest
from qsim.engine.costs import CostModel
from qsim.engine.matcher import buy, buy_exact, sell_exact
from qsim.engine.execution import ExecutionPolicy
from qsim.engine.metrics import portfolio_metrics
from qsim.engine.rotation_runner import run_rotation_backtest
from qsim.engine.cross_sectional_runner import CrossSectionalData, run_cross_sectional_backtest
from qsim.strategies.cross_sectional import TopNScoreStrategy
from qsim.risk.limits import check_risk

FREE=CostModel(commission_rate=0,min_commission=0,stamp_tax=0,slippage=0,transfer_fee_rate=0)

def test_cost_config_uses_instance_defaults():
    assert CostModel.from_config({}) == CostModel()


def test_buy_budget_never_borrows_cash():
    cash,shares,trade,_=buy(1005,0,10,'20260902',FREE,'600001.SH',budget=10000)
    assert cash == 5 and shares == 100 and trade.amount == 1000

@pytest.mark.parametrize('price',[0,-1,float('nan'),float('inf')])
def test_invalid_price_never_mutates_ledger(price):
    cash,shares,trade,reason=buy(10000,0,price,'20260902',FREE,'600001.SH')
    assert (cash,shares,trade)==(10000,0,None) and reason

@pytest.mark.parametrize('quantity',[0,-100,150])
def test_exact_buy_requires_positive_board_lots(quantity):
    result=buy_exact(10000,0,10,'20260902',FREE,'600001.SH',quantity)
    assert result[:3]==(10000,0,None) and result[3]


def test_exact_sell_cannot_create_short_position():
    result=sell_exact(0,100,10,'20260902',FREE,'600001.SH',200)
    assert result[:3]==(0,100,None) and result[3]


def test_buy_cap_is_rounded_to_board_lots():
    result=buy(10000,0,10,'20260902',FREE,'600001.SH',shares_cap=150)
    assert result[1]==100

@pytest.mark.parametrize('volume',[None,0,-1,float('nan'),float('inf')])
def test_enabled_volume_policy_blocks_unknown_liquidity(volume):
    assert ExecutionPolicy(volume_participation=.1).fill_cap(volume)==0


def test_metrics_include_first_cost_and_initial_drawdown():
    frame=pd.DataFrame({'date':['20250102','20250103','20250106'],'total_value':[90.,99.,99.]})
    m=portfolio_metrics(frame,100,1)
    assert m['max_drawdown']==pytest.approx(-.1)
    assert m['sharpe']==pytest.approx(0.)  # returns [-.1,.1,0], hand-computable


def test_cagr_uses_elapsed_calendar_time():
    frame=pd.DataFrame({'date':['20240102','20250102'],'total_value':[100.,110.]})
    m=portfolio_metrics(frame,100,0)
    assert m['annual_return']==pytest.approx(1.1**(365.2425/366)-1)


def test_constant_return_sharpe_is_undefined():
    m=portfolio_metrics(pd.DataFrame({'date':['20250102','20250103'],'total_value':[100.,100.]}),100,0)
    assert m['sharpe'] is None

@pytest.mark.parametrize('weights,dd,turnover',[({'x':float('nan')},0,0),({'x':-.1},0,0),({'a':.4,'b':.4,'c':.4},0,0),({},float('nan'),0),({},0,float('inf'))])
def test_risk_rejects_nonfinite_or_invalid_allocation(weights,dd,turnover):
    assert not check_risk(weights,current_drawdown=dd,turnover=turnover).ok


def test_failed_cross_sale_keeps_holding_and_nav():
    days=['20260901','20260902','20260903']
    data=CrossSectionalData(days,pd.DataFrame({'600001.SH':[1,0,0],'600002.SH':[0,1,1]},index=days),pd.DataFrame({'600001.SH':[10,10,9],'600002.SH':[10,10,10]},index=days),pd.DataFrame({'600001.SH':[10,10,9],'600002.SH':[10,10,10]},index=days))
    result=run_cross_sectional_backtest(data,TopNScoreStrategy(top_n=1),10000,FREE,rebalance='daily')
    assert result['shares']=={'600001.SH':1000}
    assert result['equity'].iloc[-1].total_value==9000
    assert len(result['trades'])==1


def test_rotation_failed_sale_cannot_relabel_old_shares():
    class Switch:
        def generate(self,hist):return '510300' if len(hist['510300'])==1 else '518880'
    def bars(op,cl):return pd.DataFrame({'date':['20260901','20260902','20260903'],'open':op,'close':cl})
    result=run_rotation_backtest({'510300':bars([10,10,9],[10,10,9]),'518880':bars([2,2,2],[2,2,2])},Switch(),10500,FREE)
    assert result['symbol']=='510300' and result['shares']==1000
    assert result['cash']==500 and result['equity'].iloc[-1].total_value==9500


def test_preflight_never_creates_missing_database(tmp_path,monkeypatch):
    from qsim.services import preflight
    path=tmp_path/'absent.db';monkeypatch.setattr(preflight,'QUANT_DB',path)
    assert preflight._check_error_backlog()
    assert preflight._check_db_writable()
    assert not path.exists()


def test_preflight_preserves_existing_probe_table(tmp_path,monkeypatch):
    from qsim.services import preflight
    path=tmp_path/'market.db';monkeypatch.setattr(preflight,'QUANT_DB',path)
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE _preflight_probe(id INTEGER)')
        c.execute('INSERT INTO _preflight_probe VALUES(42)')
    assert preflight._check_db_writable()==[]
    with sqlite3.connect(path) as c:assert c.execute('SELECT id FROM _preflight_probe').fetchone()==(42,)


def test_ai_review_disabled_before_credentials_and_context(monkeypatch):
    from qsim.services import ai_advisor
    monkeypatch.setattr(ai_advisor,'_conn',lambda:pytest.fail('business context must remain local'))
    assert ai_advisor.run_daily_review()['status']=='disabled'
    with pytest.raises(RuntimeError,match='禁用'):ai_advisor.LlmClient()


def test_nonfinite_nav_cannot_pass_metrics():
    with pytest.raises(ValueError):portfolio_metrics(pd.DataFrame({'date':['20260901'],'total_value':[float('inf')]}),100,0)


def test_sell_etf_never_charges_stock_stamp_tax():
    cost=CostModel(commission_rate=0,min_commission=0,transfer_fee_rate=0,slippage=0)
    assert cost.calc_fee(10,100,'SELL','510300')==0

@pytest.mark.parametrize('day,stamp,transfer',[('20220428',.001,.00002),('20220429',.001,.00001),('20230827',.001,.00001),('20230828',.0005,.00001)])
def test_historical_stock_fee_boundaries(day,stamp,transfer):
    cost=CostModel(commission_rate=0,min_commission=0,slippage=0,historical_rates=True)
    assert cost.calc_fee(10,1000,'SELL','600001.SH',date=day)==pytest.approx(10000*(stamp+transfer))
    assert cost.calc_fee(10,1000,'BUY','600001.SH',date=day)==pytest.approx(10000*transfer)


def test_chinext_limit_switch_and_tick_rounding():
    cost=CostModel()
    assert cost.limit_blocked('BUY',11,10,'300001.SZ',date='20200821')
    assert cost.limit_blocked('BUY',11,10,'300001.SZ',date='20200824') is None
    assert cost.limit_blocked('BUY',13.58,12.35,'600001.SH') is None
    assert cost.limit_blocked('BUY',13.59,12.35,'600001.SH')

@pytest.mark.parametrize('combined',[False,True])
def test_shared_portfolio_keeps_suspended_holding_mark(combined):
    from qsim.engine.portfolio_runner import Sleeve,run_portfolio_backtest
    from qsim.engine.combined_runner import run_combined_portfolio_backtest
    from qsim.allocation.policy import RegimeAllocationPolicy
    from qsim.strategies.builtin.buy_hold import BuyHoldStrategy
    class Source:
        def get_bars(self,symbol,start=None,end=None):
            days=[1,2,3,4] if symbol=='510300' else [1,2,4]
            return pd.DataFrame({'date':[dt.date(2026,9,d) for d in days],'open':[10.]*len(days),'close':[10.]*len(days)})
    class Regime:
        def detect(self,history):return {'up':1.,'range':0.,'down':0.}
    from qsim.risk.limits import RiskLimits
    policy=RegimeAllocationPolicy({k:{'hold_stock':.4} for k in ('up','range','down')})
    common=dict(policy=policy,data_source=Source(),regime_detector=Regime(),initial_cash=10000.,cost=FREE,market_symbol='510300',assumed_alpha=.02,risk_limits=RiskLimits(max_single_strategy_weight=1.))
    sleeve=Sleeve('hold_stock','518880',BuyHoldStrategy())
    if combined:
        empty=pd.DataFrame(index=['20260901','20260902','20260903','20260904'])
        result=run_combined_portfolio_backtest(single_sleeves=[sleeve],cross_sleeves=[],rotation_sleeves=[],stock_open=empty,stock_close=empty,**common)
    else:result=run_portfolio_backtest(sleeves=[sleeve],**common)
    assert result['trades']
    assert result['equity'].total_value.tolist()==[10000.]*4


def test_default_ic_eligibility_cannot_use_future_price(monkeypatch):
    from qsim.features import factor
    codes=[f'SYNTHETIC{i}' for i in range(60)]
    close=pd.DataFrame([10.,11.],index=['20260901','20260902'],columns=['x']).reindex(columns=codes).fillna(10.)
    panels={'close':close}
    monkeypatch.setattr(factor,'_load_panels',lambda _:panels)
    monkeypatch.setattr(factor,'_features_on_date',lambda *_:pd.DataFrame({'mom_20':range(60)},index=codes))
    before=factor.compute_factor_ic_scores('unused',['20260901'],label_days=1)
    close.loc['20260902',codes[0]]=float('nan')
    after=factor.compute_factor_ic_scores('unused',['20260901'],label_days=1)
    pd.testing.assert_series_equal(before['20260901'],after['20260901'])


def test_research_gate_rejects_nan_instead_of_passing():
    from qsim.research.experiments import Candidate,evaluate_candidate
    candidate=Candidate(name='synthetic',oos_return=float('nan'),after_cost_return=float('nan'),max_correlation=float('nan'))
    assert not evaluate_candidate(candidate).passed


def test_watchdog_enforces_restart_budget_without_real_systemctl(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    bindir=tmp_path/'bin';bindir.mkdir()
    log=tmp_path/'restart.log'
    (bindir/'curl').write_text('#!/bin/sh\nexit 1\n')
    (bindir/'systemctl').write_text('#!/bin/sh\nprintf "restart\\n" >> "$QSIM_TEST_RESTART_LOG"\n')
    for p in bindir.iterdir():p.chmod(0o700)
    env={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ['PATH'],
         'XDG_RUNTIME_DIR':str(tmp_path),'QSIM_TEST_RESTART_LOG':str(log)}
    script=Path(__file__).resolve().parents[1]/'scripts/qsim-watchdog.sh'
    for _ in range(5):subprocess.run(['/bin/bash',str(script)],env=env,check=True,capture_output=True)
    assert len(log.read_text().splitlines())==3
