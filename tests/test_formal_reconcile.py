import datetime as dt
from zoneinfo import ZoneInfo
import pytest
from qsim.services import data_reconcile, native_market, market_quality
from qsim.services.api import app


@pytest.fixture
def pipeline(monkeypatch):
    monkeypatch.setenv('QSIM_SCHEDULER_ENABLED','0');monkeypatch.setenv('QSIM_FORMAL_PAPER','1');monkeypatch.setenv('QSIM_PAPER_AUTORUN','1')
    monkeypatch.setattr(data_reconcile.market,'_expected_latest_trade_date',lambda _: '2026-01-14')
    monkeypatch.setattr(native_market,'enabled',lambda:True)
    monkeypatch.setattr(native_market,'run',lambda:{'status':'up_to_date'})
    monkeypatch.setattr(market_quality,'check',lambda *a,**k:{'ok':True,'issues':[]})
    calls=[];monkeypatch.setattr(app,'_run_paper_job',lambda:calls.append('settled'))
    return calls


def test_formal_pipeline_rechecks_source_even_when_current_and_accepts_fresh_settlement(pipeline,monkeypatch):
    monkeypatch.setattr(data_reconcile,'_paper_fresh',lambda _:True)
    result=data_reconcile.reconcile(dt.datetime(2026,1,14,22,tzinfo=ZoneInfo('Asia/Shanghai')))
    assert result['ok'] is True and result['actions']==['paper']
    assert pipeline==['settled']


def test_formal_pipeline_keeps_freshness_gate_after_failed_to_catch_up(pipeline,monkeypatch):
    monkeypatch.setattr(data_reconcile,'_paper_fresh',lambda _:False)
    with pytest.raises(RuntimeError,match='remains stale'):
        data_reconcile.reconcile(dt.datetime(2026,1,14,22,tzinfo=ZoneInfo('Asia/Shanghai')))
    assert pipeline==['settled']


def test_collector_only_pipeline_never_runs_paper(pipeline,monkeypatch):
    monkeypatch.setenv('QSIM_PAPER_AUTORUN','0')
    result=data_reconcile.reconcile(dt.datetime(2026,1,14,22,tzinfo=ZoneInfo('Asia/Shanghai')))
    assert result['status']=='collected_paper_blocked'
    assert pipeline==[]
