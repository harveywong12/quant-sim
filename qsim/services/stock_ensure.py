"""Stock daily-bar guarantee: backfill late-published bars, then close the loop.

Individual stock dailies often arrive after the first evening fetch, which
would leave paper accounts unsettled. This module is the idempotent
"ensure today's data" entry, scheduled every 30 minutes in the backfill
window (15:35-21:35):

1. data complete + accounts settled      -> complete (instant return)
2. data complete, accounts lagging       -> settle only
3. data missing, inside backfill window  -> fetch via the legacy trigger;
                                            settle automatically once landed
4. source still unpublished              -> pending, retried next round

All steps are incremental and idempotent; a process-local lock prevents
concurrent re-entry from overlapping scheduler rounds.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import subprocess
import threading
from pathlib import Path

from qsim.services import notify, realtime

logger = logging.getLogger("qsim.stock_ensure")

ROOT = Path(__file__).resolve().parents[2]
QUANT_DB = ROOT / "data" / "quant.db"
LOCK = threading.Lock()

WINDOW_START = dt.time(15, 35)
WINDOW_END = dt.time(21, 35)


def _cn_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(QUANT_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _expected_day(today: dt.date) -> dt.date:
    day = today
    for _ in range(10):
        if realtime._is_trading_day(day):
            return day
        day -= dt.timedelta(days=1)
    return today


def _latest_stock_bar(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(date) AS d FROM stock_bars").fetchone()
    return str(row["d"]) if row and row["d"] else None


def _critical_codes(conn: sqlite3.Connection) -> set[str]:
    """Held positions plus pending order-intent candidates must have bars."""
    codes: set[str] = set()
    try:
        for row in conn.execute(
            "SELECT DISTINCT ts_code FROM qsim_positions WHERE shares > 0"
        ):
            if row["ts_code"]:
                codes.add(str(row["ts_code"]))
    except sqlite3.Error:
        pass
    try:
        for row in conn.execute(
            "SELECT candidates_json FROM qsim_order_intents "
            "WHERE status IN ('frozen', 'pending')"
        ):
            try:
                payload = json.loads(row["candidates_json"] or "[]")
            except ValueError:
                continue
            items = payload if isinstance(payload, list) else payload.get("candidates", [])
            for item in items:
                code = str(item.get("ts_code") or item.get("code") or "") if isinstance(item, dict) else str(item)
                if code:
                    codes.add(code)
    except sqlite3.Error:
        pass
    return codes


def _missing_critical(
    conn: sqlite3.Connection, codes: set[str], expected: str
) -> list[str]:
    if not codes:
        return []
    missing: list[str] = []
    for code in sorted(codes):
        like = f"%{code[:6]}%"
        row = conn.execute(
            "SELECT 1 FROM stock_bars WHERE (ts_code=? OR ts_code LIKE ?) "
            "AND date>=? LIMIT 1",
            (code, like, expected),
        ).fetchone()
        if row is None:
            missing.append(code)
    return missing


def _settlement_lagging(
    conn: sqlite3.Connection, expected: str
) -> list[str]:
    lagging: list[str] = []
    rows = conn.execute(
        "SELECT s.strategy, MAX(s.date) AS d FROM qsim_snapshots s "
        "JOIN qsim_accounts a ON a.strategy=s.strategy "
        "WHERE a.status='active' GROUP BY s.strategy"
    ).fetchall()
    for row in rows:
        if not row["d"] or str(row["d"]) < expected:
            lagging.append(str(row["strategy"]))
    return lagging


def _fetch_via_legacy_trigger() -> dict:
    """Ask the legacy trigger container to run its canonical stock fetch."""
    from qsim.services import legacy_trigger, native_market
    try:
        if native_market.enabled():
            return native_market.run()
        return legacy_trigger.call("fetch-stocks", timeout=1200)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[-300:]}


def _settle(conn: sqlite3.Connection, expected: str, lagging: list[str]) -> dict:
    script = ROOT / "qsim" / "services" / "paper_daily.py"
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), str(script)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr[-300:]}
    remaining = _settlement_lagging(conn, expected)
    remaining = [s for s in lagging if s in remaining]
    settled = [s for s in lagging if s not in remaining]
    if remaining:
        return {"ok": False, "error": f"补账后仍滞后: {','.join(remaining)}"}
    return {"ok": True, "settled": settled}


def ensure_stock_data(now: dt.datetime | None = None) -> dict:
    now = now or _cn_now()
    today, expected = now.date(), _expected_day(now.date())
    expected_s = expected.strftime("%Y%m%d")
    is_today = expected == today

    if not LOCK.acquire(blocking=False):
        return {"ok": False, "status": "busy"}
    conn = _conn()
    try:
        latest = _latest_stock_bar(conn)
        missing = _missing_critical(conn, _critical_codes(conn), expected_s)
        data_complete = bool(latest and latest >= expected_s and not missing)

        if data_complete:
            lagging = _settlement_lagging(conn, expected_s)
            if not lagging:
                return {"ok": True, "status": "complete",
                        "latest": latest, "expected": expected_s}
            result = _settle(conn, expected_s, lagging)
            return {"ok": bool(result["ok"]), "status": "settled" if result["ok"] else "error",
                    "expected": expected_s, **result}

        if is_today:
            current = now.time().replace(tzinfo=None)
            if not (WINDOW_START <= current <= WINDOW_END):
                return {"ok": True, "status": "outside-window",
                        "latest": latest, "expected": expected_s,
                        "missing_critical": missing}

        logger.info("stock bars lagging (latest=%s expected=%s), backfilling",
                    latest or "none", expected_s)
        fetch = _fetch_via_legacy_trigger()
        if not fetch.get("ok", True):
            return {"ok": False, "status": "error", "error": f"补拉失败: {fetch.get('error', fetch)}"}

        latest2 = _latest_stock_bar(conn)
        missing2 = _missing_critical(conn, _critical_codes(conn), expected_s)
        if not (latest2 and latest2 >= expected_s and not missing2):
            return {"ok": True, "status": "pending",
                    "latest": latest2, "expected": expected_s,
                    "missing_critical": missing2}
        lagging = _settlement_lagging(conn, expected_s)
        if not lagging:
            return {"ok": True, "status": "fetched",
                    "latest": latest2, "expected": expected_s}
        result = _settle(conn, expected_s, lagging)
        return {"ok": bool(result["ok"]),
                "status": "fetched+settled" if result["ok"] else "error",
                "expected": expected_s, **result}
    finally:
        conn.close()
        LOCK.release()


def run_scheduled() -> dict:
    result = ensure_stock_data()
    status = result.get("status")
    if status in ("error",):
        notify.notify("[qsim] 个股数据保障失败",
                      json.dumps(result, ensure_ascii=False)[:400])
    logger.info("stock_ensure: %s", result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(ensure_stock_data(), ensure_ascii=False, indent=2))
