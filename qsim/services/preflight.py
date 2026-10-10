"""Pre-market environment self-check.

Runs before the daily flow starts: token/config presence, calendar validity,
DB writability, error backlog. Returns a list of issue strings — empty means
healthy. Non-fatal by design; the caller decides how to alert.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
from pathlib import Path

from qsim.services import notify, realtime

logger = logging.getLogger("qsim.preflight")

ROOT = Path(__file__).resolve().parents[2]
QUANT_DB = ROOT / "data" / "quant.db"
CALENDAR_FILE = Path(
    os.environ.get("QSIM_CALENDAR_FILE", str(ROOT / "config" / "trading-calendar.json"))
)
SECRETS_DIR = ROOT / ".secrets"


def _check_calendar(today: dt.date) -> list[str]:
    issues: list[str] = []
    try:
        payload = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
        valid_from = dt.date.fromisoformat(str(payload["valid_from"]))
        valid_through = dt.date.fromisoformat(str(payload["valid_through"]))
        if not valid_from <= today <= valid_through:
            issues.append(
                f"交易日历不覆盖今天 {today}（{valid_from}~{valid_through}），需更新"
            )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        issues.append(f"交易日历不可用: {exc}")
    return issues


def _check_db_writable() -> list[str]:
    issues: list[str] = []
    conn = None
    try:
        conn = sqlite3.connect(f"file:{QUANT_DB.resolve()}?mode=rw", uri=True, timeout=10)
        # Reserve the writer without creating/dropping a user table.
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    except sqlite3.Error as exc:
        issues.append(f"quant.db 不可写: {exc}")
    finally:
        if conn is not None:
            conn.close()
    return issues


def _check_tokens() -> list[str]:
    issues: list[str] = []
    if not (SECRETS_DIR / "qsim-auth-token").is_file():
        issues.append("缺少 .secrets/qsim-auth-token")
    # Check configured credential-file presence only; never inspect another
    # project's .env or its values during a health request.
    market_file = Path(os.environ.get("QSIM_MARKET_TOKEN_FILE", str(SECRETS_DIR / "tushare-token")))
    if not market_file.is_file() and not os.environ.get("TUSHARE_TOKEN"):
        issues.append("行情凭证文件未配置")
    return issues


def _check_error_backlog(hours: float = 48) -> list[str]:
    issues: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{QUANT_DB.resolve()}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        cutoff = (
            dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
            - dt.timedelta(hours=hours)
        ).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            "SELECT category, COUNT(*) AS n FROM event_log "
            "WHERE level='ERROR' AND ts>=? GROUP BY category ORDER BY n DESC",
            (cutoff,),
        ).fetchall()
        conn.close()
        if rows:
            detail = "；".join(f"{r['category']}×{r['n']}" for r in rows[:5])
            issues.append(f"近{int(hours)}h 有 {sum(int(r['n']) for r in rows)} 条 ERROR: {detail}")
    except sqlite3.Error as exc:
        issues.append(f"错误积压检查失败: {exc}")
    return issues


def preflight_check(now: dt.datetime | None = None) -> list[str]:
    """Run all checks, return issue list (empty = healthy)."""
    now = now or dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    today = now.date()
    issues: list[str] = []
    issues += _check_calendar(today)
    issues += _check_db_writable()
    issues += _check_tokens()
    issues += _check_error_backlog()
    return issues


def run_daily() -> dict:
    """Scheduled entry: notify when issues are found."""
    issues = preflight_check()
    if issues:
        notify.notify(
            "[qsim] 盘前自检发现问题",
            "\n".join(f"- {i}" for i in issues[:8]),
        )
        logger.warning("preflight issues: %s", issues)
    else:
        logger.info("preflight ok")
    return {"ok": not issues, "issues": issues}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    problems = preflight_check()
    if problems:
        for item in problems:
            print(f"[ISSUE] {item}")
    else:
        print("[ok] preflight healthy")
