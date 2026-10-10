"""qsim 模拟盘引擎（模型 A：日线 T+1，零未来函数）。

执行模型：
  - 收盘后（15:00）用"截至当日收盘"的数据生成次日信号
    （strategy.generate(data.iloc[:i+1])，严格无未来函数）
  - 次日 09:30 开盘价执行昨日信号（T+1）
  - 当日结算：收盘价估值，写 qsim_snapshots

回放设计：
  - 对已通过 migration gate 的策略，在显式区间内幂等重建
  - 未通过 gate 的策略保留现有数据并跳过，避免未经对账直接运行

热插拔：
  - 策略实例化走 hotzone.load(name)（热区优先，内置兜底）
  - 每次运行记录 hotzone.snapshot() 哈希到 snapshots/trades（code_hash）

数据边界：
  - 只写 qsim_* 表，读 bars（行情）与 stock_info（名称），不碰旧系统账户/交易表
"""

from __future__ import annotations

import datetime as dt
import logging
import json
import os
import sqlite3
from contextvars import ContextVar
from pathlib import Path

import pandas as pd
from qsim.services import distributions

from qsim.engine.account import Account
from qsim.engine.costs import CostModel
from qsim.engine.cross_sectional_runner import (
    CrossSectionalData,
    run_cross_sectional_backtest,
)
from qsim.engine.ledger import Ledger
from qsim.engine.rotation_runner import run_rotation_backtest
from qsim.engine.runner import DailyExecutionSink, run_daily_executor
from qsim.storage.paper import (
    PAPER_STRATEGIES,
    PAPER_VERSIONED_CANDIDATES,
    QUANT_DB,
    SCHEMA,
    get_conn,
    init_schema,
    paper_strategy_catalog,
    seed_accounts,
)
from qsim.strategies import hotzone
from qsim.strategies.rotation import TrendFallback
from qsim.strategies.cross_sectional import (
    MultiFactorStrategy, NoStarMultiFactorStrategy, Alpha158Strategy,
    TopNScoreStrategy,
)
from qsim.features.etf_momentum import MOM_SYMBOLS
from qsim.strategies.migration import paper_enabled
from qsim.strategies.market_gate import multi_factor_v3_allocations

from qsim.services.mutation_lock import serialized_mutation

logger = logging.getLogger("qsim.paper")
_ARTIFACT_WRITES = ContextVar("paper_artifact_writes", default=True)
_DISTRIBUTION_PROBE = ContextVar('distribution_probe', default=False)
_ACTION_SYMBOLS = ContextVar('action_symbols', default=frozenset())
_FROZEN_SIGNALS = ContextVar('frozen_paper_signals', default={})


_MARKET_SOURCE = ContextVar('paper_market_source', default=None)


def _market_source(conn):
    return _MARKET_SOURCE.get() or conn


def formal_enabled():
    return os.environ.get('QSIM_FORMAL_PAPER', '0') == '1'


def _corporate_kwargs(source, name, start, end):
    if not formal_enabled():
        return {}
    actions = [] if _DISTRIBUTION_PROBE.get() else distributions.load(
        source, _ACTION_SYMBOLS.get(), start, end, dt.date.today().strftime('%Y%m%d'))
    return {'distributions': actions, 'strict_distributions': True,
            'rounding_policy': os.environ.get('QSIM_DIVIDEND_ROUNDING','require_evidence'),
            'frozen_signals': _FROZEN_SIGNALS.get().get(name, {})}


def _persist_corporate(conn, name, result):
    if not formal_enabled():
        return
    if not _DISTRIBUTION_PROBE.get() and {t['symbol'] for t in result['trades']} - set(_ACTION_SYMBOLS.get()):
        error = RuntimeError('new traded instrument lacks a distribution API receipt')
        error.symbols = {t['symbol'] for t in result['trades']} - set(_ACTION_SYMBOLS.get())
        raise error
    distributions.persist(conn, name, result)


COST = CostModel(commission_rate=0.0001, min_commission=0.1,
                 stamp_tax=0.0, slippage=0.0005)
# Stock-pool paper accounts use the same A-share cost model as the parity
# runner.  ETF sleeves keep the explicit COST above for their legacy contract.
PAPER_CROSS_COST = CostModel(round_fee=False, historical_rates=True)

# 模型 A 日线策略（趋势/均值回归/防御，收盘信号→次日开盘执行）
# trend_fallback 为轮动策略（generate 返回目标标的），Step 1.2b 单独实现
DAILY_STRATEGIES = [
    "tsmom_vol", "dual_ma", "rsi_boll", "cb_dual_low", "grid_atr",
]

# 轮动策略（generate 返回目标标的，持有 A 或换 B）
ROTATION_STRATEGIES = ["trend_fallback"]

# 横截面策略（月度调仓选股）
CROSS_STRATEGIES = {"multi_factor": MultiFactorStrategy(top_n=10),
                    "alpha158": Alpha158Strategy(top_n=10),
                    "multi_factor_v3": MultiFactorStrategy(top_n=10),
                    "multi_factor_v4": NoStarMultiFactorStrategy(top_n=10),
                    "dividend_lowvol": TopNScoreStrategy(top_n=20),
                    "multi_asset_mom": TopNScoreStrategy(top_n=3)}

# 非默认面板（表/列/标的集/成本）按策略覆盖；缺省为 stock_bars + PAPER_CROSS_COST
CROSS_PANELS = {
    "multi_asset_mom": {
        "table": "bars", "symbol_col": "symbol",
        "symbols": MOM_SYMBOLS, "cost": COST,
    },
}

# 模拟交易启动月（取该月首个真实交易日为启动日；2026-08-01 是周六非交易日）
BACKFILL_START = "20260801"
BACKFILL_MONTH = "202608"
V3_GATE_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "multi_factor_v3"



# warmup 窗口（仅供诊断参考；generate 实际用全部历史）
#   tsmom_vol  需要 252 天动量 → 回填 260 个交易日
#   dual_ma    需要 21 天双均线 → 回填 40 个交易日
#   rsi_boll   需要 40 天 RSI+布林 → 回填 60 个交易日
#   cb_dual_low 需要 8 天 → 回填 40 个交易日
# 起点 = 最近 N 个交易日的首日（行情不足时用最早可得）
WARMUP_DAYS = {
    "tsmom_vol": 260, "dual_ma": 40, "rsi_boll": 60, "cb_dual_low": 40,
    "grid_atr": 40,
}


# ---------- 行情读取 ----------

def _normalize_dates(bars: pd.DataFrame) -> pd.DataFrame:
    """Datetime date columns carry a ' 00:00:00' suffix through str casts."""
    out = bars.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    return out


def _load_bars(symbol: str, start: str | None = None) -> pd.DataFrame:
    """读标的日线（qsim 库 bars 表，加载全部历史供 warmup）。"""
    conn = sqlite3.connect(f"file:{QUANT_DB}?mode=ro", uri=True)
    try:
        if start:
            df = pd.read_sql_query(
                "SELECT date, open, high, low, close, volume FROM bars "
                "WHERE symbol=? AND date>=? ORDER BY date",
                conn, params=(symbol, start),
            )
        else:
            df = pd.read_sql_query(
                "SELECT date, open, high, low, close, volume FROM bars "
                "WHERE symbol=? ORDER BY date",
                conn, params=(symbol,),
            )
        if formal_enabled():
            refs = pd.read_sql_query('SELECT date,pre_close FROM qsim_execution_reference WHERE symbol=?',conn,params=(symbol,))
            df = df.merge(refs, on='date', how='left')
            if df['pre_close'].isna().any():
                # Require reference only in the execution window below; old
                # warmup rows continue to serve strategy research.
                pass
    finally:
        conn.close()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


# ---------- 策略加载（热插拔） ----------

def _load_strategy(name: str):
    """热区优先加载策略实例；热区无则内置兜底。"""
    return hotzone.load(name)


def _code_hash_snapshot() -> str:
    """当前策略版本凭证：热区文件内容哈希；无热区时标记 builtin。"""
    snap = hotzone.snapshot()
    if not snap:
        return "builtin"
    return ",".join(f"{k}:{v[:8]}" for k, v in sorted(snap.items()))


# ---------- 单策略日线模拟 ----------


class _PaperDailySink(DailyExecutionSink):
    """把统一执行器的成交/结算写入 qsim_* 表（日线策略）。"""

    def __init__(self, conn: sqlite3.Connection, name: str, code_hash: str) -> None:
        self._conn = conn
        self._name = name
        self._code_hash = code_hash

    def on_trade(self, date: str, trade, reason: str | None) -> None:
        self._conn.execute(
            "INSERT INTO qsim_trades "
            "(date,strategy,symbol,side,price,shares,amount,fee,reason,code_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (date, self._name, trade.symbol, trade.side, trade.price,
             trade.shares, trade.amount, trade.fee, reason or "", self._code_hash),
        )

    def on_snapshot(self, date: str, cash: float, position_value: float,
                    total_value: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO qsim_snapshots "
            "(date,strategy,total_value,cash,position_value,strategy_version) "
            "VALUES (?,?,?,?,?,?)",
            (date, self._name, round(total_value, 2), round(cash, 2),
             round(position_value, 2), self._code_hash),
        )

    def on_skip(self, date: str, side: str, reason: str) -> None:
        logger.info("%s %s 跳过: %s", self._name, date, reason)


def _run_daily(conn: sqlite3.Connection, name: str, bars: pd.DataFrame,
               days: list[str], account_id: int) -> int:
    """日线策略：统一 T+1 执行器（warmup 全历史信号）+ qsim_* 持久化。"""
    acct = conn.execute(
        "SELECT cash FROM qsim_accounts WHERE id=?", (account_id,)
    ).fetchone()
    if not acct:
        return 0
    code_hash = _code_hash_snapshot()
    if formal_enabled() and bars.loc[(bars['date']>=pd.to_datetime(days[0])) & (bars['date']<=pd.to_datetime(days[-1])), 'pre_close'].isna().any():
        raise RuntimeError('ETF raw previous-close reference missing')
    result = run_daily_executor(
        bars, _load_strategy(name), float(acct["cash"]), distributions.stock_cost(_market_source(conn), COST, days[0], days[-1]) if formal_enabled() else COST,
        PAPER_STRATEGIES[name]["symbol"],
        start=days[0], end=days[-1], warmup=True,
        sink=_PaperDailySink(conn, name, code_hash),
        freeze_last_signal=formal_enabled(),
        **_corporate_kwargs(_market_source(conn), name, days[0], days[-1]),
    )
    _persist_corporate(conn, name, result)
    conn.execute(
        "UPDATE qsim_accounts SET cash=?, shares=?, avg_cost=?, code_hash=? WHERE id=?",
        (result["cash"], result["shares"], result["avg_cost"], code_hash, account_id),
    )
    conn.execute("DELETE FROM qsim_positions WHERE account_id=?", (account_id,))
    if result["shares"] > 0:
        conn.execute(
            "INSERT INTO qsim_positions (account_id,ts_code,shares,avg_cost,updated_date) "
            "VALUES (?,?,?,?,?)",
            (account_id, PAPER_STRATEGIES[name]["symbol"], result["shares"],
             result["avg_cost"], days[-1]),
        )
    return len(result["trades"])


def _run_rotation(conn: sqlite3.Connection, name: str,
                  bars_a: pd.DataFrame, bars_b: pd.DataFrame,
                  sym_a: str, sym_b: str,
                  days: list[str], account_id: int) -> int:
    """轮动策略（trend_fallback）：委托统一 rotation runner 执行并持久化。

    paper 与回测共用同一执行核心（同 matcher、同 CostModel、同涨跌停
    检查），backtest 只是喂历史数据；这里只负责把结果写入 qsim_* 表。
    """
    acct = conn.execute(
        "SELECT cash FROM qsim_accounts WHERE id=?", (account_id,)
    ).fetchone()
    if not acct:
        return 0
    if formal_enabled():
        for frame in (bars_a, bars_b):
            part=frame.loc[(frame['date']>=pd.to_datetime(days[0])) & (frame['date']<=pd.to_datetime(days[-1]))]
            if part['pre_close'].isna().any():
                raise RuntimeError('rotation ETF raw previous-close reference missing')
    result = run_rotation_backtest(
        {sym_a: _normalize_dates(bars_a), sym_b: _normalize_dates(bars_b)},
        TrendFallback(symbol=sym_a, fallback=sym_b),
        float(acct["cash"]),
        distributions.stock_cost(_market_source(conn), COST, days[0], days[-1]) if formal_enabled() else COST,
        start=days[0],
        end=days[-1],
        warmup=formal_enabled(),
        **_corporate_kwargs(_market_source(conn), name, days[0], days[-1]),
    )
    _persist_corporate(conn, name, result)
    code_hash = _code_hash_snapshot()
    for trade in result["trades"]:
        conn.execute(
            "INSERT INTO qsim_trades (date,strategy,symbol,side,price,shares,amount,fee,reason,code_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (trade["date"], name, trade["symbol"], trade["side"],
             trade["price"], trade["shares"], trade["amount"], trade["fee"],
             "rotation", code_hash),
        )
    for row in result["equity"].to_dict(orient="records"):
        conn.execute(
            "INSERT OR REPLACE INTO qsim_snapshots (date,strategy,total_value,cash,position_value,strategy_version) "
            "VALUES (?,?,?,?,?,?)",
            (row["date"], name, round(float(row["total_value"]), 2),
             round(float(row["cash"]), 2), round(float(row["position_value"]), 2),
             code_hash),
        )
    symbol = result["symbol"]
    shares = int(result["shares"])
    cash = round(float(result["cash"]), 2)
    avg_cost = round(float(result["avg_cost"]), 6)
    conn.execute(
        "UPDATE qsim_accounts SET cash=?, shares=?, symbol=?, avg_cost=?, code_hash=? WHERE id=?",
        (cash, shares, symbol, avg_cost, code_hash, account_id),
    )
    conn.execute("DELETE FROM qsim_positions WHERE account_id=?", (account_id,))
    if symbol is not None and shares > 0:
        conn.execute(
            "INSERT INTO qsim_positions (account_id,ts_code,shares,avg_cost,updated_date) VALUES (?,?,?,?,?)",
            (account_id, symbol, shares, avg_cost, days[-1]),
        )
    return len(result["trades"])


def _run_cross_sectional(conn: sqlite3.Connection, name: str,
                         days: list[str], account_id: int,
                         market_conn: sqlite3.Connection | None = None) -> int:
    """Run the approved cross-sectional runner and persist its result.

    Score history is built through the last execution date, while the runner
    itself only executes the requested paper window.  This keeps historical
    IC settlement available without using labels after the paper ``as_of``.
    """
    if not days:
        return 0

    from qsim.features.factor import compute_factor_ic_scores_asof
    from qsim.features.alpha158 import compute_alpha158_ic_scores

    # 面板参数：multi_asset_mom 用 ETF bars 面板与 ETF 成本，其余走股票默认
    panel = CROSS_PANELS.get(name, {})
    table = panel.get("table", "stock_bars")
    sym_col = panel.get("symbol_col", "ts_code")
    panel_symbols = panel.get("symbols")
    cost = panel.get("cost", PAPER_CROSS_COST)

    # ``conn`` owns paper-account writes.  An isolated replay supplies a
    # separate read-only market connection, which lets it share this exact
    # execution path without ever reading from or writing to qsim_* records.
    source_conn = market_conn or conn
    if formal_enabled():
        cost = distributions.stock_cost(source_conn, cost, days[0], days[-1])
    query = (
        f"SELECT DISTINCT date FROM {table} WHERE date<=? ORDER BY date"
    )
    history_days = [row[0] for row in source_conn.execute(query, (days[-1],))]
    if not history_days:
        return 0

    execution_table = table
    if table == "stock_bars":
        from qsim.market.execution_prices import require_raw_stock_window
        require_raw_stock_window(source_conn, days[0], days[-1])
        execution_table = "stock_execution_bars"

    def first_of_month(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            month = value[:6]
            if month not in seen:
                seen.add(month)
                result.append(value)
        return result

    history_rebalances = first_of_month(history_days)
    # ``days`` may be a paper-ledger slice beginning in the middle or at the
    # end of a month.  Its first row is not automatically a rebalance date:
    # derive real month starts from the complete available market calendar,
    # then retain only dates in this execution window.
    execution_dates = set(days)
    execution_rebalances = [d for d in history_rebalances if d in execution_dates]
    # v3 changes only the market-allocation gate; its stock-selection model
    # remains the legacy-compatible multi-factor IC ranking, never alpha158.
    if name in {"multi_factor", "multi_factor_v3", "multi_factor_v4"}:
        score_map = compute_factor_ic_scores_asof(
            QUANT_DB, history_rebalances, as_of=days[-1]
        )
    elif name == "dividend_lowvol":
        from qsim.features.dividend_lowvol import compute_dividend_lowvol_scores
        score_map = compute_dividend_lowvol_scores(
            QUANT_DB, history_rebalances, as_of=days[-1]
        )
    elif name == "multi_asset_mom":
        from qsim.features.etf_momentum import compute_momentum_scores
        score_map = compute_momentum_scores(
            QUANT_DB, history_rebalances, as_of=days[-1]
        )
    else:
        score_map = compute_alpha158_ic_scores(QUANT_DB, history_rebalances)
    # Historical month-ends are needed to settle rolling IC, but the runner's
    # data contract only accepts score rows in its execution window.  Keep the
    # former in ``score_map`` and pass only executable rebalance rows onward.
    scores = pd.DataFrame(score_map).T
    # An unavailable score must hold the portfolio, not trigger an empty
    # selection that liquidates everything on the next open.
    rebalance_dates = [d for d in execution_rebalances if d in score_map]
    scores = scores.loc[rebalance_dates] if rebalance_dates else pd.DataFrame()

    volume_select = ", " + ("vol" if execution_table == "stock_execution_bars" else "volume") + " AS execution_volume" if formal_enabled() else ""
    if panel_symbols:
        marks = ",".join("?" for _ in panel_symbols)
        bars = pd.read_sql_query(
            f"SELECT {sym_col}, date, open, close{volume_select} FROM {execution_table} "
            f"WHERE {sym_col} IN ({marks}) AND date>=? AND date<=? "
            f"ORDER BY date, {sym_col}",
            source_conn,
            params=([*panel_symbols, days[0], days[-1]]),
        )
    else:
        bars = pd.read_sql_query(
            f"SELECT {sym_col}, date, open, close{volume_select} FROM {execution_table} "
            "WHERE date>=? AND date<=? ORDER BY date, " + sym_col,
            source_conn,
            params=(days[0], days[-1]),
        )
    if bars.empty:
        return 0
    if formal_enabled():
        bars.loc[bars["execution_volume"] <= 0, "open"] = float("nan")
    references = None
    if formal_enabled() and table == 'bars':
        refs = pd.read_sql_query('SELECT symbol,date,pre_close FROM qsim_execution_reference WHERE date>=? AND date<=?', source_conn, params=(days[0],days[-1]))
        references = refs.pivot(index='date',columns='symbol',values='pre_close')
        joined = bars.merge(refs.rename(columns={'symbol':sym_col}),on=[sym_col,'date'],how='left')
        if joined['pre_close'].isna().any():
            raise RuntimeError('ETF basket raw previous-close reference missing')
    data = CrossSectionalData(
        dates=days,
        factor_scores=scores,
        open=bars.pivot(index="date", columns=sym_col, values="open"),
        close=bars.pivot(index="date", columns=sym_col, values="close"),
        pre_close=references,
    )
    acct = conn.execute(
        "SELECT cash FROM qsim_accounts WHERE id=?", (account_id,)
    ).fetchone()
    if not acct:
        return 0
    allocation_by_signal_date = None
    gate_records: list[dict[str, object]] = []
    if name in {"multi_factor_v3", "multi_factor_v4"}:
        allocation_by_signal_date, gate_records = multi_factor_v3_allocations(
            source_conn, history_days, rebalance_dates,
        )
    result = run_cross_sectional_backtest(
        data=data,
        strategy=CROSS_STRATEGIES[name],
        initial_cash=float(acct["cash"]),
        cost=cost,
        rebalance="monthly",
        rebalance_dates=rebalance_dates,
        allocation_by_signal_date=allocation_by_signal_date,
        sizing="live_affordable"
        if name in {"multi_factor_v3", "multi_factor_v4"}
        else "parity",
        **_corporate_kwargs(source_conn, name, days[0], days[-1]),
    )
    _persist_corporate(conn, name, result)
    code_hash = _code_hash_snapshot()
    version_tag = f"{name}|{code_hash}" if name in {"multi_factor_v3", "multi_factor_v4"} else code_hash
    if name in {"multi_factor_v3", "multi_factor_v4"} and _ARTIFACT_WRITES.get():
        V3_GATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "strategy": name,
            "version": "v3" if name == "multi_factor_v3" else "v4",
            "policy": "512100 trend + 000300 breadth + 20-day volatility scaling" + ("; 688/689 excluded before score backfill" if name == "multi_factor_v4" else ""),
            "source_market_as_of": days[-1],
            "records": gate_records,
        }
        target = V3_GATE_DIR / "latest-gate.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    for trade in result["trades"]:
        conn.execute(
            "INSERT INTO qsim_trades "
            "(date,strategy,symbol,side,price,shares,amount,fee,reason,code_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (trade["date"], name, trade["symbol"], trade["side"],
             trade["price"], trade["shares"], trade["amount"], trade["fee"],
             f"{name}: trend+breadth+volatility gate" if name in {"multi_factor_v3", "multi_factor_v4"} else "cross_sectional",
             version_tag),
        )
    for row in result["equity"].to_dict(orient="records"):
        conn.execute(
            "INSERT OR REPLACE INTO qsim_snapshots "
            "(date,strategy,total_value,cash,position_value,strategy_version) "
            "VALUES (?,?,?,?,?,?)",
            (row["date"], name, round(float(row["total_value"]), 2),
             round(float(row["cash"]), 2), round(float(row["position_value"]), 2),
             version_tag),
        )
    conn.execute(
        "UPDATE qsim_accounts SET cash=?, shares=?, avg_cost=?, symbol=NULL, code_hash=? "
        "WHERE id=?",
        (round(float(result["cash"]), 2), sum(result["shares"].values()),
         0.0, version_tag, account_id),
    )
    conn.execute("DELETE FROM qsim_positions WHERE account_id=?", (account_id,))
    updated_date = days[-1]
    for code, shares in result["shares"].items():
        if shares <= 0:
            continue
        conn.execute(
            "INSERT INTO qsim_positions "
            "(account_id,ts_code,shares,avg_cost,updated_date) VALUES (?,?,?,?,?)",
            (account_id, code, shares, result["avg_cost"].get(code, 0.0), updated_date),
        )
    return len(result["trades"])


# ---------- 主流程 ----------

def _trading_days(bars: pd.DataFrame) -> list[str]:
    return [d.strftime("%Y%m%d") for d in bars["date"]]


def _rebuild_paper_impl(conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
              only: tuple[str, ...] | None = None, market_conn: sqlite3.Connection | None = None) -> dict:
    """模拟盘主流程：三类执行器（日线/轮动/横截面）。

    当前采用显式区间的幂等重建；未通过 migration gate 的策略不会被
    清空或运行。``end`` 同时受今天日期限制，避免 paper 回放读到未来行情。
    """
    catalog = paper_strategy_catalog()
    seed_accounts(conn, catalog)
    start = start or BACKFILL_START
    end = min(end or dt.date.today().strftime("%Y%m%d"),
              dt.date.today().strftime("%Y%m%d"))
    eligible = [name for name in PAPER_STRATEGIES if paper_enabled(name)]
    eligible.extend(
        name for name, cfg in PAPER_VERSIONED_CANDIDATES.items()
        if cfg.get("backfill", True)
    )
    if only is not None:
        unknown = set(only) - set(catalog)
        if unknown:
            raise ValueError(f"unknown paper strategy: {sorted(unknown)}")
        eligible = [name for name in eligible if name in set(only)]
    stats = {
        "strategies": 0, "trades": 0, "snapshots": 0, "days": 0,
        "skipped": len(catalog) - len(eligible),
    }
    if not eligible:
        logger.warning("没有策略通过 paper gate，保留现有 paper 数据")
        return stats

    # 全量重建只作用于已获准策略；parity_pending 的历史数据保持可审计。
    # 横截面执行器按实际账户本金动态确定可持有只数，不强制改写账户本金。
    marks = ",".join("?" for _ in eligible)
    conn.execute(f"DELETE FROM qsim_snapshots WHERE strategy IN ({marks})", eligible)
    conn.execute(f"DELETE FROM qsim_trades WHERE strategy IN ({marks})", eligible)
    for table in ('qsim_distribution_events','qsim_distribution_nav','qsim_distribution_checkpoints','qsim_frozen_daily_signals'):
        conn.execute(f'DELETE FROM {table} WHERE strategy IN ({marks})', eligible)
    account_rows = conn.execute(
        f"SELECT id FROM qsim_accounts WHERE strategy IN ({marks})", eligible
    ).fetchall()
    account_ids = [int(row["id"]) for row in account_rows]
    if account_ids:
        account_marks = ",".join("?" for _ in account_ids)
        conn.execute(
            f"DELETE FROM qsim_positions WHERE account_id IN ({account_marks})",
            account_ids,
        )
    conn.execute(
        f"UPDATE qsim_accounts SET cash=initial_capital, shares=0, "
        f"avg_cost=0, code_hash='' WHERE strategy IN ({marks})",
        eligible,
    )
    conn.commit()
    try:
        # ---------- 1) 日线策略（T+1：昨收信号→今开执行） ----------
        for name in DAILY_STRATEGIES:
            if name not in eligible:
                continue
            cfg = PAPER_STRATEGIES[name]
            symbol = cfg["symbol"]
            bars = _load_bars(symbol)
            if bars.empty:
                logger.warning("%s 无行情数据", name)
                continue
            last = conn.execute(
                "SELECT MAX(date) FROM qsim_snapshots WHERE strategy=?", (name,)
            ).fetchone()[0]
            days = _trading_days(bars)
            if last and last >= end:
                continue
            day_list = [d for d in days if start <= d <= end]
            if not day_list:
                continue
            acct = conn.execute(
                "SELECT id FROM qsim_accounts WHERE strategy=?", (name,)
            ).fetchone()
            if not acct:
                continue
            n = _run_daily(conn, name, bars, day_list, acct["id"])
            stats["trades"] += n
            stats["snapshots"] += len(day_list)
            stats["days"] += len(day_list)
            stats["strategies"] += 1

        # ---------- 2) 轮动策略（trend_fallback：目标标的 A↔B） ----------
        for name in ROTATION_STRATEGIES:
            if name not in eligible:
                continue
            cfg = PAPER_STRATEGIES[name]
            bars_a = _load_bars(cfg["symbol"])
            bars_b = _load_bars(cfg["fallback"])
            if bars_a.empty or bars_b.empty:
                logger.warning("%s 无行情", name)
                continue
            last = conn.execute(
                "SELECT MAX(date) FROM qsim_snapshots WHERE strategy=?", (name,)
            ).fetchone()[0]
            days_r = [d for d in _trading_days(bars_a) if start <= d <= end]
            if not days_r:
                continue
            acct = conn.execute(
                "SELECT id FROM qsim_accounts WHERE strategy=?", (name,)
            ).fetchone()
            if acct:
                n = _run_rotation(conn, name, bars_a, bars_b,
                                 cfg["symbol"], cfg["fallback"], days_r, acct["id"])
                stats["trades"] += n
                stats["days"] += len(days_r)
                stats["snapshots"] += len(days_r)
                stats["strategies"] += 1

        # ---------- 3) 横截面策略（multi_factor/alpha158：月度调仓） ----------
        conn2 = sqlite3.connect(f"file:{QUANT_DB}?mode=ro", uri=True)
        try:
            days_c = [r[0] for r in conn2.execute(
                "SELECT DISTINCT date FROM stock_bars "
                "WHERE date>=? AND date<=? ORDER BY date",
                (start, end),
            )]
        finally:
            conn2.close()
        for name in CROSS_STRATEGIES:
            if name not in eligible:
                continue
            cfg = catalog[name]
            strategy_start = max(start, str(cfg.get("start") or start))
            strategy_days = [date for date in days_c if date >= strategy_start]
            if not strategy_days:
                continue
            last = conn.execute(
                "SELECT MAX(date) FROM qsim_snapshots WHERE strategy=?", (name,)
            ).fetchone()[0]
            if last and last >= end:
                continue
            acct = conn.execute(
                "SELECT id FROM qsim_accounts WHERE strategy=?", (name,)
            ).fetchone()
            if acct:
                n = _run_cross_sectional(conn, name, strategy_days, acct["id"], market_conn=market_conn)
                stats["trades"] += n
                stats["days"] += len(strategy_days)
                stats["snapshots"] += len(strategy_days)
                stats["strategies"] += 1

        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return stats


def _rebuild_paper(conn, start=None, end=None, only=None, market_conn=None):
    token=_MARKET_SOURCE.set(market_conn or conn)
    try:
        return _rebuild_paper_impl(conn,start,end,only,market_conn)
    finally:
        _MARKET_SOURCE.reset(token)


def _eligible(only: tuple[str, ...] | None) -> list[str]:
    catalog = paper_strategy_catalog()
    if only is not None and set(only) - set(catalog):
        raise ValueError(f"unknown paper strategy: {sorted(set(only) - set(catalog))}")
    names = [name for name in PAPER_STRATEGIES if paper_enabled(name)]
    names.extend(name for name, cfg in PAPER_VERSIONED_CANDIDATES.items() if cfg.get("backfill", True))
    return [name for name in names if only is None or name in only]


def _validate_staged(live: sqlite3.Connection, stage: sqlite3.Connection,
                     names: list[str], end: str) -> None:
    catalog = paper_strategy_catalog()
    for name in names:
        cfg = catalog[name]
        if name in CROSS_STRATEGIES:
            expected = live.execute(
                "SELECT MAX(date) FROM stock_bars WHERE date<=?", (end,)
            ).fetchone()[0]
        else:
            symbols = [cfg["symbol"]]
            if cfg.get("fallback"):
                symbols.append(cfg["fallback"])
            dates = [live.execute(
                "SELECT MAX(date) FROM bars WHERE symbol=? AND date<=?", (symbol, end)
            ).fetchone()[0] for symbol in symbols]
            expected = min(dates) if all(dates) else None
        actual = stage.execute(
            "SELECT MAX(date) FROM qsim_snapshots WHERE strategy=?", (name,)
        ).fetchone()[0]
        if not expected or actual != expected:
            raise RuntimeError(f"staged settlement incomplete: {name}, expected={expected}, actual={actual}")
        invalid = stage.execute(
            "SELECT COUNT(*) FROM qsim_snapshots s LEFT JOIN qsim_distribution_nav n ON n.strategy=s.strategy AND n.date=s.date WHERE s.strategy=? AND "
            "(s.total_value<=0 OR s.cash < -0.02 OR s.position_value < -0.02 "
            "OR COALESCE(n.cash_receivable,0)<0 OR COALESCE(n.dividend_tax_liability,0)<0 "
            "OR ABS(s.total_value-s.cash-s.position_value-COALESCE(n.cash_receivable,0)+COALESCE(n.dividend_tax_liability,0))>0.03)", (name,),
        ).fetchone()[0]
        if invalid:
            raise RuntimeError(f"invalid staged NAV: {name}")
        account = stage.execute("SELECT id FROM qsim_accounts WHERE strategy=?", (name,)).fetchone()
        if stage.execute("SELECT COUNT(*) FROM qsim_positions WHERE account_id=? AND shares<0",
                         (account["id"],)).fetchone()[0]:
            raise RuntimeError(f"invalid staged holdings: {name}")


def _copy_rows(stage: sqlite3.Connection, live: sqlite3.Connection, table: str,
               where: str, params: list) -> None:
    columns = [row[1] for row in stage.execute(f"PRAGMA table_info({table})") if row[1] != "id"]
    rows = stage.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {where}", params).fetchall()
    live.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES "
                     f"({','.join('?' for _ in columns)})", rows)


def _promote_staged(live, stage, names, metadata=None):
    from qsim.services.ledger_publish import promote
    return promote(live, stage, names, metadata or {}, _copy_rows)


def _settlement_state(payload):
    root = Path(QUANT_DB).parent.parent
    path = root / "artifacts" / "settlement_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({**payload, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}, ensure_ascii=False))
    temporary.replace(path)


@serialized_mutation
def run_paper(start: str | None = None, end: str | None = None,
              only: tuple[str, ...] | None = None) -> dict:
    """Recompute privately, verify history, and append only unsettled dates."""
    if os.environ.get("QSIM_PAPER_AUTORUN", "1") == "0":
        raise RuntimeError("正式模拟账本尚未通过原始价格及公司行动准入；旧账本保留")
    if formal_enabled():
        from qsim.services.formal_paper import prepare_market
        prepare_market(start or BACKFILL_START, end)
    init_schema()
    live = get_conn()
    from qsim.services.ledger_publish import SCHEMA as RUN_SCHEMA, provenance
    live.executescript(RUN_SCHEMA)
    stage = sqlite3.connect(":memory:")
    stage.row_factory = sqlite3.Row
    frozen_token=_FROZEN_SIGNALS.set({})
    symbol_token=_ACTION_SYMBOLS.set(frozenset())
    try:
        stage.executescript(SCHEMA)
        columns = [row[1] for row in live.execute("PRAGMA table_info(qsim_accounts)")]
        accounts = live.execute(f"SELECT {','.join(columns)} FROM qsim_accounts").fetchall()
        stage.executemany(f"INSERT INTO qsim_accounts ({','.join(columns)}) VALUES "
                          f"({','.join('?' for _ in columns)})", accounts)
        stage.commit()
        if formal_enabled():
            _FROZEN_SIGNALS.set({name:distributions.frozen(live,name) for name in _eligible(only)})
            _ACTION_SYMBOLS.set(frozenset(r[0] for r in live.execute('SELECT symbol FROM qsim_distribution_coverage WHERE as_of=?', (dt.date.today().strftime('%Y%m%d'),))))
        names = _eligible(only)
        actual_end = min(end or dt.date.today().strftime("%Y%m%d"), dt.date.today().strftime("%Y%m%d"))
        # No calculation or account writes when every eligible ledger is current.
        try:
            _validate_staged(live, live, names, actual_end)
            if names and not formal_enabled():
                return {"status": "up_to_date", "strategies": 0, "appended": {"snapshots": 0, "trades": 0}}
        except RuntimeError:
            pass
        metadata = {**provenance(Path(QUANT_DB).parent.parent, live), "start": start or BACKFILL_START, "end": actual_end, "strategies": names,
                    "execution_mode":"daily_delayed_immutable_close" if formal_enabled() else "legacy",
                    "tax_profile":"mainland_individual_separate_accounts" if formal_enabled() else "legacy",
                    "cash_payment_rounding":os.environ.get("QSIM_DIVIDEND_ROUNDING","require_evidence"),
                    "ten_year_target":"NOT_VERIFIED"}
        stats = _rebuild_paper(stage, start, end, only, market_conn=live)
        names = _eligible(only)
        if names:
            actual_end = min(end or dt.date.today().strftime("%Y%m%d"),
                             dt.date.today().strftime("%Y%m%d"))
            _validate_staged(live, stage, names, actual_end)
            result = _promote_staged(live, stage, names, metadata)
            stats.update(result)
            _settlement_state({"ok": True, **result, "metadata": metadata})
        return stats
    except RuntimeError as exc:
        _settlement_state({"ok": False, "error": str(exc), "differences": getattr(exc, "differences", [])})
        raise
    finally:
        _FROZEN_SIGNALS.reset(frozen_token)
        _ACTION_SYMBOLS.reset(symbol_token)
        stage.close()
        live.close()


@serialized_mutation
def run_daily_settlement() -> dict:
    if os.environ.get("QSIM_PAPER_AUTORUN", "1") == "0":
        raise RuntimeError("正式模拟账本尚未通过原始价格及公司行动准入；旧账本保留")
    if formal_enabled():
        official = run_paper(BACKFILL_START)
        return {'official':official,'provisional':{'status':'disabled_in_formal_mode'},
                'signals':{'mode':'immutable_close_daily_delayed'}}
    from qsim.services.provisional_paper import run_provisional_paper
    from qsim.services import paper_orders
    # Generate provisional output first; an error here cannot erase official records.
    provisional = run_provisional_paper(BACKFILL_START)
    official = run_paper(BACKFILL_START)
    signals = {"multi_factor_v3": paper_orders.freeze_v3_intent(),
               "multi_factor_v4": paper_orders.freeze_v4_intent()}
    if any(item.get("ok") is False for item in signals.values()):
        raise RuntimeError(f"signal freeze failed: {signals}")
    return {"official": official, "provisional": provisional, "signals": signals}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_daily_settlement()
    provisional = result["provisional"]
    print(json.dumps({"official": result["official"], "provisional": {
        "end": provisional.get("end"), "status": provisional.get("status", "updated"),
        "strategies": len(provisional.get("strategies", []))}, "signals": result["signals"]}, ensure_ascii=False))
