#!/usr/bin/env python3
"""Fail-closed runtime/data readiness check for the two-project migration."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import urllib.request
from pathlib import Path


CN = dt.timezone(dt.timedelta(hours=8))
ROOT = Path(os.environ.get("QSIM_REFACTOR_ROOT", "/srv/quant-sim-refactor"))
LEGACY = Path(os.environ.get("QSIM_LEGACY_ROOT", "/srv/quant-sim"))
CALENDAR = ROOT / "config" / "trading-calendar.json"


def expected_trading_day(now: dt.datetime) -> str:
    closed = set(json.loads(CALENDAR.read_text(encoding="utf-8"))["closed_dates"])
    candidate = now.date()
    if candidate.weekday() < 5 and candidate.isoformat() not in closed:
        if (now.hour, now.minute) < (15, 30):
            candidate -= dt.timedelta(days=1)
    while candidate.weekday() >= 5 or candidate.isoformat() in closed:
        candidate -= dt.timedelta(days=1)
    return candidate.strftime("%Y%m%d")


def max_date(path: Path, table: str, column: str) -> str | None:
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def critical_missing(path: Path, expected: str) -> list[str]:
    if not path.is_file():
        return ["<database-missing>"]
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        codes: set[str] = set()
        for table in ("stock_positions", "alpha_positions"):
            try:
                codes.update(row[0] for row in conn.execute(
                    f"SELECT ts_code FROM {table} WHERE ts_code IS NOT NULL"
                ))
            except sqlite3.Error:
                pass
        for table in ("factor_pending", "alpha_pending"):
            try:
                for row in conn.execute(f"SELECT targets FROM {table}"):
                    codes.update(item.strip() for item in str(row[0] or "").split(",") if item.strip())
            except sqlite3.Error:
                pass
        if not codes:
            return []
        marks = ",".join("?" for _ in codes)
        rows = conn.execute(
            f"SELECT DISTINCT ts_code FROM stock_bars WHERE ts_code IN ({marks}) AND date >= ?",
            (*sorted(codes), expected),
        )
        present = {row[0] for row in rows}
        return sorted(codes - present)
    finally:
        conn.close()


def command_ok(command: list[str]) -> bool:
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def docker_healthy(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--format",
         "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
         name], capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "healthy"


def docker_stopped(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", name],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "exited"


def main() -> int:
    import sys
    sys.path.insert(0, str(ROOT))
    from qsim.services.native_market import enabled
    native = enabled()
    expected = expected_trading_day(dt.datetime.now(CN))
    source = LEGACY / "data" / "quant.db"
    source_v2 = LEGACY / "data" / "v2_market.db"
    target = ROOT / "data" / "quant.db"
    v2 = ROOT / "data" / "v2_market.db"
    result = {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "expected_market_date": expected,
        "services": {
            "qsim_refactor": command_ok(["systemctl", "--user", "is-active", "qsim-refactor.service"]),
            "qsim_refactor_health": False,
            "legacy_trigger_healthy": docker_healthy("quant-sim-trigger"),
            "old_web_stopped": all(docker_stopped(name) for name in
                                   ("quant-sim", "quant-sim-mobile")),
        },
        "timers": {
            name: {
                "enabled": command_ok(["systemctl", "--user", "is-enabled", f"{name}.timer"]),
                "active": command_ok(["systemctl", "--user", "is-active", f"{name}.timer"]),
            }
            for name in ("qsim-cb-etl", "qsim-data-reconcile", "qsim-backup", "qsim-watchdog", "qsim-recovery-drill")
        },
        "data": {
            "legacy_bars": None if native else max_date(source, "bars", "date"),
            "legacy_stock_bars": None if native else max_date(source, "stock_bars", "date"),
            "legacy_critical_missing": None if native else critical_missing(source, expected),
            "legacy_stk_close_daily": None if native else max_date(source_v2, "stk_close_daily", "trade_date"),
            "legacy_cb_daily_raw": None if native else max_date(source_v2, "cb_daily_raw", "trade_date"),
            "legacy_cb_indicator": None if native else max_date(source_v2, "cb_indicator", "trade_date"),
            "refactor_bars": max_date(target, "bars", "date"),
            "refactor_stock_bars": max_date(target, "stock_bars", "date"),
            "refactor_critical_missing": critical_missing(target, expected),
            "refactor_stk_close_daily": max_date(v2, "stk_close_daily", "trade_date"),
            "refactor_cb_daily_raw": max_date(v2, "cb_daily_raw", "trade_date"),
            "refactor_cb_indicator": max_date(v2, "cb_indicator", "trade_date"),
        },
    }
    try:
        with urllib.request.urlopen("http://127.0.0.1:8611/healthz", timeout=3) as response:
            result["services"]["qsim_refactor_health"] = response.status == 200
    except Exception:
        pass
    import sys
    sys.path.insert(0, str(ROOT))
    from qsim.services.operations import readiness
    result["business"] = readiness()
    native = result["business"].get("native_collector", False)
    result["ok"] = all([
        result["business"]["ok"],
        result["services"]["qsim_refactor"],
        result["services"]["qsim_refactor_health"],
        native or result["services"]["legacy_trigger_healthy"],
        result["services"]["old_web_stopped"],
        all(all(item.values()) for item in result["timers"].values()),
        native or result["data"]["legacy_bars"] == expected,
        native or result["data"]["legacy_stock_bars"] == expected,
        native or result["data"]["legacy_critical_missing"] == [],
        native or all(result["data"][f"legacy_{table}"] == expected for table in
            ("stk_close_daily", "cb_daily_raw", "cb_indicator")),
        result["data"]["refactor_bars"] == expected,
        result["data"]["refactor_stock_bars"] == expected,
        result["data"]["refactor_critical_missing"] == [],
        all(result["data"][f"refactor_{table}"] == expected for table in
            ("stk_close_daily", "cb_daily_raw", "cb_indicator")),
    ])
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
