from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import importlib
from pathlib import Path

from qsim.services.api.app import DAILY_JOB_TIMES, create_app  # noqa: E402
from qsim.services import realtime  # noqa: E402


def _write_auth_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str = "test-token") -> None:
    token_file = tmp_path / "qsim-auth-token"
    token_file.write_text(value, encoding="utf-8")
    monkeypatch.setattr(
        importlib.import_module("qsim.services.api.app"), "_AUTH_TOKEN_FILE", token_file
    )


def test_healthz() -> None:
    client = TestClient(create_app())
    assert client.get("/healthz").json() == {"ok": True}


def test_systemd_schedule_owner_cannot_start_second_scheduler(monkeypatch, tmp_path):
    monkeypatch.setenv("QSIM_SCHEDULE_OWNER", "systemd")
    monkeypatch.setenv("QSIM_SCHEDULER_ENABLED", "0")
    _write_auth_token(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        response = client.post("/api/scheduler/start", headers={"X-Auth-Token": "test-token"})
        assert response.status_code == 409
        assert client.get("/api/status").json()["scheduler_running"] is False


def test_busy_writer_returns_409(monkeypatch, tmp_path):
    from qsim.services.mutation_lock import mutation_guard
    monkeypatch.setenv("QSIM_MUTATION_LOCK", str(tmp_path / "lock"))
    _write_auth_token(monkeypatch, tmp_path)
    with mutation_guard("background-settlement"):
        response = TestClient(create_app()).post("/api/market/sync", headers={"X-Auth-Token": "test-token"})
        assert response.status_code == 409
        assert response.json()["task"]["running"]


def test_readyz_reports_business_failure(monkeypatch):
    from qsim.services import operations
    monkeypatch.setattr(operations, "readiness", lambda: {"ok": False, "issues": ["stale data"]})
    client = TestClient(create_app())
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503


def test_status() -> None:
    client = TestClient(create_app())
    body = client.get("/api/status").json()
    assert body["mode"] in ("meta_shadow_v1", "meta_shadow_pending")
    assert body["risk"] == "shadow_only"


def test_status_exposes_scheduler_state() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/api/status").json()
        assert body["scheduler_running"] is True
        expected_jobs = {
            "legacy_stock_fetch",
            "legacy_daily_settlement",
            "sync_refactor_data",
            "paper_daily",
            "meta_shadow",
            "shadow_diff",
            "realtime_quotes_5m",
            "versioned_open_execution",
            "realtime_quotes_close",
            "qc_check",
            "ai_review",
            "preflight",
            "stock_ensure",
        }
        assert expected_jobs.issubset(set(body["scheduler_jobs"]))
        assert body["scheduler_job_details"]["paper_daily"]["next_run_time"]


def test_daily_jobs_run_in_data_to_paper_to_shadow_order() -> None:
    assert DAILY_JOB_TIMES["sync_refactor_data"] < DAILY_JOB_TIMES["paper_daily"]
    assert DAILY_JOB_TIMES["paper_daily"] < DAILY_JOB_TIMES["shadow_diff"]


def test_trigger_refresh_realtime_uses_qsim_native_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api_module = importlib.import_module("qsim.services.api.app")
    expected = {"ok": True, "codes": 1, "updated": 1, "at": "test"}
    monkeypatch.setattr(api_module._realtime, "is_trading_session", lambda: True)
    monkeypatch.setattr(api_module._realtime, "refresh_realtime", lambda: expected)

    _write_auth_token(monkeypatch, tmp_path, "test-token")
    response = TestClient(create_app()).post(
        "/refresh-realtime", headers={"X-Auth-Token": "test-token"}
    )

    assert response.json() == expected


def test_native_write_requires_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_auth_token(monkeypatch, tmp_path)
    response = TestClient(create_app()).post("/api/scheduler/shutdown")
    assert response.status_code == 401


def test_native_failure_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api_module = importlib.import_module("qsim.services.api.app")
    _write_auth_token(monkeypatch, tmp_path)
    monkeypatch.setattr(api_module._realtime, "is_trading_session", lambda: True)
    monkeypatch.setattr(
        api_module._realtime,
        "refresh_realtime",
        lambda: {"ok": False, "error": "upstream unavailable"},
    )

    response = TestClient(create_app()).post(
        "/refresh-realtime", headers={"X-Auth-Token": "test-token"}
    )

    assert response.status_code == 503


def test_native_refresh_skips_outside_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api_module = importlib.import_module("qsim.services.api.app")
    _write_auth_token(monkeypatch, tmp_path)
    monkeypatch.setattr(api_module._realtime, "is_trading_session", lambda: False)
    monkeypatch.setattr(
        api_module._realtime,
        "refresh_realtime",
        lambda: pytest.fail("refresh must not run outside session"),
    )

    response = TestClient(create_app()).post(
        "/refresh-realtime", headers={"X-Auth-Token": "test-token"}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "skipped": "outside-session"}


def test_config() -> None:
    client = TestClient(create_app())
    body = client.get("/api/config").json()
    assert "tsmom_vol" in body["default_regime_weights"]["up"]
    assert body["risk_rule"] == "tiered_drawdown"


def test_trading_calendar_excludes_holiday_and_weekend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calendar_file = tmp_path / "trading-calendar.json"
    calendar_file.write_text(
        '{"valid_from":"2026-01-01","valid_through":"2026-12-31",'
        '"closed_dates":["2026-10-01"]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(realtime, "CALENDAR_FILE", calendar_file)

    holiday = realtime.is_trading_session(
        realtime.dt.datetime(2026, 10, 1, 10, 0, tzinfo=realtime.SHANGHAI_TZ)
    )
    open_day = realtime.is_trading_session(
        realtime.dt.datetime(2026, 10, 8, 10, 0, tzinfo=realtime.SHANGHAI_TZ)
    )
    weekend = realtime.is_trading_session(
        realtime.dt.datetime(2026, 10, 10, 10, 0, tzinfo=realtime.SHANGHAI_TZ)
    )

    assert holiday is False
    assert open_day is True
    assert weekend is False


def test_trading_calendar_fails_closed_when_out_of_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calendar_file = tmp_path / "trading-calendar.json"
    calendar_file.write_text(
        '{"valid_from":"2026-01-01","valid_through":"2026-12-31",'
        '"closed_dates":[]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(realtime, "CALENDAR_FILE", calendar_file)

    assert realtime.is_trading_session(
        realtime.dt.datetime(2027, 1, 4, 10, 0, tzinfo=realtime.SHANGHAI_TZ)
    ) is False


def test_direct_realtime_refresh_also_respects_session_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime, "is_trading_session", lambda: False)

    assert realtime.refresh_realtime() == {"ok": True, "skipped": "outside-session"}


def test_market_sync_supports_background_trigger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api_module = importlib.import_module("qsim.services.api.app")
    _write_auth_token(monkeypatch, tmp_path, "test-token")
    monkeypatch.setattr(
        api_module._legacy_adapter,
        "sync_legacy_market_data",
        lambda: {"ok": True, "run_id": "test-run"},
    )

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/market/sync?background=true",
            headers={"X-Auth-Token": "test-token"},
        )

    assert response.status_code == 202
    assert response.json() == {"ok": True, "status": "started"}


def test_paper_job_is_blocked_when_market_data_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    api_module = importlib.import_module("qsim.services.api.app")
    monkeypatch.setattr(
        api_module._legacy_adapter, "market_data_gate_status",
        lambda: {"ok": False, "reason": "quant_stock_bars_stale"},
    )

    with pytest.raises(RuntimeError, match="paper_daily blocked by market-data freshness gate"):
        api_module._run_paper_job()


def test_legacy_trigger_discovers_current_docker_address(monkeypatch: pytest.MonkeyPatch) -> None:
    api_module = importlib.import_module("qsim.services.api.app")
    monkeypatch.delenv("QSIM_LEGACY_TRIGGER", raising=False)
    monkeypatch.setattr(
        api_module.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="192.0.2.10 198.51.100.10 "),
    )

    assert api_module._legacy_trigger_urls() == [
        "http://192.0.2.10:8502", "http://198.51.100.10:8502", "http://127.0.0.1:8502",
    ]


def test_legacy_trigger_uses_its_own_auth_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    api_module = importlib.import_module("qsim.services.api.app")
    token_file = tmp_path / "legacy.env"
    token_file.write_text("OTHER=value\nQUANT_AUTH_TOKEN=legacy-token\n", encoding="utf-8")
    monkeypatch.setattr(api_module, "_LEGACY_AUTH_TOKEN_FILE", token_file)

    assert api_module._legacy_auth_token() == "legacy-token"
