from __future__ import annotations

import datetime as dt
import json
import sqlite3

from qsim.services import paper_orders
from qsim.storage.paper import get_conn, init_schema


def _seed_intent(db_path, model_hash: str, strategy: str = "multi_factor_v3") -> None:
    conn = get_conn(db_path)
    try:
        init_schema(conn)
        conn.execute(
            "INSERT INTO qsim_accounts "
            "(strategy,symbol,initial_capital,cash,shares,avg_cost,code_hash,status,created_date) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (strategy, None, 100000.0, 100000.0, 0, 0.0, "", "active", "20260901"),
        )
        candidates = [
            {"code": f"6000{index:02d}.SH", "score": 1.0 - index / 100, "rank": index}
            for index in range(1, 11)
        ]
        conn.execute(
            "INSERT INTO qsim_order_intents "
            "(strategy,signal_date,execution_date,status,allocation,desired_count,"
            "candidates_json,gate_json,source_market_as_of,model_hash,signal_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                strategy, "20260901", "20260902", "pending", 0.718066,
                10, json.dumps(candidates), "{}", "20260901", model_hash,
                "signal-hash", "2026-09-01T20:30:00+08:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_frozen_v3_order_executes_once_at_open(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    model_hash = paper_orders._model_hash()
    _seed_intent(db_path, model_hash)
    monkeypatch.setattr(paper_orders.realtime, "_is_trading_day", lambda _day: True)

    def quotes(codes: list[str]) -> dict[str, dict]:
        return {
            code[:6]: {
                "open_price": 10.0, "price": 10.0, "pre_close": 9.9,
                "quote_date": "2026-09-02", "quote_time": "09:30:01",
            }
            for code in codes
        }

    now = dt.datetime(2026, 9, 2, 9, 31, tzinfo=paper_orders.SHANGHAI_TZ)
    first = paper_orders.execute_v3_open(
        now=now, db_path=db_path, quote_loader=quotes,
    )
    second = paper_orders.execute_v3_open(
        now=now, db_path=db_path, quote_loader=quotes,
    )

    assert first["ok"] is True
    assert first["status"] == "executed"
    assert first["trade_count"] == 10
    assert second["skipped"] == "no-pending-intent"
    conn = sqlite3.connect(db_path)
    try:
        status, evidence = conn.execute(
            "SELECT status,execution_json FROM qsim_order_intents"
        ).fetchone()
        positions = conn.execute("SELECT COUNT(*) FROM qsim_positions").fetchone()[0]
        trades = conn.execute("SELECT COUNT(*) FROM qsim_trades").fetchone()[0]
    finally:
        conn.close()
    assert status == "executed"
    assert json.loads(evidence)["signal_date"] == "20260901"
    assert positions == 10
    assert trades == 10


def test_v3_order_fails_closed_outside_open_window(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    _seed_intent(db_path, paper_orders._model_hash())
    monkeypatch.setattr(paper_orders.realtime, "_is_trading_day", lambda _day: True)
    now = dt.datetime(2026, 9, 2, 9, 29, tzinfo=paper_orders.SHANGHAI_TZ)

    result = paper_orders.execute_v3_open(now=now, db_path=db_path, quote_loader=lambda _: {})

    assert result["skipped"] == "outside-open-window"


def test_v3_order_rejects_stale_opening_quotes(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    _seed_intent(db_path, paper_orders._model_hash())
    monkeypatch.setattr(paper_orders.realtime, "_is_trading_day", lambda _day: True)
    stale = {
        f"6000{index:02d}": {
            "open_price": 10.0, "price": 10.0, "pre_close": 9.9,
            "quote_date": "2026-09-01", "quote_time": "15:00:00",
        }
        for index in range(1, 11)
    }
    now = dt.datetime(2026, 9, 2, 9, 31, tzinfo=paper_orders.SHANGHAI_TZ)

    result = paper_orders.execute_v3_open(
        now=now, db_path=db_path, quote_loader=lambda _codes: stale,
    )

    assert result["ok"] is False
    assert result["status"] == "pending"
    assert result["error"] == "no executable affordable candidates"


def test_v4_freeze_excludes_star_before_candidate_evidence(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    # Small frozen score panel: this proves the eligibility constraint enters
    # before the immutable candidate evidence is stored.
    monkeypatch.setattr(paper_orders, "_dates", lambda _conn, _as_of: ["20260901"])
    monkeypatch.setitem(paper_orders.STRATEGY_SPECS["multi_factor_v4"], "paper_start_date", "20260901")
    monkeypatch.setattr(paper_orders, "monthly_rebalance_dates", lambda _days: ["20260901"])
    monkeypatch.setattr(paper_orders, "compute_factor_ic_scores_asof", lambda *_args, **_kwargs: {
        "20260901": __import__("pandas").Series({"688001.SH": 3.0, "600001.SH": 2.0, "000001.SZ": 1.0})
    })
    monkeypatch.setattr(paper_orders, "multi_factor_v3_allocations", lambda *_args, **_kwargs: ({"20260901": 1.0}, [{}]))
    monkeypatch.setattr(paper_orders.realtime, "next_trading_day", lambda _day: dt.date(2026, 9, 2))
    conn = get_conn(db_path)
    try:
        init_schema(conn)
        conn.execute("CREATE TABLE stock_bars(date TEXT)")
        conn.execute("INSERT INTO stock_bars VALUES ('20260901')")
        conn.commit()
    finally:
        conn.close()
    result = paper_orders.freeze_v4_intent(as_of="20260901", db_path=db_path)
    assert result["ok"] is True
    assert result["candidate_count"] == 2
    conn = sqlite3.connect(db_path)
    try:
        raw = conn.execute("SELECT candidates_json FROM qsim_order_intents WHERE strategy='multi_factor_v4'").fetchone()[0]
    finally:
        conn.close()
    assert [item["code"] for item in json.loads(raw)] == ["600001.SH", "000001.SZ"]


def test_open_fill_does_not_claim_close_settlement(monkeypatch,tmp_path):
    """Synthetic open != close: the EOD freshness gate must still see no NAV."""
    from qsim.services import data_reconcile
    path=tmp_path/'data'/'quant.db';path.parent.mkdir()
    _seed_intent(path,paper_orders._model_hash())
    monkeypatch.setattr(paper_orders.realtime,'_is_trading_day',lambda _:True)
    quote=lambda codes:{c[:6]:{'open_price':10.,'pre_close':9.9,'quote_date':'20260902','quote_time':'09:30:01'} for c in codes}
    assert paper_orders.execute_v3_open(now=dt.datetime(2026,9,2,9,31,tzinfo=paper_orders.SHANGHAI_TZ),db_path=path,quote_loader=quote)['status']=='executed'
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT COUNT(*) FROM qsim_snapshots').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM qsim_trades').fetchone()[0]==10
    monkeypatch.setattr(data_reconcile,'ROOT',tmp_path)
    from qsim.storage import paper
    monkeypatch.setattr(paper,'PAPER_STRATEGIES',{})
    monkeypatch.setattr(paper,'PAPER_VERSIONED_CANDIDATES',{'multi_factor_v3':{'backfill':True}})
    assert not data_reconcile._paper_fresh('20260902')
