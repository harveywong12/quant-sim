import datetime as dt
import json
import sqlite3

from qsim.services import operations as ops


def test_calendar_expiry_and_advance_warning(tmp_path, monkeypatch):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({"valid_from": "2026-01-01", "valid_through": "2026-12-31", "closed_dates": []}))
    monkeypatch.setattr(ops.market, "CALENDAR_FILE", path)
    assert ops.calendar_status(dt.datetime(2026, 10, 4, tzinfo=ops.CN))["status"] == "valid"
    assert ops.calendar_status(dt.datetime(2026, 11, 15, tzinfo=ops.CN))["status"] == "expiring"
    assert ops.calendar_status(dt.datetime(2027, 1, 4, tzinfo=ops.CN))["ok"] is False


def test_stale_paper_is_not_business_ready(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    with sqlite3.connect(tmp_path / "data" / "v2_market.db") as conn:
        for table in ("cb_daily_raw", "stk_close_daily", "cb_indicator"):
            conn.execute(f"CREATE TABLE {table}(trade_date TEXT)")
            conn.execute(f"INSERT INTO {table} VALUES('20260930')")
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    monkeypatch.setattr(ops, "calendar_status", lambda: {"ok": True, "status": "valid"})
    monkeypatch.setattr(ops.market, "latest_sync_status", lambda: {"expected_latest_trade_date": "2026-09-30"})
    monkeypatch.setattr(ops.market, "market_data_gate_status", lambda: {"ok": True})
    monkeypatch.setattr(ops, "_paper_dates", lambda: {"tsmom_vol": "20260929"})
    monkeypatch.setattr(ops, "mutation_status", lambda: {"running": False})
    result = ops.readiness()
    assert not result["ok"]
    assert result["lagging_strategies"] == ["tsmom_vol"]
