"""qsim 盘中实时估值：新浪实时价（tushare get_realtime_quotes）→ 持仓收益。

8503 同款机制（TushareFeed 即新浪源）。qsim 用它实时更新持仓估值，
盘中每分钟可刷，页面持仓收益实时变化。

数据边界：只写 qsim 自己的实时价缓存表（qsim_realtime），不碰旧系统表。
"""

from __future__ import annotations
from qsim.services.mutation_lock import serialized_mutation


import datetime as dt
import json
import logging
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("qsim.realtime")

ROOT = Path(__file__).resolve().parents[2]
QUANT_DB = ROOT / "data" / "quant.db"
CALENDAR_FILE = Path(
    os.environ.get("QSIM_CALENDAR_FILE", str(ROOT / "config" / "trading-calendar.json"))
)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

SCHEMA = """
CREATE TABLE IF NOT EXISTS qsim_realtime (
    symbol TEXT PRIMARY KEY,
    price REAL NOT NULL,
    open_price REAL NOT NULL DEFAULT 0.0,
    pre_close REAL NOT NULL DEFAULT 0.0,
    quote_date TEXT NOT NULL DEFAULT '',
    quote_time TEXT NOT NULL DEFAULT '',
    name TEXT DEFAULT '',
    updated_at TEXT NOT NULL
);
"""

REALTIME_NAV_SCHEMA = """
CREATE TABLE IF NOT EXISTS qsim_realtime_nav (
    strategy TEXT NOT NULL,
    ts TEXT NOT NULL,
    total_value REAL NOT NULL,
    cash REAL NOT NULL,
    position_value REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY(strategy, ts)
);
CREATE INDEX IF NOT EXISTS idx_qsim_realtime_nav_strategy_ts
ON qsim_realtime_nav(strategy, ts);
"""
PROVISIONAL_PAPER = ROOT / "artifacts" / "provisional_paper" / "latest.json"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(QUANT_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    _ensure_realtime_schema(conn)
    return conn


def _ensure_realtime_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA + REALTIME_NAV_SCHEMA)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(qsim_realtime)")}
    if "open_price" not in columns:
        conn.execute(
            "ALTER TABLE qsim_realtime ADD COLUMN open_price REAL NOT NULL DEFAULT 0.0"
        )
    if "quote_date" not in columns:
        conn.execute(
            "ALTER TABLE qsim_realtime ADD COLUMN quote_date TEXT NOT NULL DEFAULT ''"
        )
    if "quote_time" not in columns:
        conn.execute(
            "ALTER TABLE qsim_realtime ADD COLUMN quote_time TEXT NOT NULL DEFAULT ''"
        )


def _held_symbols() -> list[str]:
    """当前模拟盘持仓标的（qsim_positions 去重）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT p.ts_code FROM qsim_positions p "
            "WHERE p.shares>0"
        ).fetchall()
    return [str(r[0]) for r in rows]


def _sina_codes(codes: list[str]) -> list[str]:
    """转新浪代码格式：6位ETF原样，股票转 sh/sz 前缀。"""
    out = []
    for c in codes:
        c6 = str(c)[:6]
        if c6.startswith(("5", "1")):
            out.append(c6)  # ETF
        elif c6.startswith(("6", "9")):
            out.append(f"sh{c6}")
        elif c6.startswith(("0", "3")):
            out.append(f"sz{c6}")
        else:
            out.append(c6)
    return out


def _position_value(
    positions: list[tuple[str, int]], prices: dict[str, float],
) -> float | None:
    """Return a fully priced position value, never treating a missing quote as 0."""
    total = 0.0
    for symbol, shares in positions:
        price = prices.get(str(symbol)[:6])
        if price is None:
            return None
        total += float(price) * int(shares)
    return total


def _pending_provisional_names() -> set[str]:
    try:
        from qsim.strategies.migration import ACTIVE_MIGRATIONS, migration_for
        return {
            item.name for item in ACTIVE_MIGRATIONS
            if migration_for(item.name).state.value == "parity_pending"
        }
    except Exception:
        return set()


def _record_realtime_nav(conn: sqlite3.Connection, timestamp: str) -> int:
    """Append presentation-only live NAV points for official and pending ledgers.

    This is intentionally a separate intraday series.  It does not update
    qsim_accounts, qsim_snapshots, positions, trades, or any old-system table.
    """
    quote_rows = conn.execute("SELECT symbol, price FROM qsim_realtime").fetchall()
    prices = {str(row["symbol"])[:6]: float(row["price"]) for row in quote_rows}
    account_rows = conn.execute("SELECT id, strategy, cash FROM qsim_accounts").fetchall()
    position_rows = conn.execute(
        "SELECT account_id, ts_code, shares FROM qsim_positions WHERE shares>0"
    ).fetchall()
    positions_by_account: dict[int, list[tuple[str, int]]] = {}
    for row in position_rows:
        positions_by_account.setdefault(int(row["account_id"]), []).append(
            (str(row["ts_code"]), int(row["shares"]))
        )

    records: dict[str, tuple[float, float, float, str]] = {}
    for row in account_rows:
        positions = positions_by_account.get(int(row["id"]), [])
        position_value = _position_value(positions, prices)
        if position_value is None:
            continue
        cash = float(row["cash"] or 0)
        records[str(row["strategy"])] = (cash + position_value, cash, position_value, "official")

    # Pending strategies are displayed from their isolated provisional ledger.
    # Overlay only those names, so live visualization follows the same ledger
    # selection as the dashboard without mutating promotion evidence.
    pending = _pending_provisional_names()
    if pending and PROVISIONAL_PAPER.exists():
        try:
            payload = json.loads(PROVISIONAL_PAPER.read_text(encoding="utf-8"))
            for item in payload.get("strategies") or []:
                strategy = str(item.get("strategy") or "")
                if strategy not in pending:
                    continue
                positions = [
                    (str(position.get("ts_code") or ""), int(position.get("shares") or 0))
                    for position in item.get("positions") or []
                    if int(position.get("shares") or 0) > 0
                ]
                position_value = _position_value(positions, prices)
                if position_value is None:
                    continue
                cash = float(item.get("cash") or 0)
                records[strategy] = (cash + position_value, cash, position_value, "provisional")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("could not load provisional live NAV: %s", exc)

    for strategy, (total_value, cash, position_value, source) in records.items():
        conn.execute(
            "INSERT OR REPLACE INTO qsim_realtime_nav "
            "(strategy, ts, total_value, cash, position_value, source) VALUES (?,?,?,?,?,?)",
            (strategy, timestamp, total_value, cash, position_value, source),
        )
    return len(records)


@serialized_mutation
def fetch_realtime_quotes(codes: list[str]) -> dict[str, dict]:
    """Fetch and persist Sina quotes for explicit symbols, including open."""
    import tushare as ts
    codes = list(dict.fromkeys(str(code) for code in codes if code))
    if not codes:
        return {}

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    df = ts.get_realtime_quotes(_sina_codes(codes))
    if df is None or not len(df):
        return {}

    quotes: dict[str, dict] = {}
    with _conn() as c:
        for r in df.itertuples():
            try:
                code6 = str(r.code)[-6:]
                price = float(r.price)
                open_price = _to_float(getattr(r, "open", 0.0))
                if price <= 0 and open_price <= 0:
                    continue
                quote = {
                    "symbol": code6,
                    "price": price,
                    "open_price": open_price,
                    "pre_close": _to_float(r.pre_close),
                    "quote_date": str(getattr(r, "date", "") or ""),
                    "quote_time": str(getattr(r, "time", "") or ""),
                    "name": str(getattr(r, "name", "") or ""),
                    "updated_at": now,
                }
                c.execute(
                    "INSERT OR REPLACE INTO qsim_realtime "
                    "(symbol,price,open_price,pre_close,quote_date,quote_time,name,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        code6, price, open_price, quote["pre_close"],
                        quote["quote_date"], quote["quote_time"], quote["name"], now,
                    ),
                )
                quotes[code6] = quote
            except (TypeError, ValueError):
                continue
        c.commit()
    return quotes


@serialized_mutation
def refresh_realtime() -> dict:
    """拉新浪实时价 → 写 qsim_realtime（幂等）。返回统计。"""
    if not is_trading_session():
        return {"ok": True, "skipped": "outside-session"}
    codes = _held_symbols()
    if not codes:
        return {"ok": True, "skipped": "no-positions", "codes": 0}
    quotes = fetch_realtime_quotes(codes)
    if not quotes:
        return {"ok": False, "error": "realtime quotes failed", "codes": len(codes)}
    now = max(str(quote["updated_at"]) for quote in quotes.values())
    with _conn() as c:
        nav_updated = _record_realtime_nav(c, now)
        c.commit()
    return {
        "ok": True, "codes": len(codes), "updated": len(quotes),
        "nav_updated": nav_updated, "at": now,
    }


def _to_float(v) -> float:
    try:
        return float(v) if v not in (None, "", "None") else 0.0
    except (TypeError, ValueError):
        return 0.0


def latest_prices() -> dict[str, dict]:
    """最新实时价（页面持仓收益用）。"""
    with _conn() as c:
        rows = c.execute("SELECT * FROM qsim_realtime").fetchall()
    return {str(r["symbol"]): dict(r) for r in rows}


def is_trading_session(now: dt.datetime | None = None) -> bool:
    """A股交易时段，依据版本化交易日历排除节假日和周末。"""
    now = now or dt.datetime.now(SHANGHAI_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI_TZ)
    else:
        now = now.astimezone(SHANGHAI_TZ)
    if not _is_trading_day(now.date()):
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= mins <= 11 * 60 + 30) or (13 * 60 <= mins <= 15 * 60)


def _is_trading_day(day: dt.date) -> bool:
    """Return the CN-A trading-day decision from the maintained calendar.

    The calendar is deliberately fail-closed: a missing, invalid, or stale
    file must not cause a holiday to be treated as an open market day.
    """
    try:
        payload = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
        valid_from = dt.date.fromisoformat(str(payload["valid_from"]))
        valid_through = dt.date.fromisoformat(str(payload["valid_through"]))
        if not valid_from <= day <= valid_through:
            logger.error("trading calendar does not cover %s", day.isoformat())
            return False
        if day.weekday() >= 5:
            return False
        closed_dates = {str(value) for value in payload["closed_dates"]}
        return day.isoformat() not in closed_dates
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.error("trading calendar unavailable: %s", exc)
        return False


def next_trading_day(day: dt.date) -> dt.date | None:
    """Return the next maintained CN-A session, failing closed."""
    for offset in range(1, 32):
        candidate = day + dt.timedelta(days=offset)
        if _is_trading_day(candidate):
            return candidate
    return None


if __name__ == "__main__":
    import json
    print(json.dumps(refresh_realtime(), ensure_ascii=False))
