import sqlite3

import pytest

from qsim.services import paper_daily as paper
from qsim.storage.paper import get_conn, init_schema, seed_accounts


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    db = tmp_path / "quant.db"
    monkeypatch.setenv("QSIM_MUTATION_LOCK", str(tmp_path / "mutation.lock"))
    conn = get_conn(db)
    init_schema(conn)
    seed_accounts(conn)
    conn.execute("CREATE TABLE bars(symbol TEXT,date TEXT)")
    conn.execute("INSERT INTO bars VALUES('513100','20260930')")
    conn.execute("INSERT INTO qsim_snapshots(date,strategy,total_value,cash,position_value) "
                 "VALUES('20260929','tsmom_vol',123456,123456,0)")
    conn.execute("INSERT INTO qsim_snapshots(date,strategy,total_value,cash,position_value) "
                 "VALUES('20260826','dual_ma',100001,100001,0)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(paper, "QUANT_DB", db)
    monkeypatch.setattr(paper, "get_conn", lambda: get_conn(db))
    monkeypatch.setattr(paper, "init_schema", lambda: None)
    monkeypatch.setattr(paper, "paper_enabled", lambda name: name == "tsmom_vol")
    monkeypatch.setattr(paper, "PAPER_VERSIONED_CANDIDATES", {})
    return db


def snapshots(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT date,strategy,total_value FROM qsim_snapshots ORDER BY strategy,date").fetchall()


def staged_result(stage, start, end, only, **kwargs):
    stage.execute("DELETE FROM qsim_snapshots WHERE strategy='tsmom_vol'")
    stage.execute("INSERT INTO qsim_snapshots(date,strategy,total_value,cash,position_value) "
                  "VALUES('20260929','tsmom_vol',123456,123456,0)")
    stage.execute("INSERT INTO qsim_snapshots(date,strategy,total_value,cash,position_value) "
                  "VALUES('20260930','tsmom_vol',125000,125000,0)")
    return {"strategies": 1}


def test_calculation_failure_keeps_live_ledger(ledger, monkeypatch):
    before = snapshots(ledger)
    def fail(stage, *args, **kwargs):
        stage.execute("DELETE FROM qsim_snapshots")
        assert snapshots(ledger) == before
        raise RuntimeError("injected calculation failure")
    monkeypatch.setattr(paper, "_rebuild_paper", fail)
    with pytest.raises(RuntimeError, match="injected"):
        paper.run_paper(end="20260930", only=("tsmom_vol",))
    assert snapshots(ledger) == before


def test_atomic_publish_keeps_other_strategies(ledger, monkeypatch):
    before = snapshots(ledger)
    def rebuild(stage, *args, **kwargs):
        result = staged_result(stage, *args)
        assert snapshots(ledger) == before
        return result
    monkeypatch.setattr(paper, "_rebuild_paper", rebuild)
    paper.run_paper(end="20260930", only=("tsmom_vol",))
    assert snapshots(ledger) == [('20260826', 'dual_ma', 100001.0),
                                 ('20260929', 'tsmom_vol', 123456.0),
                                 ('20260930', 'tsmom_vol', 125000.0)]


def test_publish_failure_rolls_back_all_deletions(ledger, monkeypatch):
    before = snapshots(ledger)
    monkeypatch.setattr(paper, "_rebuild_paper", staged_result)
    monkeypatch.setattr(paper, "_copy_rows", lambda *args: (_ for _ in ()).throw(RuntimeError("publish failed")))
    with pytest.raises(RuntimeError, match="publish failed"):
        paper.run_paper(end="20260930", only=("tsmom_vol",))
    assert snapshots(ledger) == before


def test_incomplete_calculation_cannot_replace_ledger(ledger, monkeypatch):
    before = snapshots(ledger)
    def incomplete(stage, *args, **kwargs):
        stage.execute("DELETE FROM qsim_snapshots WHERE strategy='tsmom_vol'")
        return {"strategies": 0}
    monkeypatch.setattr(paper, "_rebuild_paper", incomplete)
    with pytest.raises(RuntimeError, match="incomplete"):
        paper.run_paper(end="20260930", only=("tsmom_vol",))
    assert snapshots(ledger) == before


def test_changed_history_cannot_rewrite_official_ledger(ledger, monkeypatch):
    before = snapshots(ledger)
    def corrected(stage, *args, **kwargs):
        stats = staged_result(stage, *args)
        stage.execute("UPDATE qsim_snapshots SET total_value=99999,cash=99999 WHERE date='20260929'")
        return stats
    monkeypatch.setattr(paper, "_rebuild_paper", corrected)
    with pytest.raises(RuntimeError, match="历史"):
        paper.run_paper(end="20260930", only=("tsmom_vol",))
    assert snapshots(ledger) == before


def test_repeated_settlement_is_noop_and_keeps_row_ids(ledger, monkeypatch):
    monkeypatch.setattr(paper, "_rebuild_paper", staged_result)
    paper.run_paper(end="20260930", only=("tsmom_vol",))
    with sqlite3.connect(ledger) as conn:
        before = conn.execute("SELECT * FROM qsim_snapshots").fetchall()
    monkeypatch.setattr(paper, "_rebuild_paper", lambda *args, **kwargs: 1/0)
    assert paper.run_paper(end="20260930", only=("tsmom_vol",))["status"] == "up_to_date"
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT * FROM qsim_snapshots").fetchall() == before


def test_isolated_replay_reports_changes_and_preserves_official(ledger, monkeypatch):
    from qsim.services import historical_replay
    before = snapshots(ledger)
    def corrected(stage, *args, **kwargs):
        stats = staged_result(stage, *args)
        stage.execute("UPDATE qsim_snapshots SET total_value=99999,cash=99999 WHERE date='20260929'")
        return stats
    monkeypatch.setattr(paper, "_rebuild_paper", corrected)
    result = historical_replay.run(end="20260930", only=("tsmom_vol",))
    assert result["differences"]
    assert result["mode"] == "isolated_historical_replay"
    assert snapshots(ledger) == before


def test_already_executed_open_fill_is_not_duplicated(ledger, monkeypatch):
    with sqlite3.connect(ledger) as conn:
        conn.execute("INSERT INTO qsim_trades(date,strategy,symbol,side,price,shares,amount,fee) VALUES('20260930','tsmom_vol','513100','BUY',10,100,1000,1)")
    def with_fill(stage, *args, **kwargs):
        result = staged_result(stage, *args)
        stage.execute("INSERT INTO qsim_trades(date,strategy,symbol,side,price,shares,amount,fee) VALUES('20260930','tsmom_vol','513100','BUY',10,100,1000,1)")
        return result
    monkeypatch.setattr(paper, '_rebuild_paper', with_fill)
    result = paper.run_paper(end='20260930', only=('tsmom_vol',))
    assert result['appended']['trades'] == 0
    with sqlite3.connect(ledger) as conn:
        assert conn.execute('SELECT COUNT(*) FROM qsim_trades').fetchone()[0] == 1
