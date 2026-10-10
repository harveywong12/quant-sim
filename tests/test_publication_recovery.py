"""Actual storage/publisher paths, synthetic SQLite files and child crashes."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from qsim.services import native_market as native, market_quality
from qsim.storage import paper, publication
from tests.test_native_market import market_root, Provider


def test_paper_then_collector_keeps_a_publishable_database(market_root):
    conn = paper.get_conn(market_root/'data/quant.db')
    assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    assert conn.execute('PRAGMA synchronous').fetchone()[0] == 3
    conn.close()
    result = native.run(root=market_root, pro=Provider())
    assert publication.committed_result(market_root)['run_id'] == result['run_id']
    assert market_quality.check('20260930', market_root, require_publication=True)['ok']


def test_existing_wal_is_rejected_not_silently_converted(tmp_path):
    path = tmp_path/'quant.db'
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE preserve(value)')
        conn.execute('INSERT INTO preserve VALUES(42)')
    with pytest.raises(RuntimeError, match='WAL'):
        paper.get_conn(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert conn.execute('SELECT value FROM preserve').fetchone() == (42,)


@pytest.mark.parametrize('mode', ['OFF', 'MEMORY', 'WAL'])
def test_unsafe_journals_are_rejected_before_data_transaction(tmp_path, mode):
    with sqlite3.connect(tmp_path/'quant.db') as conn:
        conn.execute('PRAGMA journal_mode='+mode)
        with pytest.raises(RuntimeError, match=mode):
            publication.require_rollback(conn)
        assert not conn.in_transaction


def test_memory_database_cannot_claim_disk_publication():
    with sqlite3.connect(':memory:') as conn:
        with pytest.raises(RuntimeError, match='on-disk'):
            publication.require_rollback(conn)


def test_committed_run_survives_cache_failure_and_retry(market_root, monkeypatch):
    def cache_unavailable(*args):
        raise OSError('synthetic cache failure')
    monkeypatch.setattr(native, '_state', cache_unavailable)
    result = native.run(root=market_root, pro=Provider())
    assert result['ok'] and result['state_cache_ok'] is False
    assert publication.committed_result(market_root)['ok']
    retry = native.run(root=market_root, pro=object())
    assert retry['status'] == 'up_to_date' and retry['run_id'] == result['run_id']
    with sqlite3.connect(market_root/'data/quant.db') as conn:
        assert conn.execute('SELECT COUNT(*) FROM qsim_native_ingest_runs').fetchone() == (1,)
        assert conn.execute('SELECT status FROM qsim_native_ingest_runs').fetchone() == ('success',)


def test_post_commit_interruption_cannot_relabel_success(market_root, monkeypatch):
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(native, '_state', interrupt)
    with pytest.raises(KeyboardInterrupt):
        native.run(root=market_root, pro=Provider())
    assert publication.committed_result(market_root)['ok']


def test_mismatched_pair_rejected_even_with_success_file_cache(market_root):
    native.run(root=market_root, pro=Provider())
    with sqlite3.connect(market_root/'data/v2_market.db') as conn:
        conn.execute("UPDATE qsim_market_publication SET run_id='different-synthetic-version'")
    assert publication.committed_result(market_root) is None
    quality = market_quality.check('20260930', market_root, require_publication=True)
    assert not quality['ok'] and quality['details']['publication_consistent'] is False


def test_missing_pair_marker_cannot_pass_native_quality(market_root):
    native.run(root=market_root, pro=Provider())
    with sqlite3.connect(market_root/'data/v2_market.db') as conn:
        conn.execute('DROP TABLE qsim_market_publication')
    assert not market_quality.check('20260930', market_root, require_publication=True)['ok']


@pytest.mark.parametrize('stop', ['main_written', 'both_written', 'before_commit', 'after_commit'])
def test_process_death_recovers_both_payloads_and_version_markers(tmp_path, stop):
    q, v = tmp_path/'quant.db', tmp_path/'v2.db'
    for path in (q, v):
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE payload(id INTEGER PRIMARY KEY,value TEXT)')
            conn.executemany('INSERT INTO payload VALUES(?,?)', [(i, 'old'*700) for i in range(80)])
    with sqlite3.connect(q) as conn:
        conn.execute('ATTACH DATABASE ? AS v2', (str(v),))
        publication.require_rollback(conn, ('main', 'v2'))
        conn.execute('BEGIN IMMEDIATE')
        publication.mark(conn, 'old-version', '20260929')
    program = '''
import os,sqlite3,sys
from qsim.storage.publication import require_rollback,mark
conn=sqlite3.connect(sys.argv[1])
conn.execute('ATTACH DATABASE ? AS v2',(sys.argv[2],))
require_rollback(conn,('main','v2'))
conn.execute('PRAGMA main.cache_size=5')
conn.execute('PRAGMA v2.cache_size=5')
conn.execute('BEGIN IMMEDIATE')
conn.execute("UPDATE main.payload SET value=?",('new'*700,))
if sys.argv[3]=='main_written': os._exit(71)
conn.execute("UPDATE v2.payload SET value=?",('new'*700,))
if sys.argv[3]=='both_written': os._exit(71)
mark(conn,'new-version','20260930')
if sys.argv[3]=='before_commit': os._exit(71)
conn.commit()
os._exit(71)
'''
    # NAS disk spill with this deliberately tiny cache was measured at 25.5s
    # for two writes. This test certifies recovery, not a 15s latency target.
    # Keep EXTRA, cache_size=5, payload size and every recovery assertion.
    result = subprocess.run([sys.executable, '-c', program, str(q), str(v), stop],
                            capture_output=True, timeout=60)
    assert result.returncode == 71, result.stderr.decode()
    expected = 'new' if stop == 'after_commit' else 'old'
    for path in (q, v):
        # Normal SQLite open performs recovery of hot rollback journals.
        with sqlite3.connect(path) as conn:
            assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            assert {row[0] for row in conn.execute('SELECT value FROM payload')} == {expected*700}
            assert publication.marker(conn)[0] == expected+'-version'
