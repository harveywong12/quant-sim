#!/usr/bin/env python3
"""新 sleeve 研究回测（严格 PIT，与回测页同窗口/同成本/同基准口径）。

候选 A：红利低波选股（300 宇宙，dv_ratio + 60日低波动，月度 top15）
候选 B：多资产 ETF 动量组合（252日动量 top3，511880 货币 ETF 做现金代理）

输出证据 JSON：绝对指标 + 相对 510300 基准指标。达标与否由人判断，
本脚本只产证据、不改任何生产配置。
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_strategy_backtest_curves import (  # noqa: E402
    CAPITAL, CROSS_COST, DB, SINGLE_COST, START, END,
    _load_benchmark_close, _load_stock_panels, _relative_metrics, _trading_stats,
)
from qsim.engine.cross_sectional_runner import (  # noqa: E402
    CrossSectionalData, monthly_rebalance_dates, run_cross_sectional_backtest,
)
from qsim.features.factor import _winsorize, _zscore  # noqa: E402
from qsim.strategies.cross_sectional import TopNScoreStrategy  # noqa: E402

MOM_UNIVERSE = ["510300", "510500", "512100", "518880",
                "511010", "513100", "513030", "159915"]
CASH_PROXY = "511880"  # 货币 ETF：动量全负时的停靠资产
MOM_LOOKBACK = 252
MOM_TOP_N = 3
DV_TOP_N = 15
DV_VOL_DAYS = 60


def _load_etf_panels() -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    symbols = MOM_UNIVERSE + [CASH_PROXY]
    marks = ",".join("?" for _ in symbols)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        bars = pd.read_sql_query(
            f"SELECT symbol,date,open,close FROM bars WHERE symbol IN ({marks}) "
            "AND date>=? AND date<=? ORDER BY date,symbol",
            conn, params=[*symbols, START, END],
        )
    finally:
        conn.close()
    open_p = bars.pivot(index="date", columns="symbol", values="open")
    close_p = bars.pivot(index="date", columns="symbol", values="close")
    return open_p, close_p, sorted(close_p.index.astype(str))


def build_momentum_scores(close: pd.DataFrame, dates: list[str],
                          lookbacks: tuple[int, ...] = (MOM_LOOKBACK,)) -> pd.DataFrame:
    mom = sum(close / close.shift(lb) - 1 for lb in lookbacks) / len(lookbacks)
    rows = {d: mom.loc[d].dropna() for d in dates if d in mom.index}
    df = pd.DataFrame(rows).T
    # 货币 ETF 计 0 分：仅当正动量资产不足 top_n 时自然入选
    df[CASH_PROXY] = 0.0
    return df


def _universe_members(conn: sqlite3.Connection, date: str) -> set[str]:
    rows = conn.execute(
        "SELECT ts_code FROM stock_universe_history WHERE index_code='000300.SH' "
        "AND trade_date=(SELECT MAX(trade_date) FROM stock_universe_history "
        "WHERE index_code='000300.SH' AND trade_date<=?)",
        (date,),
    ).fetchall()
    return {r[0] for r in rows}


def build_dividend_lowvol_scores(conn: sqlite3.Connection, dates: list[str],
                                 close: pd.DataFrame) -> pd.DataFrame:
    vol = close.pct_change().rolling(DV_VOL_DAYS).std() * math.sqrt(252)
    rows: dict[str, pd.Series] = {}
    for d in dates:
        dv = pd.read_sql_query(
            "SELECT v.ts_code, v.dv_ratio FROM stock_valuation v "
            "WHERE v.date=(SELECT MAX(date) FROM stock_valuation WHERE date<=?)",
            conn, params=(d,),
        ).set_index("ts_code")["dv_ratio"]
        members = _universe_members(conn, d)
        dv = dv[dv.index.isin(members)].dropna()
        if dv.empty:
            continue
        v60 = vol.loc[d].reindex(dv.index).dropna() if d in vol.index else pd.Series(dtype=float)
        common = dv.index.intersection(v60.index)
        if len(common) < DV_TOP_N * 2:
            continue
        score = _zscore(_winsorize(dv.loc[common])) - _zscore(_winsorize(v60.loc[common]))
        rows[d] = score.dropna()
    return pd.DataFrame(rows).T


def _evidence(key: str, name: str, result: dict, bench: pd.Series) -> dict:
    metrics = _trading_stats(result, CAPITAL)
    equity = result["equity"][["date", "cash", "position_value", "total_value"]].to_dict(orient="records")
    metrics.update(_relative_metrics(equity, bench))
    return {"key": key, "name": name, "metrics": metrics, "points": len(equity)}


def main() -> None:
    bench = _load_benchmark_close()
    out: list[dict] = []

    # 候选 B：多资产 ETF 动量组合
    etf_open, etf_close, etf_dates = _load_etf_panels()
    etf_rb = monthly_rebalance_dates(etf_dates)
    mom_scores = build_momentum_scores(etf_close, etf_rb)
    mom_data = CrossSectionalData(
        dates=etf_dates, factor_scores=mom_scores, open=etf_open, close=etf_close,
    )
    mom = run_cross_sectional_backtest(
        mom_data, TopNScoreStrategy(top_n=MOM_TOP_N), CAPITAL, SINGLE_COST,
        rebalance="monthly", rebalance_dates=etf_rb,
    )
    out.append(_evidence("multi_asset_mom", "多资产ETF动量", mom, bench))

    # 变体 B'：63+252 日混合动量（稳健性检验，非参数优选）
    mom2_scores = build_momentum_scores(etf_close, etf_rb, lookbacks=(63, 252))
    mom2_data = CrossSectionalData(
        dates=etf_dates, factor_scores=mom2_scores, open=etf_open, close=etf_close,
    )
    mom2 = run_cross_sectional_backtest(
        mom2_data, TopNScoreStrategy(top_n=MOM_TOP_N), CAPITAL, SINGLE_COST,
        rebalance="monthly", rebalance_dates=etf_rb,
    )
    out.append(_evidence("multi_asset_mom_blend", "多资产ETF动量·混合", mom2, bench))

    # 候选 A：红利低波选股
    stock_open, stock_close, stock_dates = _load_stock_panels()
    stock_rb = monthly_rebalance_dates(stock_dates)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        dv_scores = build_dividend_lowvol_scores(conn, stock_rb, stock_close)
    finally:
        conn.close()
    dv_data = CrossSectionalData(
        dates=stock_dates, factor_scores=dv_scores, open=stock_open, close=stock_close,
    )
    dv = run_cross_sectional_backtest(
        dv_data, TopNScoreStrategy(top_n=DV_TOP_N), CAPITAL, CROSS_COST,
        rebalance="monthly", rebalance_dates=stock_rb,
    )
    out.append(_evidence("dividend_lowvol", "红利低波", dv, bench))

    # 变体 A'：top20 降换手降成本（稳健性检验）
    dv20 = run_cross_sectional_backtest(
        dv_data, TopNScoreStrategy(top_n=20), CAPITAL, CROSS_COST,
        rebalance="monthly", rebalance_dates=stock_rb,
    )
    out.append(_evidence("dividend_lowvol_t20", "红利低波·top20", dv20, bench))

    for item in out:
        m = item["metrics"]
        print(json.dumps({
            "key": item["key"], "name": item["name"], "points": item["points"],
            "total_return": round(m.get("total_return", 0) * 100, 1),
            "annual": round(m.get("annual_return", 0) * 100, 1),
            "mdd": round(m.get("max_drawdown", 0) * 100, 1),
            "sharpe": round(m.get("sharpe", 0), 2),
            "excess": round(m.get("excess_total_return", 0) * 100, 1),
            "IR": round(m.get("information_ratio", 0), 2),
            "calmar": round(m.get("calmar", 0), 2),
            "win_rate": round(m.get("monthly_win_rate", 0) * 100),
            "trades": m.get("num_trades"), "cost_drag": round(m.get("cost_drag", 0) * 100, 2),
            "days": m.get("days"),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
