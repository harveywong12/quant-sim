"""qsim 网页数据层：裸 SQL 只读访问 quant.db（与旧系统同库同构）。

替换旧系统 app/db.py 的 get_* 封装，供移植的 mobile 页面使用。
所有函数只读（mode=ro），不修改生产数据。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
QUANT_DB = ROOT / "data" / "quant.db"


def conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{QUANT_DB}?mode=ro", uri=True)


def _df(cur, columns) -> pd.DataFrame:
    rows = cur.fetchall()
    return pd.DataFrame(rows, columns=columns)


# ---------- 账户 ----------

def get_account() -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT symbol, initial_capital, cash, shares, pending_signal, "
            "pending_date, last_update_date, td_shares, yd_shares "
            "FROM account LIMIT 1"
        ).fetchone()
    if not row:
        return {}
    keys = ["symbol", "initial_capital", "cash", "shares", "pending_signal",
            "pending_date", "last_update_date", "td_shares", "yd_shares"]
    return dict(zip(keys, row))


def get_factor_account() -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT initial_capital, cash, engine_version FROM factor_account LIMIT 1"
        ).fetchone()
    if not row:
        return {}
    keys = ["initial_capital", "cash", "engine_version"]
    return dict(zip(keys, row))


def get_alpha_accounts() -> pd.DataFrame:
    with conn() as c:
        cur = c.execute(
            "SELECT strategy_name, initial_capital, cash FROM alpha_accounts ORDER BY strategy_name"
        )
        return _df(cur, ["strategy_name", "initial_capital", "cash"])


def get_positions() -> pd.DataFrame:
    with conn() as c:
        cur = c.execute("SELECT ts_code, shares FROM stock_positions")
        return _df(cur, ["ts_code", "shares"])


# ---------- 快照 / 净值 ----------

def get_snapshots(symbol: str) -> pd.DataFrame:
    with conn() as c:
        cur = c.execute(
            "SELECT date, symbol, cash, position_value, total_value "
            "FROM snapshots WHERE symbol=? ORDER BY date",
            (symbol,),
        )
        return _df(cur, ["date", "symbol", "cash", "position_value", "total_value"])


def get_factor_snapshots() -> pd.DataFrame:
    with conn() as c:
        cur = c.execute("SELECT date, total_value FROM factor_snapshots ORDER BY date")
        return _df(cur, ["date", "total_value"])


def get_alpha_snapshots(strategy_name: str | None = None) -> pd.DataFrame:
    with conn() as c:
        if strategy_name:
            cur = c.execute(
                "SELECT date, total_value FROM alpha_snapshots "
                "WHERE strategy_name=? ORDER BY date",
                (strategy_name,),
            )
        else:
            cur = c.execute("SELECT date, total_value FROM alpha_snapshots ORDER BY date")
        return _df(cur, ["date", "total_value"])


def get_race_snapshots(strategy_name: str | None = None) -> pd.DataFrame:
    with conn() as c:
        if strategy_name:
            cur = c.execute(
                "SELECT date, total_value FROM race_snapshots "
                "WHERE strategy_name=? ORDER BY date",
                (strategy_name,),
            )
        else:
            cur = c.execute("SELECT date, total_value FROM race_snapshots ORDER BY date")
        return _df(cur, ["date", "total_value"])


def get_realtime_nav() -> pd.DataFrame:
    with conn() as c:
        cur = c.execute("SELECT strategy, ts, value FROM realtime_nav ORDER BY ts")
        return _df(cur, ["strategy", "ts", "value"])


def get_meta_nav(mode: str) -> pd.DataFrame:
    with conn() as c:
        cur = c.execute(
            "SELECT date, nav AS total_value FROM meta_nav WHERE mode=? ORDER BY date",
            (mode,),
        )
        return _df(cur, ["date", "total_value"])


def get_meta_state(mode: str) -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT state_json FROM meta_state WHERE mode=?", (mode,)
        ).fetchone()
    if not row or not row[0]:
        return {}
    import json
    try:
        return json.loads(row[0])
    except Exception:
        return {}


# ---------- 交易 ----------

def get_trades() -> pd.DataFrame:
    with conn() as c:
        cur = c.execute("SELECT * FROM trades ORDER BY date DESC, id DESC")
        return _df(cur, [d[0] for d in cur.description])


def get_factor_trades() -> pd.DataFrame:
    with conn() as c:
        cur = c.execute("SELECT * FROM factor_trades ORDER BY date DESC")
        return _df(cur, [d[0] for d in cur.description])


def get_alpha_trades(strategy_name: str | None = None) -> pd.DataFrame:
    with conn() as c:
        if strategy_name:
            cur = c.execute(
                "SELECT * FROM alpha_trades WHERE strategy_name=? ORDER BY date DESC",
                (strategy_name,),
            )
        else:
            cur = c.execute("SELECT * FROM alpha_trades ORDER BY date DESC")
        return _df(cur, [d[0] for d in cur.description])


def get_race_trades(strategy_name: str | None = None) -> pd.DataFrame:
    with conn() as c:
        if strategy_name:
            cur = c.execute(
                "SELECT * FROM race_trades WHERE strategy_name=? ORDER BY date DESC",
                (strategy_name,),
            )
        else:
            cur = c.execute("SELECT * FROM race_trades ORDER BY date DESC")
        return _df(cur, [d[0] for d in cur.description])


# ---------- 辅助 ----------

def get_stock_info() -> pd.DataFrame:
    with conn() as c:
        cur = c.execute("SELECT ts_code, name FROM stock_info")
        return _df(cur, ["ts_code", "name"])


def get_event_logs(limit: int = 6) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT ts, level, category, message FROM event_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"ts": r[0], "level": r[1], "category": r[2], "message": r[3]} for r in rows]


def bar_range(symbol: str) -> tuple[str | None, str | None]:
    with conn() as c:
        row = c.execute(
            "SELECT MIN(date), MAX(date) FROM bars WHERE symbol=?", (symbol,)
        ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def last_bar_date(symbol: str) -> str | None:
    with conn() as c:
        row = c.execute(
            "SELECT MAX(date) FROM bars WHERE symbol=?", (symbol,)
        ).fetchone()
    return row[0] if row else None


def get_last_close(code6: str) -> float | None:
    """标的最近收盘价（优先 bars，回退 stock_bars 前六位匹配）。"""
    with conn() as c:
        row = c.execute(
            "SELECT close FROM bars WHERE symbol=? ORDER BY date DESC LIMIT 1", (code6,)
        ).fetchone()
        if row:
            return float(row[0])
        row = c.execute(
            "SELECT close FROM stock_bars WHERE ts_code LIKE ? ORDER BY date DESC LIMIT 1",
            (f"{code6}%",),
        ).fetchone()
        return float(row[0]) if row else None


def get_realtime_prices() -> dict[str, dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT code, price, pre_close, name, open, high, low, volume "
            "FROM realtime_price"
        ).fetchall()
    out = {}
    for r in rows:
        out[str(r[0])] = {
            "price": r[1], "pre_close": r[2], "name": r[3], "open": r[4],
            "high": r[5], "low": r[6], "volume": r[7],
        }
    return out


def get_updated_at() -> str | None:
    with conn() as c:
        row = c.execute(
            "SELECT MAX(updated_at) FROM realtime_price"
        ).fetchone()
    return row[0] if row else None


def data_version() -> str:
    """轻量版本号：自动刷新轮询端点用（版本变化才整页 reload）。"""
    with conn() as c:
        parts = []
        for table, col in (("bars", "date"), ("stock_bars", "date"),
                           ("snapshots", "date"), ("realtime_nav", "ts"),
                           ("event_log", "ts")):
            try:
                row = c.execute(f"SELECT MAX({col}), COUNT(*) FROM {table}").fetchone()
                parts.append(f"{table}:{row[0]}:{row[1]}")
            except Exception:
                pass
    return "|".join(parts)


def get_strategy_accounts() -> pd.DataFrame:
    with conn() as c:
        cur = c.execute(
            "SELECT id, strategy_name, symbol, initial_capital, cash, shares, "
            "pending_signal, pending_date FROM strategy_accounts ORDER BY id"
        )
        return _df(cur, ["id", "strategy_name", "symbol", "initial_capital",
                         "cash", "shares", "pending_signal", "pending_date"])


if __name__ == "__main__":
    a = get_account()
    print("account:", {k: a.get(k) for k in ("symbol", "cash", "shares")})
    print("snapshots:", len(get_snapshots("512100")))
    print("meta_nav c:", len(get_meta_nav("c")))
    print("realtime_nav strategies:", get_realtime_nav()["strategy"].nunique())
    print("trades:", len(get_trades()))
    print("version:", data_version()[:80])


# ---------- 持仓/版本（总览详情用） ----------

def get_alpha_positions(strategy_name: str | None = None) -> pd.DataFrame:
    with conn() as c:
        if strategy_name:
            cur = c.execute(
                "SELECT * FROM alpha_positions WHERE strategy_name=?",
                (strategy_name,),
            )
        else:
            cur = c.execute("SELECT * FROM alpha_positions")
        return _df(cur, [d[0] for d in cur.description])


def get_factor_version() -> str:
    with conn() as c:
        row = c.execute("SELECT engine_version FROM factor_account LIMIT 1").fetchone()
    return str(row[0]) if row and row[0] else "v1"


def get_alpha_versions() -> dict[str, str]:
    with conn() as c:
        rows = c.execute(
            "SELECT strategy_name, engine_version FROM alpha_accounts"
        ).fetchall()
    return {r[0]: str(r[1] or "v1") for r in rows}





def get_realtime_prices_full() -> pd.DataFrame:
    with conn() as c:
        cur = c.execute(
            "SELECT code, price, pre_close, name, open, high, low, volume, "
            "updated_at FROM realtime_price"
        )
        return _df(cur, [d[0] for d in cur.description])


def get_checkpoints(limit: int = 5) -> pd.DataFrame:
    with conn() as c:
        cur = c.execute(
            "SELECT * FROM process_checkpoints ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return _df(cur, [d[0] for d in cur.description])


def get_ai_reports(limit: int = 5) -> pd.DataFrame:
    with conn() as c:
        cur = c.execute(
            "SELECT date, report_json, model, created_at "
            "FROM ai_reports ORDER BY date DESC LIMIT ?", (limit,)
        )
        return _df(cur, ["date", "report_json", "model", "created_at"])


def get_pilot_directive() -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT payload FROM pilot_directive ORDER BY date DESC LIMIT 1"
        ).fetchone()
    if not row or not row[0]:
        return {}
    import json
    try:
        return json.loads(row[0])
    except Exception:
        return {}


# ---------- 热力图历史基准 ----------

def get_bars_for_heatmap(symbol: str) -> pd.DataFrame:
    with conn() as c:
        cur = c.execute(
            "SELECT date, close FROM bars WHERE symbol=? AND close>0 ORDER BY date",
            (symbol,),
        )
        return _df(cur, ["date", "close"])


def get_stock_bars_for_heatmap(ts_code: str) -> pd.DataFrame:
    with conn() as c:
        cur = c.execute(
            "SELECT date, close FROM stock_bars WHERE ts_code=? AND close>0 "
            "ORDER BY date", (ts_code,),
        )
        return _df(cur, ["date", "close"])
