from decimal import Decimal
import sqlite3
from dataclasses import replace

import pandas as pd
import pytest

from qsim.engine.cash_distributions import CashDistribution, CashDistributionBook, dividend_tax_rate
from qsim.engine.cross_sectional_runner import CrossSectionalData, run_cross_sectional_backtest, target_shares_for_budget
from qsim.engine.matcher import buy_exact, sell_exact, buy
from qsim.engine.costs import CostModel
from qsim.services import distributions


ZERO=CostModel(commission_rate=0,min_commission=0,slippage=0,stamp_tax=0,transfer_fee_rate=0)


def action(kind='a_share', **kwargs):
    values=dict(key='cash1',symbol='000001.SZ' if kind=='a_share' else '510300',kind=kind,
                record_date='20260109',ex_date='20260112',pay_date='20260114',gross_per_unit='1')
    values.update(kwargs)
    return CashDistribution(**values)


@pytest.mark.parametrize('acquired,sold,expected',[
 ('20260108','20260208','.20'),('20260108','20260209','.10'),
 ('20250108','20260108','.10'),('20250108','20260109','0'),
 ('20260131','20260228','.20'),('20260131','20260301','.10'),
 ('20250130','20250228','.20'),('20250130','20250301','.10')])
def test_natural_calendar_holding_period(acquired,sold,expected):
    assert dividend_tax_rate(acquired,sold)==Decimal(expected)


def test_stock_entitlement_receivable_payment_and_deferred_tax():
    a=action();b=CashDistributionBook([a],strict_rounding=True)
    assert b.open('20260108')==0
    assert b.close('20260108',{a.symbol:300})==0
    b.open('20260109');assert b.close('20260109',{a.symbol:300})==0
    assert b.valuation('20260109')['distribution_value']==0
    b.open('20260112');assert b.close('20260112',{a.symbol:300})==0
    assert b.valuation('20260112')==dict(cash_receivable=300,dividend_tax_liability=60,distribution_value=240)
    b.open('20260114');assert b.close('20260114',{a.symbol:300})==300
    assert b.valuation('20260114')['dividend_tax_liability']==60
    assert b.tax_reserve('20260115',{})==60
    b.open('20260115');assert b.close('20260115',{})==-60
    assert b.valuation('20260115')['distribution_value']==0
    assert [(e['kind'],e['cash_delta']) for e in b.events]==[
      ('RECORD_ENTITLEMENT','0'),('EX_RECEIVABLE','0'),('CASH_PAYMENT','300.00'),('DIVIDEND_TAX','-60.00')]
    with pytest.raises(RuntimeError):b.close('20260115',{})
    assert sum(Decimal(e['cash_delta']) for e in b.events)==240


def test_etf_right_survives_exit_and_payment_without_stock_tax():
    a=action('mainland_etf');b=CashDistributionBook([a])
    b.close('20260108',{a.symbol:100})
    b.close('20260109',{a.symbol:100})
    b.close('20260112',{})
    assert b.valuation('20260112')['cash_receivable']==100
    assert b.valuation('20260112')['dividend_tax_liability']==0
    assert b.close('20260114',{})==100
    assert b.close('20260115',{})==0


def test_purchase_after_record_has_no_entitlement():
    a=action();b=CashDistributionBook([a])
    b.close('20260109',{})
    b.close('20260112',{a.symbol:100})
    assert b.close('20260114',{a.symbol:100})==0
    assert b.events==[]


def test_sale_before_payment_keeps_gross_receivable_and_realized_tax():
    a=action();b=CashDistributionBook([a])
    b.close('20260108',{a.symbol:100});b.close('20260109',{a.symbol:100})
    assert b.close('20260112',{})==0
    assert b.valuation('20260112')['distribution_value']==80
    assert b.close('20260114',{})==80
    assert b.valuation('20260114')['distribution_value']==0


def test_fifo_uses_end_of_day_net_changes():
    a=action(record_date='20260210',ex_date='20260211',pay_date='20260212')
    b=CashDistributionBook([a]);b.close('20260108',{a.symbol:100})
    b.close('20260209',{a.symbol:200});b.close('20260210',{a.symbol:200})
    b.close('20260211',{a.symbol:200})
    # A same-day sell+rebuy with unchanged final quantity must retain tax lots.
    b.close('20260212',{a.symbol:200})
    assert b.tax_reserve('20260213',{a.symbol:100})==10
    assert b.close('20260213',{a.symbol:100})==-10
    assert b.valuation('20260213')['dividend_tax_liability']==20
    assert b.close('20260216',{})==-20


@pytest.mark.parametrize('changes',[
 {'share_ratio':'.1'},{'gross_per_unit':'NaN'},{'gross_per_unit':'-1'},
 {'pay_date':''},{'record_date':'20260230'},{'pay_date':'20260108'},
 {'record_date':'20150908','ex_date':'20150909','pay_date':'20150910'}])
def test_unknown_share_or_tax_evidence_fails_closed(changes):
    a=action(**changes);b=CashDistributionBook([a])
    with pytest.raises((ValueError,RuntimeError)):
        b.close(a.record_date,{a.symbol:100})


def test_subcent_allocation_is_blocked_in_formal_mode():
    a=action(gross_per_unit='.00001')
    with pytest.raises(RuntimeError,match='allocation'):
        CashDistributionBook([a],strict_rounding=True).close(a.record_date,{a.symbol:100})


@pytest.mark.parametrize('quantity,accepted',[(100,False),(199,False),(200,True),(201,True),(350,True)])
def test_star_order_minimum_and_unit_step(quantity,accepted):
    cash,shares,trade,reason=buy_exact(10000,0,10,'20260109',ZERO,'688001.SH',quantity)
    assert bool(trade)==accepted
    if accepted:assert shares==quantity and cash==10000-quantity*10
    else:assert (cash,shares)==(10000,0)


@pytest.mark.parametrize('available,quantity,accepted',[(350,100,False),(350,199,False),(350,200,True),(350,201,True),(350,350,True),(199,100,False),(199,199,True)])
def test_star_sell_residual_and_partial_order_rules(available,quantity,accepted):
    cash,shares,trade,reason=sell_exact(0,available,10,'20260109',ZERO,'689001.SH',quantity)
    assert bool(trade)==accepted
    assert shares==available-quantity if accepted else shares==available


def test_star_budget_sizing_does_not_buy_an_illegal_100_share_order():
    assert target_shares_for_budget(1999,10,'688001.SH',ZERO)==0
    assert target_shares_for_budget(2010,10,'688001.SH',ZERO)==201
    assert buy(1999,0,10,'20260109',ZERO,'688001.SH')[2] is None


def test_official_price_limits_handle_st_and_ex_dividend_reference():
    cost=replace(ZERO,daily_limits={('20260112','000001.SZ'):(8.55,9.45)},require_daily_limits=True)
    assert buy_exact(10000,0,9.45,'20260112',cost,'000001.SZ',100)[2] is None
    assert sell_exact(0,100,8.55,'20260112',cost,'000001.SZ',100)[2] is None
    assert buy_exact(10000,0,9,'20260112',cost,'000001.SZ',100)[2] is not None
    with pytest.raises(RuntimeError,match='missing'):
        buy_exact(10000,0,9,'20260113',cost,'000001.SZ',100)


class Select:
    top_n=1
    def select(self,ctx):return list(ctx.factor_scores.sort_values(ascending=False).index)


def test_cross_runner_cash_nav_and_frozen_candidates():
    dates=['20260108','20260109','20260112','20260114','20260115']
    prices=pd.DataFrame({'000001.SZ':[10,10,9,9,9],'000002.SZ':[10]*5},index=dates)
    scores=pd.DataFrame({'000001.SZ':[1],'000002.SZ':[2]},index=[dates[0]])
    data=CrossSectionalData(dates,scores,prices,prices)
    frozen={dates[0]:{'candidates':['000001.SZ'],'allocation':1}}
    r=run_cross_sectional_backtest(data,Select(),1000,ZERO,rebalance_dates=[dates[0]],
        distributions=[action()],frozen_signals=frozen,strict_distributions=True)
    assert r['trades'][0]['symbol']=='000001.SZ'
    assert r['trades'][0]['date']=='20260109'
    rows=r['equity'].set_index('date')
    assert rows.loc['20260112','total_value']==980 # 900 market +100 receivable -20 tax
    assert rows.loc['20260114','cash']==100
    assert rows.loc['20260114','total_value']==980
    assert r['decisions'][0]['candidates']==['000001.SZ']


def test_source_loader_uses_gross_and_deduplicates_fund_aliases():
    db=sqlite3.connect(':memory:');db.executescript(distributions.SCHEMA)
    for symbol,source,records in [
       ('000001.SZ','dividend',[{'ts_code':'000001.SZ','div_proc':'实施','record_date':'20260109','ex_date':'20260112','pay_date':'20260114','cash_div_tax':1,'cash_div':.8}]),
       ('510300','fund_div',[{'ts_code':code,'record_date':'20260109','ex_date':'20260112','pay_date':'20260114','div_cash':.138,'base_unit':10000} for code in ('510300.SH','510300.OF')])]:
        db.execute('INSERT INTO qsim_distribution_coverage VALUES(?,?,?,?)',(symbol,'20260115','x','x'))
        for row in records:db.execute('INSERT INTO qsim_distribution_observations VALUES(?,?,?,?,?)',(symbol,source,distributions.digest(row),'x',distributions.encoded(row)))
    acts=distributions.load(db,['000001.SZ','510300'],'20260101','20260115','20260115')
    assert len(acts)==2
    assert {a.symbol:Decimal(a.gross_per_unit) for a in acts}=={'000001.SZ':Decimal(1),'510300':Decimal('.138')}
    assert all(a.share_ratio=='0' for a in acts)
    with pytest.raises(RuntimeError,match='coverage'):
        distributions.load(db,['000002.SZ'],'20260101','20260115','20260115')


def test_failed_provider_never_marks_coverage(monkeypatch):
    db=sqlite3.connect(':memory:')
    monkeypatch.setattr(distributions.time,'sleep',lambda _:None)
    class Bad:
        def query(self,*args,**kwargs):raise RuntimeError('synthetic denied')
    with pytest.raises(RuntimeError,match='synthetic'):
        distributions.collect(db,['000001.SZ'],'20260115',Bad())
    assert db.execute('SELECT COUNT(*) FROM qsim_distribution_coverage').fetchone()[0]==0


def test_paging_uses_endpoint_safe_size_and_detects_repeated_page(monkeypatch):
    monkeypatch.setattr(distributions.time,'sleep',lambda _:None)
    class Pages:
        def query(self,endpoint,**kwargs):
            assert kwargs['limit']==1000
            return pd.DataFrame([{'x':kwargs['offset']+i} for i in range(1000 if kwargs['offset']==0 else 1)])
    assert len(distributions.paged(Pages(),'dividend'))==1001
    class Repeated:
        def query(self,*args,**kwargs):return pd.DataFrame([{'x':i} for i in range(1000)])
    with pytest.raises(RuntimeError,match='repeated'):
        distributions.paged(Repeated(),'dividend')


def test_explicit_conservative_payment_rule_retains_fractional_entitlement():
    a=action('mainland_etf',gross_per_unit='.00099')
    b=CashDistributionBook([a],rounding_policy='conservative_floor')
    b.close('20260109',{a.symbol:101})
    b.close('20260112',{a.symbol:101})
    assert b.valuation('20260112')['cash_receivable']==.09 # 0.09999 total, never 0 or 0.10
    assert b.close('20260114',{a.symbol:101})==.09
    assert Decimal(b.state()['rounding_shortfall'])==Decimal('.00999')
    assert b.state()['cash_payment_rounding']=='conservative_floor'
    assert b.state()['rounding_used'] is True
    with pytest.raises(ValueError):CashDistributionBook([a],rounding_policy='round_to_make_target')


@pytest.mark.parametrize('side,opening,lower,upper,expected',[
 ('BUY',99.99,80,100,100),('SELL',80.01,80,100,80)])
def test_slippage_fill_cannot_cross_official_daily_price_band(side,opening,lower,upper,expected):
    cost=replace(ZERO,slippage=.0005,daily_limits={('20260112','000001.SZ'):(lower,upper)},require_daily_limits=True)
    fn=buy_exact if side=='BUY' else sell_exact
    result=fn(30000,200,opening,'20260112',cost,'000001.SZ',200)
    assert result[2] is not None
    assert result[2].price==expected
    assert lower<=result[2].price<=upper
