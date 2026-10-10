"""Scheduler 持久化与自愈：job_runs 落库、连续失败计数、opt-in 重试。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

import qsim.services.scheduler as sched_mod
from qsim.services.scheduler import Scheduler


def _event(job_id: str, code: int, exc: Exception | None = None):
    return SimpleNamespace(
        job_id=job_id,
        code=code,
        scheduled_run_time=datetime(2026, 9, 10, 9, 30),
        exception=exc,
    )


def _make(tmp_path, monkeypatch):
    monkeypatch.setattr(sched_mod.notify, "notify", lambda *a, **k: True)
    return Scheduler(state_path=tmp_path / "scheduler_state.db")


def test_error_and_success_persisted(tmp_path, monkeypatch):
    s = _make(tmp_path, monkeypatch)
    s._record_job_event(_event("paper_daily", EVENT_JOB_ERROR, RuntimeError("boom")))
    s._record_job_event(_event("paper_daily", EVENT_JOB_EXECUTED))
    assert s.last_runs["paper_daily"]["status"] == "success"
    assert s.consecutive_failures["paper_daily"] == 0
    s2 = Scheduler(state_path=tmp_path / "scheduler_state.db")
    assert s2.last_runs["paper_daily"]["status"] == "success"


def test_consecutive_failure_count_and_escalation(tmp_path, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(sched_mod.notify, "notify", lambda title, body: sent.append(title))
    s = _make(tmp_path, monkeypatch)
    monkeypatch.setattr(sched_mod.notify, "notify", lambda title, body: sent.append(title))
    for _ in range(3):
        s._record_job_event(_event("meta_shadow", EVENT_JOB_ERROR, RuntimeError("x")))
    assert s.consecutive_failures["meta_shadow"] == 3
    assert any("escalated" in t for t in sent)
    s._record_job_event(_event("meta_shadow", EVENT_JOB_EXECUTED))
    assert s.consecutive_failures["meta_shadow"] == 0


def test_retry_once_only_for_opted_in_jobs(tmp_path, monkeypatch):
    s = _make(tmp_path, monkeypatch)
    ran: list[str] = []

    def fn_ok():
        ran.append("ok")

    def fn_bad():
        ran.append("bad")

    s.add_daily_job("retryable_job", fn_ok, hour=9, minute=0, retry_once=True)
    s.add_daily_job("plain_job", fn_bad, hour=9, minute=0)
    s._record_job_event(_event("retryable_job", EVENT_JOB_ERROR, RuntimeError("e")))
    s._record_job_event(_event("retryable_job", EVENT_JOB_ERROR, RuntimeError("e")))
    s._record_job_event(_event("plain_job", EVENT_JOB_ERROR, RuntimeError("e")))
    ids = [j.id for j in s._scheduler.get_jobs()]
    assert "retryable_job__retry" in ids
    assert "plain_job__retry" not in ids


def test_state_survives_restart(tmp_path, monkeypatch):
    s = _make(tmp_path, monkeypatch)
    for _ in range(2):
        s._record_job_event(_event("shadow_diff", EVENT_JOB_ERROR, RuntimeError("e")))
    s2 = Scheduler(state_path=tmp_path / "scheduler_state.db")
    assert s2.consecutive_failures["shadow_diff"] == 2
    assert s2.last_runs["shadow_diff"]["status"] == "error"


def test_retry_success_updates_original_job(tmp_path, monkeypatch):
    s = _make(tmp_path, monkeypatch)
    s.add_daily_job("daily", lambda: None, hour=9, minute=0, retry_once=True)
    s._record_job_event(_event("daily", EVENT_JOB_ERROR, RuntimeError("busy")))
    s._record_job_event(_event("daily__retry", EVENT_JOB_EXECUTED))
    assert s.last_runs["daily"]["status"] == "success"
    assert s.consecutive_failures["daily"] == 0
    assert "daily__retry" not in s.last_runs


def test_new_schedule_gets_a_new_retry_budget(tmp_path, monkeypatch):
    s = _make(tmp_path, monkeypatch)
    s.add_daily_job("daily", lambda: None, hour=9, minute=0, retry_once=True)
    s._record_job_event(_event("daily", EVENT_JOB_ERROR, RuntimeError("busy")))
    s._record_job_event(_event("daily__retry", EVENT_JOB_ERROR, RuntimeError("still busy")))
    s._scheduler.remove_job("daily__retry")
    next_day = _event("daily", EVENT_JOB_ERROR, RuntimeError("busy again"))
    next_day.scheduled_run_time = datetime(2026, 9, 11, 9, 30)
    s._record_job_event(next_day)
    assert "daily__retry" in [job.id for job in s._scheduler.get_jobs()]
