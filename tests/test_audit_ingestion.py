"""Synthetic ingestion and storage contract failures."""
import datetime as dt
import sqlite3
import pandas as pd
import pytest
from qsim.market.data_source import SqliteDataSource
from qsim.market.tushare_source import TushareDataSource
from qsim.services import native_market


def test_sqlite_ranges_handle_iso_dates(tmp_path):
    p=tmp_path/'market.db'
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE bars(symbol TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL)')
        c.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?)',[('510300',d,10,10,10,10,100) for d in ('2026-09-01','20260902','2026-09-03')])
    frame=SqliteDataSource(p).get_bars('510300','20260901','20260903')
    assert list(frame.date)==[dt.date(2026,9,1),dt.date(2026,9,2),dt.date(2026,9,3)]


def test_tushare_adapter_sorts_and_converts_volume(monkeypatch):
    import tushare
    class Pro:
        def query(self,name,**params):
            return pd.DataFrame([{'ts_code':'600001.SH','trade_date':d,'open':10,'high':11,'low':9,'close':10,'vol':2,'amount':3} for d in ('20260902','20260901')])
        def daily(self,**params):return self.query('daily',**params)
    from qsim.market import secure_tushare
    monkeypatch.setattr(secure_tushare,'make_client',lambda _:Pro())
    df=TushareDataSource('synthetic-token').get_bars('600001.SH','20260901','20260902')
    assert df.date.is_monotonic_increasing
    assert df.volume.tolist()==[200,200] and df.amount.tolist()==[3000,3000]


def test_repeated_provider_page_fails_closed(monkeypatch):
    monkeypatch.setattr(native_market.time,'sleep',lambda _:None)
    class Pro:
        def query(self,*args,**kwargs):return pd.DataFrame({'ts_code':['600001.SH']*3000,'trade_date':['20260901']*3000})
    with pytest.raises(RuntimeError,match='pagination|duplicate'):
        native_market.query(Pro(),'daily',trade_date='20260901')


def test_stock_coverage_requires_actual_universe_members(tmp_path):
    from qsim.services.data_reconcile import _source_stock_coverage
    p=tmp_path/'market.db'
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE stock_bars(ts_code TEXT,date TEXT)')
        c.execute('CREATE TABLE stock_universe_history(ts_code TEXT,trade_date TEXT)')
        c.executemany('INSERT INTO stock_universe_history VALUES(?,?)',[(f'A{i}','20260901') for i in range(100)])
        c.executemany('INSERT INTO stock_bars VALUES(?,?)',[(f'B{i}','20260901') for i in range(100)])
    assert not _source_stock_coverage(p,'2026-09-01')


def test_transport_rejects_http_before_network():
    from qsim.market.secure_tushare import HTTPSClient
    with pytest.raises(ValueError,match='HTTPS'):HTTPSClient('synthetic-token','http://example.invalid/dataapi')


def test_transport_verifies_tls_and_never_follows_redirect(monkeypatch):
    from qsim.market import secure_tushare as secure
    calls=[]
    class Response:
        status_code=200
        def json(self):return {'code':0,'data':{'fields':['ts_code'],'items':[['SYNTHETIC']]}}
    def post(url,**kwargs):calls.append((url,kwargs));return Response()
    monkeypatch.setattr(secure.requests,'post',post)
    result=secure.HTTPSClient('synthetic-token','https://example.invalid/dataapi').query('daily')
    assert result.ts_code.tolist()==['SYNTHETIC']
    assert calls[0][1]['verify'] is True and calls[0][1]['allow_redirects'] is False


def test_provider_rejection_never_becomes_success_empty_frame(monkeypatch):
    from qsim.market import secure_tushare as secure
    class Response:
        status_code=200
        def json(self):return {'code':-2001,'msg':'permission denied synthetic-token'}
    monkeypatch.setattr(secure.requests,'post',lambda *a,**kw:Response())
    with pytest.raises(secure.ProviderError) as e:secure.HTTPSClient('synthetic-token','https://example.invalid/dataapi').query('daily')
    assert 'synthetic-token' not in str(e.value)


def test_legacy_failure_does_not_return_success_or_repeat_post(monkeypatch):
    import requests
    from qsim.services import legacy_trigger
    monkeypatch.setattr(legacy_trigger,'_auth_token',lambda:'synthetic-token')
    monkeypatch.setattr(legacy_trigger,'_trigger_urls',lambda:['http://example.invalid/one','http://example.invalid/two'])
    calls=[]
    class Response:
        def raise_for_status(self):raise requests.HTTPError('500')
        def json(self):return {'ok':True}
    monkeypatch.setattr(legacy_trigger.requests,'post',lambda *a,**kw:calls.append(a) or Response())
    with pytest.raises(RuntimeError,match='failed'):legacy_trigger.call('run-daily')
    assert len(calls)==1


def test_real_validation_blocks_synthetic_provenance(tmp_path):
    import json
    from qsim.research.real_validation import validate
    contract=tmp_path/'contract.json';contract.write_text(json.dumps({'synthetic':True,'source':'Tushare'}))
    with pytest.raises(ValueError,match='real mode'):validate(tmp_path/'absent.db',contract,tmp_path/'result','dual_ma','20161001','20260930')
    assert not (tmp_path/'result').exists()


def test_local_validation_never_claims_performance_acceptance(tmp_path):
    """Synthetic harness exercises the real-mode gate; metrics are not evidence."""
    import json
    from qsim.research.real_validation import validate
    db=tmp_path/'synthetic.db'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE bars(symbol TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL)')
        c.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?)',[('510300',d,10,10,10,10,1000) for d in ('20260901','20260902')])
    contract=tmp_path/'contract.json';contract.write_text(json.dumps({'synthetic':False,'source':'Tushare','volume_unit':'lots','amount_unit':'thousand_cny','expected_sessions':['20260901','20260902']}))
    report=validate(db,contract,tmp_path/'result','dual_ma','20260901','20260902')
    assert report['goal_status']=='NOT_VERIFIED'
    assert len(report['database_sha256'])==64 and report['code_sha256']
    assert report['execution_policy']['volume_participation']==.05
    assert report['metrics']['sharpe'] is None
    assert (tmp_path/'result'/'nav.csv').stat().st_mode & 0o777 == 0o600


def test_restore_cannot_read_through_parent_symlink(tmp_path):
    import json,hashlib
    from qsim.services.recovery import restore
    outside=tmp_path/'outside';outside.mkdir();(outside/'payload.txt').write_text('synthetic protected data')
    bundle=tmp_path/'bundle';bundle.mkdir();(bundle/'nested').symlink_to(outside,target_is_directory=True)
    (bundle/'manifest.json').write_text(json.dumps({'kind':'qsim-recovery','files':{'nested/payload.txt':{'kind':'file','sha256':hashlib.sha256((outside/'payload.txt').read_bytes()).hexdigest(),'mode':0o600}}}))
    with pytest.raises(RuntimeError,match='unsafe|symlink'):restore(bundle,tmp_path/'restored')
    assert not (tmp_path/'restored').exists()
