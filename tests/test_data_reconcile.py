import datetime as dt

from qsim.services import data_reconcile as job


def test_primary_window_defers_repair(monkeypatch):
    monkeypatch.setenv("QSIM_SCHEDULER_ENABLED", "1")
    monkeypatch.setattr(job.market, "_expected_latest_trade_date", lambda now: "2026-09-30")
    monkeypatch.setattr(job, "_source_fresh", lambda *args: 1 / 0)
    result = job.reconcile(dt.datetime(2026, 9, 30, 19, 0, tzinfo=job.CN))
    assert result["status"] == "primary_schedule_running"


def test_systemd_owner_runs_native_pipeline_during_primary_window(monkeypatch):
    from qsim.services import native_market, market_quality
    monkeypatch.setenv("QSIM_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("QSIM_NATIVE_MARKET", "1")
    monkeypatch.setattr(job.market, "_expected_latest_trade_date", lambda now: "2026-09-30")
    calls = []
    monkeypatch.setattr(native_market, "run", lambda: calls.append("collect") or {"status": "updated"})
    monkeypatch.setattr(market_quality, "check", lambda *a, **kw: calls.append("quality") or {"ok": True})
    monkeypatch.setattr(job, "_paper_fresh", lambda *a: True)
    result = job.reconcile(dt.datetime(2026, 9, 30, 19, 0, tzinfo=job.CN))
    assert result["status"] == "repaired"
    assert result["actions"] == ["native-market"]
    assert calls == ["collect", "quality"]


def test_collector_can_run_while_paper_admission_is_blocked(monkeypatch):
    from qsim.services import native_market, market_quality
    monkeypatch.setenv("QSIM_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("QSIM_NATIVE_MARKET", "1")
    monkeypatch.setenv("QSIM_PAPER_AUTORUN", "0")
    monkeypatch.setattr(job.market, "_expected_latest_trade_date", lambda now: "2026-09-30")
    monkeypatch.setattr(native_market, "run", lambda: {"status": "updated"})
    monkeypatch.setattr(market_quality, "check", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(job, "_paper_fresh", lambda *a: 1 / 0)
    result=job.reconcile(dt.datetime(2026, 9, 30, 19, 0, tzinfo=job.CN))
    assert result["status"] == "collected_paper_blocked"
    assert result["paper"] == "BLOCKED"
    assert result["actions"] == ["native-market"]


def test_missed_stages_repair_in_dependency_order(monkeypatch):
    monkeypatch.setattr(job.market, "_expected_latest_trade_date", lambda now: "2026-09-30")
    state = {"stock_bars": False, "bars": False, "synced": False}
    calls = []

    def source_fresh(_db, table, _column, _expected):
        return state[table]

    def trigger(name):
        calls.append(name)
        state["stock_bars" if name == "fetch-stocks" else "bars"] = True

    def sync():
        calls.append("sync")
        state["synced"] = True
        return {"ok": True}

    def status():
        freshness = "fresh" if state["synced"] else "stale"
        return {"datasets": {name: {"freshness": freshness}
                             for name in ("quant.db", "v2_market.db")}}

    monkeypatch.setattr(job, "_source_fresh", source_fresh)
    monkeypatch.setattr(job, "_source_stock_coverage", lambda *args: state["stock_bars"])
    monkeypatch.setattr(job, "_source_etfs_complete", lambda *args: state["bars"])
    monkeypatch.setattr(job, "_call_trigger", trigger)
    monkeypatch.setattr(job.cb_etl_runner, "run", lambda: calls.append("cb-etl") or {"status": "updated"})
    monkeypatch.setattr(job.market, "latest_sync_status", status)
    monkeypatch.setattr(job.market, "market_data_gate_status", lambda: {"ok": True})
    monkeypatch.setattr(job, "_v2_fresh", lambda *args: state["synced"])
    monkeypatch.setattr(job.market, "sync_legacy_market_data", sync)
    monkeypatch.setattr(job, "_paper_fresh", lambda expected: True)

    result = job.reconcile(dt.datetime(2026, 10, 4, 22, 0, tzinfo=job.CN))
    assert result["status"] == "repaired"
    assert calls == ["fetch-stocks", "run-daily", "cb-etl", "sync"]


def test_complete_data_remains_untouched(monkeypatch):
    monkeypatch.setattr(job.market, "_expected_latest_trade_date", lambda now: "2026-09-30")
    monkeypatch.setattr(job, "_source_fresh", lambda *args: True)
    monkeypatch.setattr(job, "_source_stock_coverage", lambda *args: True)
    monkeypatch.setattr(job, "_source_etfs_complete", lambda *args: True)
    monkeypatch.setattr(job.cb_etl_runner, "run", lambda: {"status": "up_to_date"})
    monkeypatch.setattr(job.market, "latest_sync_status", lambda: {
        "datasets": {name: {"freshness": "fresh"}
                     for name in ("quant.db", "v2_market.db")},
    })
    monkeypatch.setattr(job.market, "market_data_gate_status", lambda: {"ok": True})
    monkeypatch.setattr(job, "_v2_fresh", lambda *args: True)
    monkeypatch.setattr(job.market, "sync_legacy_market_data", lambda: 1 / 0)
    monkeypatch.setattr(job, "_paper_fresh", lambda expected: True)

    result = job.reconcile(dt.datetime(2026, 10, 4, 22, 0, tzinfo=job.CN))
    assert result == {"ok": True, "status": "up_to_date", "session": "2026-09-30", "actions": []}


def test_missing_target_cb_indicator_forces_sync(monkeypatch):
    monkeypatch.setattr(job.market, "_expected_latest_trade_date", lambda now: "2026-09-30")
    monkeypatch.setattr(job, "_source_fresh", lambda *args: True)
    monkeypatch.setattr(job, "_source_stock_coverage", lambda *args: True)
    monkeypatch.setattr(job, "_source_etfs_complete", lambda *args: True)
    monkeypatch.setattr(job.cb_etl_runner, "run", lambda: {"status": "up_to_date"})
    monkeypatch.setattr(job.market, "latest_sync_status", lambda: {
        "datasets": {name: {"freshness": "fresh"}
                     for name in ("quant.db", "v2_market.db")},
    })
    monkeypatch.setattr(job.market, "market_data_gate_status", lambda: {"ok": True})
    monkeypatch.setattr(job, "_paper_fresh", lambda expected: True)
    state = {"synced": False}
    monkeypatch.setattr(job, "_v2_fresh", lambda *args: state["synced"])

    def sync():
        state["synced"] = True
        return {"ok": True}

    monkeypatch.setattr(job.market, "sync_legacy_market_data", sync)
    result = job.reconcile(dt.datetime(2026, 10, 4, 22, 0, tzinfo=job.CN))
    assert result["actions"] == ["sync"]
