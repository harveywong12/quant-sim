"""Catch up missed end-of-day data jobs after API downtime.

When the API owns scheduling, defer during its evening window. With
QSIM_SCHEDULER_ENABLED=0, systemd owns the pipeline and must run in that window.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from qsim.market import legacy_adapter as market
from qsim.services import cb_etl_runner, legacy_trigger, notify

ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROOT = Path(os.environ.get("QSIM_LEGACY_ROOT", "/srv/quant-sim"))
STATE = ROOT / "artifacts" / "data_reconcile_state.json"
CN = ZoneInfo("Asia/Shanghai")


def _max_date(db: Path, table: str, column: str) -> str | None:
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _source_fresh(db: Path, table: str, column: str, expected: str) -> bool:
    return market._date_key(_max_date(db, table, column)) >= market._date_key(expected)


def _source_stock_coverage(db: Path, expected: str) -> bool:
    day = market._date_key(expected)
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        observed = int(conn.execute(
            "SELECT COUNT(DISTINCT ts_code) FROM stock_bars WHERE REPLACE(date, '-', '')=?",
            (day,),
        ).fetchone()[0])
        snapshot = conn.execute(
            "SELECT MAX(trade_date) FROM stock_universe_history "
            "WHERE REPLACE(trade_date, '-', '')<=?", (day,),
        ).fetchone()[0]
        if not snapshot:
            return observed >= 285
        universe = {row[0] for row in conn.execute(
            "SELECT DISTINCT ts_code FROM stock_universe_history "
            "WHERE REPLACE(trade_date, '-', '')=?", (market._date_key(snapshot),),
        )}
        actual = {row[0] for row in conn.execute(
            "SELECT DISTINCT ts_code FROM stock_bars WHERE REPLACE(date,'-','')=?", (day,)
        )}
        return bool(universe) and len(universe & actual) >= math.ceil(len(universe) * 0.95)
    finally:
        conn.close()


def _source_etfs_complete(db: Path, expected: str) -> bool:
    required = ("513100", "510300", "512100", "518880", "511380")
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        return all(
            market._date_key(conn.execute(
                "SELECT MAX(date) FROM bars WHERE symbol=?", (symbol,)
            ).fetchone()[0]) >= market._date_key(expected)
            for symbol in required
        )
    finally:
        conn.close()


def _v2_fresh(db: Path, expected: str) -> bool:
    return all(
        _source_fresh(db, table, "trade_date", expected)
        for table in ("cb_daily_raw", "stk_close_daily", "cb_indicator")
    )


def _call_trigger(name: str) -> None:
    result = legacy_trigger.call(name, timeout=1200)
    if result.get("ok") is not True:
        raise RuntimeError(f"legacy {name} failed: {str(result)[:400]}")


def _paper_fresh(expected: str) -> bool:
    from qsim.storage.paper import PAPER_STRATEGIES, PAPER_VERSIONED_CANDIDATES
    from qsim.strategies.migration import paper_enabled

    eligible = [name for name in PAPER_STRATEGIES if paper_enabled(name)]
    eligible.extend(name for name, cfg in PAPER_VERSIONED_CANDIDATES.items()
                    if cfg.get("backfill", True))
    if not eligible:
        return True
    conn = sqlite3.connect(f"file:{(ROOT / 'data' / 'quant.db').resolve()}?mode=ro", uri=True)
    try:
        return all(
            market._date_key(conn.execute(
                "SELECT MAX(date) FROM qsim_snapshots WHERE strategy=?", (name,)
            ).fetchone()[0]) >= market._date_key(expected)
            for name in eligible
        )
    finally:
        conn.close()


def _inside_primary_window(now: dt.datetime, expected: str) -> bool:
    minutes = now.hour * 60 + now.minute
    return now.date().isoformat() == expected and 17 * 60 + 45 <= minutes < 21 * 60 + 45


def reconcile(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(CN)
    expected = market._expected_latest_trade_date(now)
    if not expected:
        raise RuntimeError("trading calendar has no expected session")
    if os.environ.get("QSIM_SCHEDULER_ENABLED", "1") != "0" and _inside_primary_window(now, expected):
        return {"ok": True, "status": "primary_schedule_running", "session": expected}

    from qsim.services import native_market
    if native_market.enabled():
        result = native_market.run()
        from qsim.services.market_quality import check
        quality = check(expected, require_publication=True)
        if not quality["ok"]:
            raise RuntimeError("行情质量检查失败：" + "；".join(quality["issues"]))
        actions = ["native-market"] if result["status"] == "updated" else []
        if os.environ.get("QSIM_PAPER_AUTORUN", "1") == "0":
            return {"ok": True, "status": "collected_paper_blocked", "session": expected,
                    "actions": actions, "quality": quality, "paper": "BLOCKED"}
        if os.environ.get("QSIM_FORMAL_PAPER", "0") == "1" or not _paper_fresh(expected):
            from qsim.services.api.app import _run_paper_job
            _run_paper_job()
            if not _paper_fresh(expected):
                raise RuntimeError(f"paper settlement remains stale for {expected}")
            actions.append("paper")
        return {"ok": True, "status": "repaired" if actions else "up_to_date",
                "session": expected, "actions": actions, "quality": quality}

    actions: list[str] = []
    source = LEGACY_ROOT / "data" / "quant.db"
    if (not _source_fresh(source, "stock_bars", "date", expected)
            or not _source_stock_coverage(source, expected)):
        _call_trigger("fetch-stocks")
        actions.append("fetch-stocks")
        if (not _source_fresh(source, "stock_bars", "date", expected)
                or not _source_stock_coverage(source, expected)):
            raise RuntimeError(f"legacy stock bars remain incomplete after fetch: {expected}")
    if (not _source_fresh(source, "bars", "date", expected)
            or not _source_etfs_complete(source, expected)):
        _call_trigger("run-daily")
        actions.append("run-daily")
        if (not _source_fresh(source, "bars", "date", expected)
                or not _source_etfs_complete(source, expected)):
            raise RuntimeError(f"legacy ETF bars remain incomplete after settlement: {expected}")

    cb = cb_etl_runner.run()
    if cb["status"] == "updated":
        actions.append("cb-etl")

    status = market.latest_sync_status()
    datasets = status.get("datasets") or {}
    target_v2 = ROOT / "data" / "v2_market.db"
    sync_needed = bool(actions) or any(
        (datasets.get(name) or {}).get("freshness") != "fresh"
        for name in ("quant.db", "v2_market.db")
    ) or not _v2_fresh(target_v2, expected) or not market.market_data_gate_status().get("ok")
    if sync_needed:
        result = market.sync_legacy_market_data()
        if not result.get("ok"):
            raise RuntimeError(f"market sync failed: {str(result.get('error'))[:400]}")
        actions.append("sync")

    status = market.latest_sync_status()
    if any((status.get("datasets", {}).get(name) or {}).get("freshness") != "fresh"
           for name in ("quant.db", "v2_market.db")):
        raise RuntimeError(f"market data remains stale for {expected}")
    if not _v2_fresh(target_v2, expected):
        raise RuntimeError(f"convertible-bond data remains stale for {expected}")
    if not market.market_data_gate_status().get("ok"):
        raise RuntimeError(f"stock coverage gate failed for {expected}")

    if not _paper_fresh(expected):
        from qsim.services.api.app import _run_paper_job

        _run_paper_job()
        if not _paper_fresh(expected):
            raise RuntimeError(f"paper settlement remains stale for {expected}")
        actions.append("paper")
    return {"ok": True, "status": "repaired" if actions else "up_to_date",
            "session": expected, "actions": actions}


def _save_state(payload: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(STATE)


def main() -> int:
    try:
        from qsim.services.calendar_maintenance import check
        check()
        result = reconcile()
        result["checked_at"] = dt.datetime.now(CN).isoformat(timespec="seconds")
        _save_state(result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        now = dt.datetime.now(CN)
        result = {"ok": False, "status": "error", "error": str(exc)[:400],
                  "checked_at": now.isoformat(timespec="seconds")}
        try:
            previous = json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        last_notified = previous.get("last_notified_at")
        try:
            since_alert = now - dt.datetime.fromisoformat(last_notified)
        except (TypeError, ValueError):
            since_alert = dt.timedelta.max
        if previous.get("error") != result["error"] or since_alert >= dt.timedelta(hours=6):
            notify.notify("[qsim] 数据自动补跑失败", result["error"])
            result["last_notified_at"] = result["checked_at"]
        else:
            result["last_notified_at"] = last_notified
        _save_state(result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
