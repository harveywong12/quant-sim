"""qsim 模拟盘存储层：qsim_* 表（与旧系统表共存但隔离）。

模拟盘数据写 qsim 自己的 quant.db 的 qsim_* 表：
  - qsim_accounts   : 策略账户（8 策略各 10 万）
  - qsim_positions  : 每股持仓
  - qsim_snapshots  : 每日净值
  - qsim_trades     : 成交记录（带 code_hash 策略版本）
  - qsim_order_intents: 收盘冻结、次日开盘执行的不可变订单证据

设计要点：
  - 只写 qsim_* 前缀表，绝不触碰旧系统表（数据边界）
  - 每次运行记录 code_hash（hotzone 快照）→ 热插拔版本可追溯
  - 可写连接（模拟盘引擎用）；网页读取走只读
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
QUANT_DB = ROOT / "data" / "quant.db"

# 8 个活跃策略 × 标的映射（与 run_shadow_diff.py 口径一致）
# 单标的 ETF 策略用 symbol；横截面策略（multi_factor/alpha158）用股票池
PAPER_STRATEGIES: dict[str, dict] = {
    "tsmom_vol":      {"symbol": "513100", "kind": "etf",   "initial": 100000.0},
    "rsi_boll":       {"symbol": "518880", "kind": "etf",   "initial": 100000.0},
    "dual_ma":        {"symbol": "510300", "kind": "etf",   "initial": 100000.0},
    "grid_atr":       {"symbol": "512100", "kind": "etf",   "initial": 100000.0},
    "trend_fallback": {"symbol": "518880", "kind": "etf",   "initial": 100000.0, "fallback": "511380"},
    "cb_dual_low":    {"symbol": "511380", "kind": "etf",   "initial": 100000.0},
    # 股票池策略的实际本金可以不同；执行器会按可买一手的最大目标数动态
    # 收缩持仓数，而不是依赖旧系统的固定 22 万起始资金。
    "multi_factor":   {"symbol": None,     "kind": "cross", "initial": 100000.0},
    "alpha158":       {"symbol": None,     "kind": "cross", "initial": 100000.0},
}

# Versioned candidates are intentionally separate from the eight migration
# strategies.  They may run in paper, but cannot overwrite a parity-approved
# ledger or be mistaken for the legacy-compatible implementation.
PAPER_VERSIONED_CANDIDATES: dict[str, dict] = {
    "multi_factor_v3": {
        "symbol": None,
        "kind": "cross",
        "initial": 100000.0,
        "start": "20260801",
        "display_name": "多因子 · v3",
        "parent": "multi_factor",
        "state": "paper_candidate",
    },
    "multi_factor_v4": {
        "symbol": None,
        "kind": "cross",
        "initial": 100000.0,
        "start": "20260902",
        "display_name": "多因子 · v4（非科创板账户）",
        "parent": "multi_factor_v3",
        "state": "paper_candidate",
        # Do not fabricate a paper ledger before its own live observation
        # window. v4 starts with a separate clean account and first freezes at
        # the next monthly signal.
        "backfill": False,
    },
    "dividend_lowvol": {
        "symbol": None,
        "kind": "cross",
        "initial": 100000.0,
        "start": "20260914",
        "display_name": "红利低波",
        "parent": "multi_factor",
        "state": "paper_candidate",
    },
    "multi_asset_mom": {
        "symbol": None,
        "kind": "etf_cross",
        "initial": 100000.0,
        "start": "20260914",
        "display_name": "多资产ETF动量",
        "parent": "tsmom_vol",
        "state": "paper_candidate",
    },
}


def paper_strategy_catalog() -> dict[str, dict]:
    """Return official sleeves plus explicitly isolated version candidates."""
    return {**PAPER_STRATEGIES, **PAPER_VERSIONED_CANDIDATES}

# ETF 名称内置兜底（stock_info 不覆盖 ETF 时用）
ETF_NAMES = {
    "512100": "中证1000ETF", "510300": "沪深300ETF", "510500": "中证500ETF",
    "518880": "黄金ETF", "513100": "纳指ETF", "511380": "可转债ETF",
    "513030": "德国DAX", "511880": "银华日利", "511010": "5年国债ETF",
    "512480": "半导体ETF", "159915": "创业板ETF", "588000": "科创50ETF",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS qsim_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL UNIQUE,
    symbol TEXT,
    initial_capital REAL NOT NULL,
    cash REAL NOT NULL,
    shares INTEGER NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0.0,
    code_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qsim_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES qsim_accounts(id),
    ts_code TEXT NOT NULL,
    shares INTEGER NOT NULL,
    avg_cost REAL NOT NULL DEFAULT 0.0,
    updated_date TEXT NOT NULL,
    UNIQUE(account_id, ts_code)
);

CREATE TABLE IF NOT EXISTS qsim_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    strategy TEXT NOT NULL,
    total_value REAL NOT NULL,
    cash REAL NOT NULL,
    position_value REAL NOT NULL,
    strategy_version TEXT NOT NULL DEFAULT '',
    UNIQUE(date, strategy)
);

CREATE TABLE IF NOT EXISTS qsim_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    shares INTEGER NOT NULL,
    amount REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0.0,
    reason TEXT DEFAULT '',
    code_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS qsim_order_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    execution_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    allocation REAL NOT NULL,
    desired_count INTEGER NOT NULL,
    candidates_json TEXT NOT NULL,
    gate_json TEXT NOT NULL,
    source_market_as_of TEXT NOT NULL,
    model_hash TEXT NOT NULL,
    signal_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    executed_at TEXT,
    execution_json TEXT,
    last_error TEXT,
    UNIQUE(strategy, signal_date)
);

CREATE INDEX IF NOT EXISTS idx_qsim_snapshots_date ON qsim_snapshots(date);
CREATE INDEX IF NOT EXISTS idx_qsim_trades_strategy ON qsim_trades(strategy);
CREATE INDEX IF NOT EXISTS idx_qsim_positions_acct ON qsim_positions(account_id);
CREATE INDEX IF NOT EXISTS idx_qsim_order_intents_execution
ON qsim_order_intents(execution_date, status);

CREATE TABLE IF NOT EXISTS qsim_order_intent_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    reason TEXT NOT NULL,
    prior_payload_json TEXT NOT NULL,
    revised_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qsim_order_intent_revisions_intent
ON qsim_order_intent_revisions(intent_id);
"""


from qsim.services.distributions import LEDGER_SCHEMA
SCHEMA += LEDGER_SCHEMA


def get_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    """可写连接（模拟盘引擎用）。"""
    path = Path(db_path) if db_path else QUANT_DB
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        # A paper connection must not silently switch the market database to WAL.
        # Existing incompatible databases require an isolated migration/release.
        from qsim.storage.publication import require_rollback
        require_rollback(conn)
        conn.execute("PRAGMA busy_timeout=30000")
    except BaseException:
        conn.close()
        raise
    return conn


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    """建 qsim_* 表（幂等）。"""
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


def seed_accounts(conn: sqlite3.Connection | None = None,
                  strategies: dict | None = None) -> int:
    """初始开户：每个策略一个账户，各 10 万（幂等，已存在则跳过）。"""
    import datetime as dt
    own = conn is None
    conn = conn or get_conn()
    today = dt.date.today().strftime("%Y%m%d")
    created = 0
    try:
        for name, cfg in (strategies or PAPER_STRATEGIES).items():
            exists = conn.execute(
                "SELECT 1 FROM qsim_accounts WHERE strategy=?", (name,)
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO qsim_accounts "
                "(strategy, symbol, initial_capital, cash, shares, avg_cost, "
                " code_hash, status, created_date) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (name, cfg.get("symbol"), cfg["initial"], cfg["initial"],
                 0, 0.0, "", "active", today),
            )
            created += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return created


def list_accounts(conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    conn = conn or get_conn()
    try:
        rows = conn.execute("SELECT * FROM qsim_accounts ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    init_schema()
    n = seed_accounts()
    print(f"开户完成: {n} 个新账户")
    for a in list_accounts():
        print(f"  {a['strategy']:<16} {a['symbol'] or '股票池':<8} "
              f"初始={a['initial_capital']:.0f} 现金={a['cash']:.0f}")
