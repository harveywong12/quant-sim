import json
import sqlite3
import pandas as pd
import pytest
from qsim.research.screening import classify, cost_stress, metrics, chain_metrics, freeze, verify_contract, ETF_COST
from qsim.research.screening_execution import simulate_basket
from qsim.engine.costs import CostModel
from qsim.strategies.cross_sectional import TopNScoreStrategy

ZERO = CostModel(commission_rate=0, min_commission=0, slippage=0, stamp_tax=0, transfer_fee_rate=0)


def test_failed_resize_sell_keeps_shares_and_cash():
    dates=['20220104','20220105','20220106','20220107']
    opens=pd.DataFrame({'600001.SH':[10,10,8,10]},index=dates)
    close=pd.DataFrame({'600001.SH':[10]*4},index=dates)
    scores=pd.DataFrame({'600001.SH':[1,1]},index=[dates[0],dates[1]])
    r=simulate_basket(opens,close,scores,TopNScoreStrategy(1),dates,ZERO,
                      allocations={dates[0]:1,dates[1]:.5})
    assert len(r['trades'])==1
    assert r['shares']['600001.SH']==10000
    assert r['equity'].iloc[2].total_value==100000
    assert r['skips'] and r['cash']==0


def test_missing_volume_blocks_fill_and_never_assumes_infinite_liquidity():
    days=['20220104','20220105']; prices=pd.DataFrame({'600001.SH':[10,10]},index=days)
    scores=pd.DataFrame({'600001.SH':[1]},index=days[:1])
    volume=pd.DataFrame({'600001.SH':[float('nan'),0]},index=days)
    r=simulate_basket(prices,prices,scores,TopNScoreStrategy(1),days,ZERO,volume=volume)
    assert not r['trades'] and r['cash']==100000 and r['skips']


def test_signal_is_next_open_and_future_close_cannot_affect_affordability():
    days=['20220104','20220105','20220106']
    opens=pd.DataFrame({'600001.SH':[10,10,10]},index=days)
    close=opens.copy();close.iloc[1,0]=1000
    scores=pd.DataFrame({'600001.SH':[1]},index=days[:1])
    r=simulate_basket(opens,close,scores,TopNScoreStrategy(1),days,ZERO,sizing='live_affordable')
    assert r['trades'][0]['date']==days[1] and r['trades'][0]['shares']==10000
    assert r['equity'].iloc[0].total_value==100000


def annual(year=2022, ret=.1, trades=10, decision=True):
    return {'year':year,'decision':decision,'metrics':{'days':242,'total_return':ret,'num_trades':trades}}


def stress():
    return {str(n):{'total_return':.3,'max_drawdown':-.1,'excess_return':.1,'benchmark_drawdown':-.2} for n in (1,2,4)}


def test_partial_year_never_changes_screening_decision():
    a=[annual(y) for y in range(2022,2026)];s=stress()
    assert classify(a,s,[])['state']=='observe'
    assert classify(a+[annual(2026,-.99,0,False)],s,[])==classify(a,s,[])
    a[0]['metrics']['total_return']=-.1;a[1]['metrics']['total_return']=-.1
    assert classify(a,s,[])['state']=='failed'


def test_data_and_trade_gates_block_even_spectacular_returns():
    a=[annual(y,10) for y in range(2022,2026)]
    assert classify(a,stress(),['缺少历史名单'])['state']=='data_blocked'
    a=[annual(y,10,1) for y in range(2022,2026)]
    assert classify(a,stress(),[])['state']=='insufficient'
    s=stress();s['4']['total_return']=-.01
    assert classify([annual(y) for y in range(2022,2026)],s,[])['state']=='failed'


def test_stress_increases_friction_without_changing_statutory_tax():
    s=cost_stress(ETF_COST,4)
    assert s.commission_rate==ETF_COST.commission_rate*4
    assert s.slippage==ETF_COST.slippage*4 and s.stamp_tax==ETF_COST.stamp_tax


def test_initial_fee_and_first_day_benchmark_return_are_included():
    r={'equity':pd.DataFrame([{'date':'20220104','total_value':99000,'position_value':0},
                             {'date':'20220105','total_value':110000,'position_value':0}]),'trades':[]}
    b=pd.Series([10,11,12],index=['20211231','20220104','20220105'])
    m,returns,nav=metrics(r,b)
    assert m['max_drawdown']==pytest.approx(-.01)
    assert returns.iloc[0]==pytest.approx(-.01)
    assert m['benchmark_return']==pytest.approx(.2)
    chain=chain_metrics([{'values':r['equity'].total_value.to_numpy(),'bnav':nav}]*2)
    assert chain['total_return']==pytest.approx(.21)
    assert chain['benchmark_return']==pytest.approx(.44)


def test_future_inputs_do_not_change_prior_basket_trades():
    days=['20220104','20220105','20220106','20220107']
    price=pd.DataFrame({'600001.SH':[10,11,12,13]},index=days)
    scores=pd.DataFrame({'600001.SH':[1]},index=days[:1])
    before=simulate_basket(price,price,scores,TopNScoreStrategy(1),days[:3],ZERO)
    changed=price.copy();changed.iloc[3,0]=5000
    after=simulate_basket(changed,changed,scores,TopNScoreStrategy(1),days[:3],ZERO)
    assert before['trades']==after['trades']
    pd.testing.assert_frame_equal(before['equity'],after['equity'])


def test_frozen_input_or_contract_changes_are_rejected(tmp_path):
    root=tmp_path/'src';(root/'qsim').mkdir(parents=True);(root/'qsim'/'__init__.py').write_text('')
    (root/'config').mkdir()
    db=tmp_path/'market.db'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE bars(symbol TEXT,date TEXT)');c.execute("INSERT INTO bars VALUES('510300','20260930')")
        c.execute('CREATE TABLE stock_bars(date TEXT)');c.execute('CREATE TABLE stock_valuation(date TEXT)')
        c.execute('CREATE TABLE stock_universe_history(index_code TEXT,trade_date TEXT)')
        c.execute("INSERT INTO stock_universe_history VALUES('000300.SH','20260930')")
    run_dir=freeze(db,tmp_path/'out',root)
    assert verify_contract(run_dir,root)['source_end']=='20260930'
    manifest=run_dir/'contract.json';manifest.chmod(0o600)
    data=json.loads(manifest.read_text());data['gates']['min_trades']=0;manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='checksum'):
        verify_contract(run_dir,root)


def test_report_ui_explains_historical_evidence_and_quarantine(monkeypatch):
    from qsim.services.web import mobile, qsim_data
    data={'mode':'chronological_screening','state':'research_only','run_id':'frozen-test',
          'contract':{'source_end':'20260930'},'strategies':[
            {'key':'bad','label':'问题策略','annual':[],'stress':{},
             'decision':{'state':'data_blocked','label':'数据待补','reasons':['缺少历史名单']}}]}
    monkeypatch.setattr(qsim_data,'strategy_screening',lambda:data)
    page=mobile._strategy_screening_card()
    assert '数据待补' in page and '缺少历史名单' in page
    assert '不能称为真实样本外通过' in page and '未自动晋升' in page
    assert '2026补充' in page and '四个完整年度' in page


@pytest.mark.parametrize('scorer_name',['factor','alpha'])
def test_ic_prefix_ignores_future_prices_labels_and_universe(tmp_path, scorer_name):
    from qsim.features.factor import compute_factor_ic_scores_asof
    from qsim.features.alpha158 import compute_alpha158_ic_scores
    from qsim.engine.cross_sectional_runner import monthly_rebalance_dates
    db=tmp_path/'ic.db';days=list(pd.bdate_range('2021-01-01',periods=125).strftime('%Y%m%d'))
    codes=[f'{600001+j:06d}.SH' for j in range(60)]
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE stock_bars(ts_code TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,vol REAL,amount REAL,vwap REAL)')
        c.execute('CREATE TABLE stock_valuation(ts_code TEXT,date TEXT,pe_ttm REAL,pb REAL,dv_ratio REAL,total_mv REAL)')
        c.execute('CREATE TABLE stock_info(ts_code TEXT,name TEXT,industry TEXT,list_date TEXT)')
        c.execute('CREATE TABLE stock_universe_history(index_code TEXT,trade_date TEXT,ts_code TEXT)')
        for j,code in enumerate(codes):
            c.execute('INSERT INTO stock_info VALUES(?,?,?,?)',(code,'普通股','制造业','20100101'))
            c.execute('INSERT INTO stock_universe_history VALUES(?,?,?)',('000300.SH',days[0],code))
            for i,date in enumerate(days):
                p=10+j*.1+i*(j+1)*.001+(i%7)*.01
                c.execute('INSERT INTO stock_bars VALUES(?,?,?,?,?,?,?,?,?)',(code,date,p,p+.2,p-.2,p,10000+j*10+i,100000,p))
                c.execute('INSERT INTO stock_valuation VALUES(?,?,?,?,?,?)',(code,date,10+j*.1,1+j*.01,1+j*.1,10000+j*100))
    dates=monthly_rebalance_dates(days);cutoff=days[85]
    scorer=(lambda:compute_factor_ic_scores_asof(db,dates,as_of=cutoff)) if scorer_name=='factor' else (lambda:compute_alpha158_ic_scores(db,dates,as_of=cutoff))
    before=scorer()
    with sqlite3.connect(db) as c:
        c.execute('UPDATE stock_bars SET close=close*100,open=open*100,high=high*100,low=low*100 WHERE date>?',(cutoff,))
        c.execute('UPDATE stock_valuation SET pe_ttm=pe_ttm*100,pb=pb*100 WHERE date>?',(cutoff,))
        c.execute('INSERT INTO stock_universe_history VALUES(?,?,?)',('000300.SH',days[100],'999999.SH'))
    after=scorer()
    assert len(before)>=3 and set(before)==set(after)
    for date in before:
        pd.testing.assert_series_equal(before[date],after[date])


def test_legacy_curves_do_not_claim_strict_point_in_time_validation():
    from qsim.services.web import mobile
    page=mobile._backtest_curve_card({'key':'multi_factor','data':[{'date':'20220104','total_value':100000}]},0)
    assert '严格时点回测' not in page and '时点限制见上方筛选审查' in page
