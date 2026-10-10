"""Run the legacy convertible-bond ETL from the accessible qsim runtime.

The legacy unit points at a root-only shell script.  This runner reuses its
ETL functions while keeping credentials out of command lines and logs.
"""

from __future__ import annotations
from qsim.services.mutation_lock import serialized_mutation


import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from qsim.market.legacy_adapter import _date_key, _expected_latest_trade_date
from qsim.services import notify

LEGACY_ROOT = Path(os.environ.get("QSIM_LEGACY_ROOT", "/srv/quant-sim"))
DB = LEGACY_ROOT / "data" / "v2_market.db"


def _watermark(conn: sqlite3.Connection, table: str) -> str | None:
    return conn.execute(f"SELECT MAX(trade_date) FROM {table}").fetchone()[0]


def _coverage(conn: sqlite3.Connection, day: str) -> dict[str, int]:
    return {
        table: int(conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE trade_date=?", (day,)
        ).fetchone()[0])
        for table in ("cb_daily_raw", "stk_close_daily", "cb_indicator")
    }


def _complete(conn: sqlite3.Connection, day: str) -> tuple[bool, dict[str, int]]:
    counts = _coverage(conn, day)
    raw = counts["cb_daily_raw"]
    complete = (
        raw >= 100
        and counts["stk_close_daily"] >= 100
        and counts["cb_indicator"] >= int(raw * 0.9)
    )
    return complete, counts


@serialized_mutation
def run() -> dict:
    from qsim.services import native_market
    if native_market.enabled():
        return native_market.run()
    raise RuntimeError("legacy ETL 源码未提供且 HTTP 传输未审计；请使用经验证的 native HTTPS 行情入口")


def main() -> int:
    try:
        print(json.dumps(run(), ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        notify.notify("[qsim] 可转债数据更新失败", str(exc)[:400])
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
