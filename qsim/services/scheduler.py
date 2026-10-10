"""APScheduler wrapper for qsim jobs.

持久化：每次 job 事件写入 artifacts/scheduler_state.db（重启不丢），
并从库恢复 last_runs / 连续失败计数。
自愈：opt-in（retry_once=True）的幂等 job 失败后自动重试一次；
连续失败达阈值升级告警。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler

from qsim.services import notify

logger = logging.getLogger("qsim.scheduler")

ROOT = Path(__file__).resolve().parents[2]

FAIL_THRESHOLD = 3
RETRY_DELAY_MINUTES = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    scheduled_at TEXT,
    error TEXT,
    finished_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job_id ON job_runs(job_id, id);
CREATE TABLE IF NOT EXISTS fail_counts (
    job_id TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);
"""


class Scheduler:
    def __init__(self, state_path: str | Path | None = None) -> None:
        self._scheduler = BackgroundScheduler(daemon=True, timezone="Asia/Shanghai", executors={"default": {"type": "threadpool", "max_workers": 1}})
        self._last_runs: dict[str, dict[str, str]] = {}
        self._fail_counts: dict[str, int] = {}
        self._funcs: dict[str, Callable[[], None]] = {}
        self._retryable: set[str] = set()
        self._retried_once: set[str] = set()
        if state_path is None:
            state_path = os.environ.get("QSIM_SCHEDULER_STATE_PATH")
        self._state_path = Path(state_path) if state_path else ROOT / "artifacts" / "scheduler_state.db"
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._load_state()
        self._scheduler.add_listener(
            self._record_job_event,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
        )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._state_path)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _load_state(self) -> None:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT job_id, status, scheduled_at, error FROM job_runs j"
                " WHERE id = (SELECT MAX(id) FROM job_runs WHERE job_id = j.job_id)"
            ).fetchall()
            for job_id, status, scheduled_at, error in rows:
                entry: dict[str, str] = {"status": status, "scheduled_run_time": scheduled_at or ""}
                if error:
                    entry["error"] = error
                self._last_runs[job_id] = entry
            self._fail_counts = {
                r[0]: r[1] for r in conn.execute("SELECT job_id, count FROM fail_counts").fetchall()
            }
        finally:
            conn.close()

    def _persist_run(self, job_id: str, status: str, scheduled_at: str, error: str | None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO job_runs(job_id, status, scheduled_at, error, finished_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (job_id, status, scheduled_at, error, datetime.now().isoformat(timespec="seconds")),
            )
            conn.execute(
                "INSERT INTO fail_counts(job_id, count) VALUES (?, ?)"
                " ON CONFLICT(job_id) DO UPDATE SET count = excluded.count",
                (job_id, self._fail_counts.get(job_id, 0)),
            )
            conn.commit()
        finally:
            conn.close()

    def add_cron_job(
        self,
        job_id: str,
        func: Callable[[], None],
        retry_once: bool = False,
        **trigger_kwargs: object,
    ) -> None:
        self._funcs[job_id] = func
        if retry_once:
            self._retryable.add(job_id)
        self._scheduler.add_job(
            func,
            "cron",
            id=job_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
            **trigger_kwargs,
        )
        logger.info("scheduled %s with cron %s", job_id, trigger_kwargs)

    def add_daily_job(
        self,
        job_id: str,
        func: Callable[[], None],
        hour: int,
        minute: int,
        retry_once: bool = False,
    ) -> None:
        self.add_cron_job(
            job_id,
            func,
            retry_once=retry_once,
            hour=hour,
            minute=minute,
        )

    def _record_job_event(self, event: object) -> None:
        job_id = getattr(event, "job_id", None)
        if not job_id:
            return
        is_retry = str(job_id).endswith("__retry")
        if is_retry:
            job_id = str(job_id)[:-7]
        scheduled = getattr(event, "scheduled_run_time", None)
        scheduled_at = scheduled.isoformat() if isinstance(scheduled, datetime) else None
        if not is_retry and scheduled_at != self._last_runs.get(job_id, {}).get("scheduled_run_time"):
            self._retried_once.discard(job_id)
        code = getattr(event, "code", None)
        if code == EVENT_JOB_EXECUTED:
            self._last_runs[job_id] = {
                "status": "success",
                "scheduled_run_time": scheduled_at or "",
            }
            self._fail_counts[job_id] = 0
            self._retried_once.discard(job_id)
            self._persist_run(job_id, "success", scheduled_at or "", None)
        elif code == EVENT_JOB_MISSED:
            self._last_runs[job_id] = {
                "status": "missed",
                "scheduled_run_time": scheduled_at or "",
            }
            self._persist_run(job_id, "missed", scheduled_at or "", None)
        else:
            exception = getattr(event, "exception", None)
            error = str(exception or "job failed")[-500:]
            self._last_runs[job_id] = {
                "status": "error",
                "scheduled_run_time": scheduled_at or "",
                "error": error,
            }
            self._fail_counts[job_id] = self._fail_counts.get(job_id, 0) + 1
            self._persist_run(job_id, "error", scheduled_at or "", error)
            notify.notify(
                f"[qsim] 调度任务失败: {job_id}",
                f"scheduled={scheduled_at or 'unknown'}\n{str(exception or 'job failed')[-400:]}",
            )
            if self._fail_counts[job_id] == FAIL_THRESHOLD:
                notify.notify(
                    f"[qsim][escalated] 连续 {FAIL_THRESHOLD} 次失败: {job_id}",
                    "达到阈值，需要人工介入排查",
                )
            if not is_retry:
                self._maybe_retry(job_id)
        self._publish(job_id, self._last_runs[job_id]["status"], scheduled_at)

    def _publish(self, job_id: str, status: str, scheduled_at: str | None) -> None:
        try:
            from qsim.services import events

            events.publish(
                {"type": "job", "job_id": job_id,
                 "status": status, "scheduled_run_time": scheduled_at or ""}
            )
        except Exception:
            logger.warning("publish job event failed", exc_info=True)

    def _maybe_retry(self, job_id: str) -> None:
        if job_id not in self._retryable or job_id in self._retried_once:
            return
        if job_id not in self._funcs:
            return
        self._retried_once.add(job_id)
        self._scheduler.add_job(
            self._funcs[job_id],
            "date",
            id=f"{job_id}__retry",
            run_date=datetime.now().astimezone() + timedelta(minutes=RETRY_DELAY_MINUTES),
            max_instances=1,
        )
        logger.warning("scheduled one-time retry for %s in %d min", job_id, RETRY_DELAY_MINUTES)

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("scheduler started")

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler stopped")

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    @property
    def jobs(self) -> list[str]:
        return [job.id for job in self._scheduler.get_jobs()]

    @property
    def job_details(self) -> dict[str, dict[str, str | None]]:
        return {
            job.id: {
                "next_run_time": getattr(job, "next_run_time", None).isoformat()
                if getattr(job, "next_run_time", None) is not None
                else None,
            }
            for job in self._scheduler.get_jobs()
        }

    @property
    def last_runs(self) -> dict[str, dict[str, str]]:
        return {job_id: dict(details) for job_id, details in self._last_runs.items()}

    @property
    def consecutive_failures(self) -> dict[str, int]:
        return dict(self._fail_counts)
