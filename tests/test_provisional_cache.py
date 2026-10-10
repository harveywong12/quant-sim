import json
import sqlite3

from qsim.services import provisional_paper as job


def test_provisional_cache_reuses_unchanged_input_and_reacts_to_correction(tmp_path, monkeypatch):
    db=tmp_path/'market.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE bars(symbol TEXT,date TEXT,close REAL)')
        conn.execute("INSERT INTO bars VALUES('513100','20260930',10)")
    monkeypatch.setenv('QSIM_MUTATION_LOCK',str(tmp_path/'lock'))
    monkeypatch.setattr(job,'ROOT',tmp_path)
    monkeypatch.setattr(job,'LATEST',tmp_path/'latest.json')
    monkeypatch.setattr(job,'_market_conn',lambda: sqlite3.connect(db))
    monkeypatch.setattr(job,'paper_enabled',lambda _: True)
    monkeypatch.setattr(job,'_serialize',lambda *args: [])
    monkeypatch.setattr(job,'_write',lambda payload: job.LATEST.write_text(json.dumps(payload)) or job.LATEST)
    result=job.run_provisional_paper()
    assert result['input_fingerprint']
    monkeypatch.setattr(job,'_ledger',lambda: (_ for _ in ()).throw(RuntimeError('must recompute')))
    assert job.run_provisional_paper()['status']=='up_to_date'
    # With no pending strategies no market rows are consumed; change configuration.
    (tmp_path/'config').mkdir()
    (tmp_path/'config/strategy.json').write_text('{"changed":true}')
    import pytest
    with pytest.raises(RuntimeError,match='must recompute'):
        job.run_provisional_paper()


def test_fingerprint_detects_same_day_price_revision(tmp_path, monkeypatch):
    conn=sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE bars(symbol TEXT,date TEXT,close REAL)')
    conn.execute("INSERT INTO bars VALUES('513100','20260930',10)")
    monkeypatch.setattr(job,'ROOT',tmp_path)
    before=job._generation_fingerprint(conn,['tsmom_vol'],'20260801','20260930')
    conn.execute('UPDATE bars SET close=11')
    assert job._generation_fingerprint(conn,['tsmom_vol'],'20260801','20260930')!=before
