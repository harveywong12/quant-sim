"""Read-only business readiness, separate from process liveness."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import datetime as dt
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

from qsim.market import legacy_adapter as market
from qsim.services.mutation_lock import mutation_status

ROOT = Path(__file__).resolve().parents[2]
CN = ZoneInfo("Asia/Shanghai")
_PAGE_STATE = ContextVar("qsim_page_operations", default=None)


def calendar_status(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(CN)
    payload = market._load_calendar_payload()
    if not payload:
        return {"ok": False, "status": "invalid", "error": "交易日历缺失或格式错误"}
    remaining = (payload["valid_through"] - now.date()).days
    valid = payload["valid_from"] <= now.date() <= payload["valid_through"]
    return {"ok": valid, "status": "expired" if not valid else "expiring" if remaining <= 60 else "valid",
            "valid_through": payload["valid_through"].isoformat(), "days_remaining": remaining}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def timer_status() -> list[dict]:
    names = ("qsim-cb-etl.timer", "qsim-data-reconcile.timer", "qsim-backup.timer", "qsim-watchdog.timer", "qsim-recovery-drill.timer")
    try:
        proc = subprocess.run(["systemctl", "--user", "show", *names,
                               "--property=Id,ActiveState,NextElapseUSecRealtime"],
                              capture_output=True, text=True, timeout=3)
        rows = []
        for block in proc.stdout.strip().split("\n\n"):
            values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if values.get("Id"):
                rows.append({"unit": values["Id"], "active": values.get("ActiveState") == "active",
                             "next_run": values.get("NextElapseUSecRealtime") or None})
        return rows
    except (OSError, subprocess.SubprocessError):
        return []


def _paper_dates() -> dict[str, str | None]:
    from qsim.storage.paper import PAPER_STRATEGIES, PAPER_VERSIONED_CANDIDATES
    from qsim.strategies.migration import paper_enabled
    names = [name for name in PAPER_STRATEGIES if paper_enabled(name)]
    names.extend(name for name, cfg in PAPER_VERSIONED_CANDIDATES.items() if cfg.get("backfill", True))
    conn = sqlite3.connect(f"file:{(ROOT / 'data' / 'quant.db').resolve()}?mode=ro", uri=True)
    try:
        return {name: conn.execute("SELECT MAX(date) FROM qsim_snapshots WHERE strategy=?", (name,)).fetchone()[0]
                for name in names}
    finally:
        conn.close()


def scheduler_runs() -> list[dict]:
    path = ROOT / "artifacts" / "scheduler_state.db"
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(
                "SELECT job_id,status,error,finished_at FROM job_runs j WHERE id="
                "(SELECT MAX(id) FROM job_runs WHERE job_id=j.job_id) ORDER BY job_id")]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def readiness() -> dict:
    issues = []
    if os.environ.get("QSIM_PAPER_AUTORUN", "1") == "0":
        issues.append("正式模拟执行未通过原始价格及公司行动准入；采集单独运行，旧账本保留")
    calendar = calendar_status()
    if not calendar["ok"]:
        issues.append("交易日历无效，自动交易与更新已暂停")
    try:
        sync = market.latest_sync_status()
        expected = sync.get("expected_latest_trade_date")
        gate = market.market_data_gate_status()
        if not gate.get("ok"):
            issues.append("股票或 ETF 行情未达到预期日期或覆盖率")
        dataset = sync.get("datasets", {}).get("v2_market.db", {})
        conn = sqlite3.connect(f"file:{(ROOT / 'data' / 'v2_market.db').resolve()}?mode=ro", uri=True)
        try:
            v2_dates = {name: conn.execute(f"SELECT MAX(trade_date) FROM {name}").fetchone()[0]
                        for name in ("cb_daily_raw", "stk_close_daily", "cb_indicator")}
        finally:
            conn.close()
        if not expected or any(market._date_key(day) < market._date_key(expected) for day in v2_dates.values()):
            issues.append("可转债行情或指标尚未更新完整")
        paper = _paper_dates()
        lagging = [name for name, day in paper.items()
                   if not expected or market._date_key(day) < market._date_key(expected)]
        if lagging:
            issues.append("模拟盘待结算：" + "、".join(lagging))
    except (sqlite3.Error, OSError, ValueError) as exc:
        expected, sync, gate, v2_dates, paper, lagging = None, {}, {}, {}, {}, []
        issues.append("业务数据无法读取：" + str(exc)[:150])
    from qsim.services import native_market
    quality = {}
    if native_market.enabled() and expected:
        from qsim.services.market_quality import check
        quality = check(expected, ROOT, require_publication=True)
        issues.extend(quality["issues"])
    settlement = _read_json(ROOT / "artifacts" / "settlement_state.json")
    if settlement.get("ok") is False:
        issues.append("正式账本已保护，结算待处理：" + str(settlement.get("error", "")))
    task = mutation_status()
    return {"ok": not issues, "checked_at": dt.datetime.now(CN).isoformat(timespec="seconds"),
            "execution_mode": "daily_delayed_immutable_close" if os.environ.get("QSIM_FORMAL_PAPER", "0") == "1" else "legacy",
            "tax_profile": "mainland_individual_separate_accounts" if os.environ.get("QSIM_FORMAL_PAPER", "0") == "1" else "legacy",
            "ten_year_target": "NOT_VERIFIED",
            "cash_payment_rounding": os.environ.get("QSIM_DIVIDEND_ROUNDING", "require_evidence"),
            "expected_trade_date": expected, "issues": issues, "calendar": calendar,
            "market": sync, "coverage": gate, "convertible_dates": v2_dates,
            "offsite_backup": _read_json(ROOT / "artifacts" / "offsite_backup_state.json"), "native_market_state": _read_json(ROOT / "artifacts" / "native_market_state.json"), "quality": quality, "settlement": settlement, "native_collector": native_market.enabled(), "paper_dates": paper, "lagging_strategies": lagging, "task": task,
            "reconcile": _read_json(ROOT / "artifacts" / "data_reconcile_state.json"),
            "backup": _read_json(ROOT / "artifacts" / "recovery_backup_state.json"),
            "restore_drill": _read_json(ROOT / "artifacts" / "recovery_drill_state.json")}


def status() -> dict:
    cached = _PAGE_STATE.get()
    if cached is not None:
        return cached
    return {**readiness(), "timers": timer_status(), "scheduler_runs": scheduler_runs()}


@contextmanager
def page_snapshot():
    token = _PAGE_STATE.set(status())
    try:
        yield
    finally:
        _PAGE_STATE.reset(token)
