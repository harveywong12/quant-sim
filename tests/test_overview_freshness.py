import datetime as dt
import json
import sqlite3
import inspect
from zoneinfo import ZoneInfo

import pytest

from qsim.services.web import qsim_data as q, mobile, performance
from qsim.services import realtime
from qsim.storage import paper
from qsim.strategies import migration

CN = ZoneInfo('Asia/Shanghai')
NOW = dt.datetime(2026, 10, 8, 17, 0, tzinfo=CN)

@pytest.fixture(autouse=True)
def baseline_clock_adapter(monkeypatch):
    # Let the same behavioral assertions run on the old implementation, which
    # never accepted or checked today's date. Do not turn API-shape TypeErrors
    # into evidence of a return-calculation defect.
    if 'now' not in inspect.signature(q.strategy_overview).parameters:
        original = q.strategy_overview
        monkeypatch.setattr(q, 'strategy_overview', lambda *, now=None: original())
    if 'now' not in inspect.signature(q.data_version).parameters:
        original_version = q.data_version
        monkeypatch.setattr(q, 'data_version', lambda *, now=None: original_version())

@pytest.fixture
def overview(monkeypatch, tmp_path):
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
    c.executescript('''
    CREATE TABLE qsim_snapshots(strategy TEXT,date TEXT,total_value REAL,cash REAL,position_value REAL);
    CREATE TABLE qsim_trades(strategy TEXT,date TEXT,fee REAL);
    CREATE TABLE bars(symbol TEXT,date TEXT,close REAL);
    ''')
    c.executemany('INSERT INTO qsim_snapshots VALUES(?,?,?,?,?)', [
        ('multi_factor_v3','20260929',10000,1000,9000),
        ('multi_factor_v3','20260930',11000,1000,10000)])
    calendar = {'valid_from':'2026-01-01','valid_through':'2026-12-31',
                'closed_dates':[f'2026-10-{i:02d}' for i in range(1,8)]}
    (tmp_path/'config').mkdir(); (tmp_path/'config/trading-calendar.json').write_text(json.dumps(calendar))
    monkeypatch.setattr(q,'ROOT',tmp_path)
    monkeypatch.setattr(q,'_paper_conn',lambda:c)
    monkeypatch.setattr(q,'paper_accounts',lambda:[{'strategy':'multi_factor_v3','initial_capital':10000,'cash':1000}])
    monkeypatch.setattr(q,'paper_positions_detail',lambda:[])
    monkeypatch.setattr(q,'_mark_positions',lambda *a:[{'strategy':'multi_factor_v3','ts_code':'600001.SH','shares':100,
        'price':110,'market_value':11000,'price_date':'20260930'}])
    monkeypatch.setattr(q,'paper_trades',lambda **kw:[])
    monkeypatch.setattr(q,'paper_realtime_prices',lambda:{})
    monkeypatch.setattr(q,'paper_realtime_status',lambda **kw:{'fresh':False,'today_data':False,'in_session':False,'updated_at':None})
    monkeypatch.setattr(q,'market_status',lambda:{'as_of':'20260930'})
    monkeypatch.setattr(q,'provisional_paper',lambda:None)
    monkeypatch.setattr(migration,'ACTIVE_MIGRATIONS',[])
    monkeypatch.setattr(paper,'PAPER_VERSIONED_CANDIDATES',{'multi_factor_v3':{'state':'paper_enabled'}})
    monkeypatch.setattr(performance,'activation_records',lambda *a:{})
    return c

def test_previous_session_profit_never_shown_as_today(overview):
    row = q.strategy_overview(now=NOW)[0]
    assert row['day_return'] is None
    assert row['day_return_status'] == 'awaiting_today_settlement'
    assert row['settled_period_return'] == pytest.approx(.10)
    assert row['settled_return_date'] == '20260930'

def test_settled_return_comes_from_snapshots_not_mixed_market_marks(overview):
    overview.execute("UPDATE qsim_snapshots SET date=CASE WHEN date='20260930' THEN '20261008' ELSE '20260930' END")
    row = q.strategy_overview(now=NOW)[0]
    assert row['day_return'] == pytest.approx(.10)
    assert row['day_return_status'] == 'settled'
    assert row['day_return_date'] == '20261008'
    assert row['total_value'] == 12000  # Separate, explicitly dated asset estimate.

def test_new_market_watermark_is_not_a_position_price_date(overview, monkeypatch):
    monkeypatch.setattr(q,'market_status',lambda:{'as_of':'20261008'})
    row = q.strategy_overview(now=NOW)[0]
    assert row['day_return'] is None and row['as_of'] == '20260930'

def test_intraday_estimate_requires_immediately_previous_settlement(overview, monkeypatch):
    monkeypatch.setattr(q,'paper_realtime_status',lambda **kw:{'fresh':True,'today_data':True,'in_session':True,'updated_at':'2026-10-08 10:00:00'})
    monkeypatch.setattr(q,'_mark_positions',lambda *a:[{'strategy':'multi_factor_v3','ts_code':'600001.SH','shares':100,'price':110,'market_value':11000,'price_date':'20261008','price_source':'realtime'}])
    row = q.strategy_overview(now=dt.datetime(2026,10,8,10,1,tzinfo=CN))[0]
    assert row['day_return'] == pytest.approx(12000/11000-1)
    assert row['day_return_status'] == 'intraday_estimate'
    overview.execute("UPDATE qsim_snapshots SET date='20260928' WHERE date='20260930'")
    row = q.strategy_overview(now=dt.datetime(2026,10,8,10,1,tzinfo=CN))[0]
    assert row['day_return'] is None and row['day_return_status'] == 'previous_settlement_missing'

def test_one_stale_holding_blocks_a_mixed_intraday_return(overview, monkeypatch):
    monkeypatch.setattr(q,'paper_realtime_status',lambda **kw:{'fresh':True,'today_data':True,'in_session':True,'updated_at':'2026-10-08 10:00:00'})
    row = q.strategy_overview(now=dt.datetime(2026,10,8,10,1,tzinfo=CN))[0]
    assert row['day_return'] is None and row['day_return_status'] == 'quotes_incomplete'

@pytest.mark.parametrize('value',[0,None,float('inf'),-100])
def test_invalid_or_missing_baseline_is_never_zero_or_infinity(overview,value):
    overview.execute('UPDATE qsim_snapshots SET total_value=? WHERE date=?',(value,'20260929'))
    row = q.strategy_overview(now=NOW)[0]
    assert row['settled_period_return'] is None and row['day_return'] is None
    assert row['performance']['status'] == 'invalid_settlement_data'

def test_missed_session_is_not_called_one_day_return(overview):
    overview.execute("UPDATE qsim_snapshots SET date='20261008' WHERE date='20260930'")
    overview.execute("UPDATE qsim_snapshots SET date='20260928' WHERE date='20260929'")
    row = q.strategy_overview(now=NOW)[0]
    assert row['day_return'] is None
    assert row['settled_return_from_date'] == '20260928'

def test_future_quotes_are_not_fresh(monkeypatch):
    monkeypatch.setattr(q,'paper_realtime_prices',lambda:{'600001':{'updated_at':'2026-10-08 11:00:00'}})
    monkeypatch.setattr(realtime,'is_trading_session',lambda *a:True)
    status = q.paper_realtime_status(now=dt.datetime(2026,10,8,10,0,tzinfo=CN))
    assert not status['fresh'] and not status['today_data']

def test_calendar_unavailable_does_not_label_a_period_as_daily(overview,tmp_path):
    (tmp_path/'config/trading-calendar.json').unlink()
    overview.execute("UPDATE qsim_snapshots SET date='20261008' WHERE date='20260930'")
    row = q.strategy_overview(now=NOW)[0]
    assert row['day_return'] is None and row['day_return_status'] == 'calendar_unavailable'

def test_render_explains_pending_and_exact_last_settlement(monkeypatch):
    monkeypatch.setattr(q,'strategy_overview',lambda:[{'strategy':'multi_factor_v3','display_name':'多因子 v3',
        'day_return':None,'day_return_status':'awaiting_today_settlement','day_return_date':None,
        'settled_date':'20260930','settled_period_return':-.02,'settled_return_date':'20260930',
        'settled_return_from_date':'20260929','as_of':'20260930','total_value':10000,'cash':10000,'positions':[],'trades':[]}])
    monkeypatch.setattr(q,'paper_realtime_status',lambda:{'fresh':False,'in_session':False,'updated_at':None})
    page = mobile._strategy_overview_card()
    assert '今日待结算' in page and '2026-09-30' in page and '-2.00%' in page
    assert '每 5 分钟更新' not in page
    assert '实时行情 5 分钟更新' not in mobile.PAGE
    assert '运行中' not in mobile.PAGE
    assert '数据日期见各模块' in mobile.PAGE

def test_dividend_components_prevent_incomplete_intraday_net_return(overview,monkeypatch):
    overview.execute("UPDATE qsim_snapshots SET total_value=total_value+100 WHERE date='20260930'")
    monkeypatch.setattr(q,'paper_realtime_status',lambda **kw:{'fresh':True,'today_data':True,'in_session':True,'updated_at':'2026-10-08 10:00:00'})
    monkeypatch.setattr(q,'_mark_positions',lambda *a:[{'strategy':'multi_factor_v3','ts_code':'600001.SH','shares':100,
        'price':110,'market_value':11000,'price_date':'20261008','price_source':'realtime'}])
    row=q.strategy_overview(now=dt.datetime(2026,10,8,10,1,tzinfo=CN))[0]
    assert row['day_return'] is None and row['day_return_status']=='valuation_components_missing'

@pytest.mark.parametrize('value',[None,float('nan'),float('inf'),-float('inf')])
def test_missing_and_nonfinite_format_as_unavailable(value):
    assert '>—<' in mobile._pct(value)

def test_zero_and_loss_keep_their_actual_values():
    assert '+0.00%' in mobile._pct(0)
    assert '-2.00%' in mobile._pct(-.02)

def test_version_changes_on_day_rollover_quote_expiry_and_failure_state(tmp_path,monkeypatch):
    (tmp_path/'data').mkdir();(tmp_path/'artifacts').mkdir()
    with sqlite3.connect(tmp_path/'data/quant.db') as c:
        c.executescript('''CREATE TABLE qsim_snapshots(date TEXT); CREATE TABLE qsim_trades(date TEXT);
        CREATE TABLE qsim_accounts(created_date TEXT); CREATE TABLE qsim_realtime(updated_at TEXT);
        CREATE TABLE qsim_data_sync_runs(id INTEGER);''')
        c.execute("INSERT INTO qsim_realtime VALUES('2026-10-08 10:00:00')")
    monkeypatch.setattr(q,'ROOT',tmp_path);monkeypatch.setattr(q,'ARTIFACTS',tmp_path/'artifacts')
    monkeypatch.setattr(q,'hot_zone_snapshot',lambda:{})
    monkeypatch.setattr(realtime,'is_trading_session',lambda *a:True)
    first=q.data_version(now=dt.datetime(2026,10,8,10,1,tzinfo=CN))
    assert first==q.data_version(now=dt.datetime(2026,10,8,10,2,tzinfo=CN))
    assert first!=q.data_version(now=dt.datetime(2026,10,8,10,11,tzinfo=CN))
    assert first!=q.data_version(now=dt.datetime(2026,10,9,10,1,tzinfo=CN))
    (tmp_path/'artifacts/data_reconcile_state.json').write_text('{"ok":false}')
    assert first!=q.data_version(now=dt.datetime(2026,10,8,10,1,tzinfo=CN))

def test_market_session_boundary_changes_version_without_new_data(tmp_path,monkeypatch):
    monkeypatch.setattr(q,'ROOT',tmp_path);monkeypatch.setattr(q,'ARTIFACTS',tmp_path)
    monkeypatch.setattr(q,'hot_zone_snapshot',lambda:{})
    monkeypatch.setattr(realtime,'is_trading_session',lambda now:now.hour<15)
    assert q.data_version(now=dt.datetime(2026,10,8,14,59,tzinfo=CN))!=q.data_version(now=dt.datetime(2026,10,8,15,1,tzinfo=CN))
