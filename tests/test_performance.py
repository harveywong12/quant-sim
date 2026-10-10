import sqlite3

import pytest

from qsim.services.web.performance import curve_metrics, evaluate
from qsim.services.web import mobile, qsim_data


def ledger():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE bars(symbol TEXT,date TEXT,close REAL)')
    c.executemany('INSERT INTO bars VALUES(?,?,?)', [('513100','20260901',10),('513100','20260902',11)])
    return c


def snapshots():
    return [{'date':'20260901','total_value':99900,'position_value':49900},
            {'date':'20260902','total_value':105000,'position_value':55000}]


def test_identical_dates_initial_capital_and_all_fees():
    p = evaluate(ledger(), {'strategy':'tsmom_vol','initial_capital':100000},
                 snapshots(), [{'fee':5},{'fee':8}], '20260902')
    assert p['total_return'] == pytest.approx(.05)
    assert p['benchmark_return'] == pytest.approx(.10)
    assert p['excess_return'] == pytest.approx(-.05)
    assert p['max_drawdown'] == pytest.approx(-.001)
    assert p['trade_count'] == 2 and p['fees'] == 13
    assert p['first_date'] == '20260901' and p['last_date'] == '20260902'
    assert p['benchmark_curve'][-1]['total_value'] == pytest.approx(110000)


def test_missing_benchmark_does_not_fabricate_excess():
    c = ledger()
    c.execute("DELETE FROM bars WHERE date='20260902'")
    p = evaluate(c, {'strategy':'tsmom_vol'}, snapshots(), [], '20260902')
    assert p['benchmark_return'] is None and p['excess_return'] is None
    assert p['benchmark_curve'] == []
    assert p['activity'] == '尚未成交'


def test_stale_single_point_is_not_live_performance():
    p = evaluate(ledger(), {'strategy':'multi_factor_v4','state':'paper_candidate'},
                 snapshots()[:1], [{'fee':5}], '20260930')
    assert p['stale']
    assert p['sharpe'] is None and p['benchmark_return'] is None
    assert '结算截至' in p['warnings'][0]
    assert '尚无完整证据' in p['observation_note']
    assert curve_metrics([],100000)['total_return'] is None


def test_curve_render_flags_candidates_and_includes_benchmark():
    item = {'strategy':'multi_factor_v4','state':'paper_candidate','ledger':'official',
            'snapshots':snapshots(),'initial_capital':100000,
            'metrics':curve_metrics(snapshots(),100000),
            'performance':evaluate(ledger(), {'strategy':'tsmom_vol'},snapshots(),[], '20260930')}
    page = mobile._strategy_curve_card(item,'#abcdef',0)
    assert '候选 · 未提升' in page
    assert '结算落后' not in page  # warning gives exact dates instead
    assert '结算截至' in page and '同期间基准' in page
    assert '非回测' not in page and '含回填' in page


def test_intraday_marks_never_extend_settled_curve(monkeypatch):
    row = {'strategy':'x','ledger':'official','initial_capital':100000,'positions':[],
           'performance':{'last_date':'20260902'}}
    monkeypatch.setattr(qsim_data,'strategy_overview',lambda:[row])
    monkeypatch.setattr(qsim_data,'provisional_paper',lambda:None)
    monkeypatch.setattr(qsim_data,'paper_snapshots',lambda n:snapshots())
    monkeypatch.setattr(qsim_data,'paper_realtime_status',lambda:{'fresh':True,'updated_at':'2026-09-03 10:00'})
    monkeypatch.setattr(qsim_data,'paper_realtime_prices',lambda:{})
    monkeypatch.setattr(qsim_data,'_mark_positions',lambda *a:[])
    monkeypatch.setattr(qsim_data,'paper_realtime_nav_history',lambda *a:pytest.fail('must not append realtime estimates'))
    result = qsim_data.strategy_visuals()[0]
    assert len(result['snapshots']) == 2
    assert result['metrics']['total_return'] == pytest.approx(.05)


def test_marks_use_exact_exchange_and_latest_price(monkeypatch):
    c=sqlite3.connect(':memory:'); c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE bars(symbol TEXT,date TEXT,close REAL)')
    c.execute('CREATE TABLE stock_bars(ts_code TEXT,date TEXT,close REAL,PRIMARY KEY(ts_code,date))')
    c.executemany('INSERT INTO stock_bars VALUES(?,?,?)',
        [('000001.SZ','20260929',10),('000001.SZ','20260930',11),('000001.SH','20260930',99)])
    monkeypatch.setattr(qsim_data,'_paper_conn',lambda:c)
    p=qsim_data._mark_positions([{'ts_code':'000001.SZ','shares':100}])[0]
    assert p['price']==11 and p['price_date']=='20260930'
    assert p['market_value']==1100
