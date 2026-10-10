"""Synthetic account drill-down contract; never open real databases."""
from html.parser import HTMLParser
import pytest
from qsim.services.web import mobile, qsim_data as q

class Contract(HTMLParser):
    def __init__(self,page):
        super().__init__();self.buttons=[];self.details={};self.data=[];self.feed(page)
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='button' and attrs.get('data-qsim-detail-target'):self.buttons.append(attrs)
        if tag=='details' and attrs.get('id'):self.details[attrs['id']]=attrs
    def handle_data(self,text):self.data.append(text)

@pytest.fixture
def render(monkeypatch):
    monkeypatch.setattr(q,'paper_realtime_status',lambda:{'fresh':False,'in_session':False})
    def page(rows):
        monkeypatch.setattr(q,'strategy_overview',lambda:rows)
        return Contract(mobile._strategy_overview_card())
    return page

def account(strategy='multi_factor_v3',ledger='official',**kw):
    return dict(strategy=strategy,ledger=ledger,display_name=kw.pop('display_name','合成策略'),
                positions=kw.pop('positions',[]),trades=[],cash=kw.pop('cash',0),**kw)

def test_holding_button_controls_actual_inline_detail(render):
    page=render([account(holdings_count=1)])
    assert len(page.buttons)==1
    button=page.buttons[0]
    assert button['type']=='button' and button['aria-expanded']=='false'
    assert button['aria-controls']==button['data-qsim-detail-target']
    assert page.details[button['aria-controls']]['data-qsim-account-detail']=='1'

def test_account_identity_survives_reorder_and_display_name_change(render):
    first=render([account('A'),account('B')])
    second=render([account('B'),account('A',display_name='改名后的合成策略')])
    assert first.buttons[0]['aria-controls']==second.buttons[1]['aria-controls']
    assert first.buttons[1]['aria-controls']==second.buttons[0]['aria-controls']

def test_same_strategy_in_two_ledgers_has_distinct_controls(render):
    page=render([account(ledger='official'),account(ledger='candidate')])
    assert len(page.details)==2
    assert page.buttons[0]['aria-controls']!=page.buttons[1]['aria-controls']

def test_empty_holding_account_still_opens_honest_empty_detail(render):
    page=render([account(holdings_count=0)])
    assert len(page.buttons)==1
    assert '当前无持仓' in ''.join(page.data) and '暂无成交' in ''.join(page.data)
    assert '查看持仓 · 0 只' in ''.join(page.data)

@pytest.mark.parametrize('name',['合成策略\" onclick=\"alert(1)','<script>alert(1)</script>'])
def test_account_names_are_escaped_in_accessible_control(render,name):
    page=render([account(display_name=name)])
    assert page.buttons[0]['aria-label']=='查看'+name+'的持仓与成交'
    assert 'onclick' not in page.buttons[0]

def test_inline_details_preserve_source_units_dates_and_zero_values(render):
    page=render([account(holdings_count=1,positions=[dict(ts_code='600001.SH',shares=123400,
          avg_cost=0,price=10.123,market_value=1249178.2,price_date='20261008')])])
    text=''.join(page.data)
    assert '123,400 股' in text and '成本 0.000' in text and '现价 10.123' in text
    assert '市值 1,249,178' in text and '2026-10-08' in text and '现金 0 元' in text

def test_missing_prices_are_not_rendered_as_zero(render):
    page=render([account(holdings_count=1,positions=[dict(ts_code='600001.SH',shares=100,
          avg_cost=9,price=None,market_value=None,price_date=None)])])
    text=''.join(page.data)
    assert '现价 —' in text and '市值 —' in text and '现价 0' not in text
