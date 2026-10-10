"""Deterministic daily-flow verification for qsim (no LLM).

Every check emits {check, status: pass/warn/fail, detail}. Query failures
degrade to warn instead of crashing — a broken check must not hide itself
behind an exception. The AI review only interprets these results.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from qsim.services import notify, realtime

logger = logging.getLogger("qsim.qc")

ROOT = Path(__file__).resolve().parents[2]
QUANT_DB = ROOT / "data" / "quant.db"
SHADOW_DIR = ROOT / "artifacts" / "shadow"
META_SHADOW = ROOT / "artifacts" / "meta_shadow_v1" / "latest.json"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _fail(check: str, detail: str) -> dict:
    return {"check": check, "status": "fail", "detail": detail}


def _pass(check: str, detail: str) -> dict:
    return {"check": check, "status": "pass", "detail": detail}


def _warn(check: str, detail: str) -> dict:
    return {"check": check, "status": "warn", "detail": detail}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(QUANT_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_expected_day(today: dt.date) -> dt.date:
    day = today
    for _ in range(10):
        if realtime._is_trading_day(day):
            return day
        day -= dt.timedelta(days=1)
    return today


def _latest_bar_date(conn: sqlite3.Connection, table: str) -> str | None:
    try:
        row = conn.execute(f"SELECT MAX(date) AS d FROM {table}").fetchone()
        return str(row["d"]) if row and row["d"] else None
    except sqlite3.Error:
        return None


def _active_strategies(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT strategy FROM qsim_accounts WHERE status='active'"
    ).fetchall()
    return [str(r["strategy"]) for r in rows]


PROVISIONAL_LATEST = ROOT / "artifacts" / "provisional_paper" / "latest.json"


def _provisional_as_of() -> str | None:
    try:
        payload = json.loads(PROVISIONAL_LATEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    strategies = payload.get("strategies") or []
    dates = [str(item.get("as_of") or "") for item in strategies if isinstance(item, dict)]
    dates = [d for d in dates if d]
    return min(dates) if dates else None


def _snapshot_coverage(
    conn: sqlite3.Connection, expected: str
) -> tuple[bool, str]:
    strategies = _active_strategies(conn)
    if not strategies:
        return False, "qsim_accounts 无 active 账户"
    problems: list[str] = []
    latest_values: list[str] = []
    provisional_asof = None
    for name in strategies:
        row = conn.execute(
            "SELECT MAX(date) AS d, COUNT(*) AS n FROM qsim_snapshots "
            "WHERE strategy=?",
            (name,),
        ).fetchone()
        latest = str(row["d"]) if row and row["d"] else None
        if latest and latest >= expected:
            latest_values.append(latest)
            continue
        # Official snapshots lag: acceptable when the strategy is carried by
        # the provisional ledger and that ledger has reached the expected day.
        if latest is None or latest < expected:
            if provisional_asof is None:
                provisional_asof = _provisional_as_of()
            if provisional_asof and provisional_asof >= expected:
                latest_values.append(provisional_asof)
                continue
        if not latest:
            problems.append(f"{name} 无快照")
        else:
            problems.append(f"{name} 仅至 {latest}")
    if problems:
        return False, "；".join(problems[:8]) + f"，期望 {expected}"
    return True, f"{len(strategies)} 策略连续记账至 {min(latest_values)}"


def _realtime_nav_freshness(
    conn: sqlite3.Connection, expected: str
) -> tuple[bool, str]:
    row = conn.execute(
        "SELECT MAX(ts) AS t FROM qsim_realtime_nav"
    ).fetchone()
    latest = str(row["t"]) if row and row["t"] else None
    if not latest:
        return False, "qsim_realtime_nav 无记录"
    if latest[:10] >= expected:
        return True, f"实时净值最新 {latest[:16]}"
    return False, f"实时净值停在 {latest[:16]}，期望覆盖 {expected}"


def _dual_source_check(
    conn: sqlite3.Connection, expected: str
) -> dict:
    """qsim_realtime (Sina) vs daily bars cross-check by same-day return."""
    tolerance = 0.005
    day_prefix = f"{expected[:4]}-{expected[4:6]}-{expected[6:8]}"
    rows = conn.execute(
        "SELECT symbol, price, pre_close, updated_at FROM qsim_realtime"
    ).fetchall()
    if not rows:
        return _pass("双源对账", "无实时价快照，跳过")
    compared, mismatched, skipped_stale = 0, [], 0
    for r in rows:
        code = str(r["symbol"])
        rt_price, rt_pre = float(r["price"] or 0), float(r["pre_close"] or 0)
        if rt_price <= 0 or rt_pre <= 0:
            continue
        stamp = str(r["updated_at"] or "")
        if not stamp.startswith(day_prefix) or stamp[11:16] < "14:55":
            skipped_stale += 1
            continue
        bars = conn.execute(
            "SELECT date, close FROM bars WHERE symbol=? AND date<=? "
            "ORDER BY date DESC LIMIT 2", (code, expected)).fetchall()
        if len(bars) < 2 or str(bars[0]["date"]) != expected:
            bars = conn.execute(
                "SELECT date, close FROM stock_bars WHERE ts_code LIKE ? "
                "AND date<=? ORDER BY date DESC LIMIT 2",
                (f"%{code}%", expected)).fetchall()
        if len(bars) < 2 or float(bars[1]["close"] or 0) <= 0:
            continue
        rt_ret = rt_price / rt_pre - 1
        bar_ret = float(bars[0]["close"]) / float(bars[1]["close"]) - 1
        compared += 1
        if abs(rt_ret - bar_ret) > tolerance:
            mismatched.append(f"{code} 实时{rt_ret:+.2%}/日线{bar_ret:+.2%}")
    if mismatched:
        return _fail(
            "双源对账",
            f"{len(mismatched)}/{compared} 只偏差超{tolerance:.1%}: "
            + "；".join(mismatched[:4]),
        )
    if compared == 0:
        return _warn("双源对账", f"无可比数据（{skipped_stale} 只快照非收盘时段）")
    return _pass("双源对账", f"{compared} 只实时/日线收益率一致")


def _trade_timing_check(conn: sqlite3.Connection, today: str) -> dict:
    rows = conn.execute(
        "SELECT strategy, symbol, side, price, shares, date FROM qsim_trades "
        "WHERE date=?", (today,)).fetchall()
    if not rows:
        return _pass("成交记录", f"{today} 无成交（空仓/信号未触发属正常）")
    buys = sum(1 for r in rows if r["side"] == "BUY")
    sells = sum(1 for r in rows if r["side"] == "SELL")
    return _pass(
        "成交记录",
        f"{len(rows)} 笔成交（买{buys}/卖{sells}），T+1 由 paper_daily 保证",
    )


def _event_log_check(conn: sqlite3.Connection, today: str) -> dict:
    prefix = f"{today[:4]}-{today[4:6]}-{today[6:8]}"
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM event_log WHERE level='ERROR' "
            "AND ts LIKE ?", (prefix + "%",)).fetchone()["n"]
    except sqlite3.Error as exc:
        return _warn("今日告警", f"检查失败: {exc}")
    if n:
        return _warn("今日告警", f"{int(n)} 条 ERROR")
    return _pass("今日告警", "无 ERROR")


def _sync_check(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT run_id, status, finished_at FROM qsim_data_sync_runs "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return _warn("数据同步", "qsim_data_sync_runs 无记录")
    if row["status"] not in ("success", "rolled_back"):
        return _fail("数据同步", f"最近同步 {row['run_id']} 状态 {row['status']}")
    return _pass("数据同步", f"最近同步 {row['run_id']} ({row['status']})")


def _shadow_artifact_check(expected: str) -> dict:
    files = sorted(SHADOW_DIR.glob("*.json"))
    if not files:
        return _warn("影子对账", "artifacts/shadow 无对账文件")
    latest = files[-1]
    stamp = latest.stem
    day = stamp[:8]
    if day >= expected:
        try:
            payload = json.loads(latest.read_text(encoding="utf-8"))
            ok = payload.get("all_ok")
            if ok is True:
                return _pass("影子对账", f"{stamp} all_ok=True")
            return _fail("影子对账", f"{stamp} all_ok={ok}")
        except (OSError, ValueError) as exc:
            return _warn("影子对账", f"{stamp} 解析失败: {exc}")
    return _fail("影子对账", f"最新对账 {stamp} 落后于期望 {expected}")


def _meta_shadow_check() -> dict:
    if not META_SHADOW.is_file():
        return _warn("meta影子", "meta_shadow_v1/latest.json 不存在")
    try:
        payload = json.loads(META_SHADOW.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _warn("meta影子", f"解析失败: {exc}")
    if payload.get("mode") == "meta_shadow_v1" and payload.get("state") == "shadow_only":
        return _pass("meta影子", "meta_shadow_v1 / shadow_only")
    return _fail("meta影子", f"mode={payload.get('mode')} state={payload.get('state')}")


def run_checks(date: str | None = None) -> list[dict]:
    """Run all checks for the given YYYYMMDD (default: today, fail-closed)."""
    today = dt.datetime.now(SHANGHAI_TZ).date()
    if date:
        try:
            today = dt.date.fromisoformat(f"{date[:4]}-{date[4:6]}-{date[6:8]}")
        except ValueError:
            pass
    expected = _latest_expected_day(today)
    expected_s = expected.strftime("%Y%m%d")
    is_trading_day = expected == today

    checks: list[dict] = []
    if not is_trading_day:
        checks.append(_pass("交易日判断", f"{today} 非交易日，跳过流程检查"))

    conn = _conn()
    try:
        if is_trading_day:
            bar = _latest_bar_date(conn, "bars")
            stock_bar = _latest_bar_date(conn, "stock_bars")
            problems = []
            if not bar or bar < expected_s:
                problems.append(f"bars 最新 {bar}")
            if not stock_bar or stock_bar < expected_s:
                problems.append(f"stock_bars 最新 {stock_bar}")
            if problems:
                checks.append(_fail("数据新鲜度", "；".join(problems) + f"，期望 {expected_s}"))
            else:
                checks.append(_pass("数据新鲜度", f"bars/stock_bars 均至 {expected_s}"))

            try:
                ok, detail = _snapshot_coverage(conn, expected_s)
                checks.append((_pass if ok else _fail)("记账完整·qsim", detail))
            except Exception as exc:
                checks.append(_warn("记账完整·qsim", f"检查失败: {exc}"))

            try:
                ok, detail = _realtime_nav_freshness(conn, expected_s)
                checks.append((_pass if ok else _warn)("实时净值", detail))
            except Exception as exc:
                checks.append(_warn("实时净值", f"检查失败: {exc}"))

            try:
                checks.append(_trade_timing_check(conn, expected_s))
            except Exception as exc:
                checks.append(_warn("成交记录", f"检查失败: {exc}"))

            try:
                checks.append(_dual_source_check(conn, expected_s))
            except Exception as exc:
                checks.append(_warn("双源对账", f"检查失败: {exc}"))

            checks.append(_shadow_artifact_check(expected_s))
            checks.append(_meta_shadow_check())

        try:
            checks.append(_sync_check(conn))
        except Exception as exc:
            checks.append(_warn("数据同步", f"检查失败: {exc}"))

        checks.append(_event_log_check(conn, expected_s))
    finally:
        conn.close()
    return checks


def flow_verdict(checks: list[dict]) -> str:
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    if fails:
        return f"fail({len(fails)}项异常)"
    if warns:
        return f"warn({len(warns)}项待关注)"
    return "ok"


def run_daily(date: str | None = None) -> dict:
    """Scheduled entry: run checks, push on fail, return summary."""
    checks = run_checks(date)
    verdict = flow_verdict(checks)
    fails = [c for c in checks if c["status"] == "fail"]
    if fails:
        body = "\n".join(f"[FAIL] {c['check']}: {c['detail']}" for c in fails[:8])
        notify.notify("[qsim] 每日流程核验未通过", body)
    logger.info("qc verdict=%s checks=%d", verdict, len(checks))
    return {"ok": not fails, "verdict": verdict, "checks": checks}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for check in run_checks():
        print(f"[{check['status']:>4}] {check['check']}: {check['detail']}")
