"""红利低波选股因子：dv_ratio（股息率）+ 60 日低波动 等权 z 合成。

score_map 契约与 factor/alpha158 一致：{date: Series(ts_code -> score)}，
仅含当日 300 宇宙成员（PIT），dv_ratio 取该日前最新一条估值记录。
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Iterable

import pandas as pd

from qsim.features.factor import _winsorize, _zscore

VOL_DAYS = 60
MIN_NAMES = 40


def _universe_members(conn: sqlite3.Connection, date: str) -> set[str]:
    rows = conn.execute(
        "SELECT ts_code FROM stock_universe_history WHERE index_code='000300.SH' "
        "AND trade_date=(SELECT MAX(trade_date) FROM stock_universe_history "
        "WHERE index_code='000300.SH' AND trade_date<=?)",
        (date,),
    ).fetchall()
    return {r[0] for r in rows}


def compute_dividend_lowvol_scores(
    db_path: str | Path,
    dates: Iterable[str],
    vol_days: int = VOL_DAYS,
    as_of: str | None = None,
) -> dict[str, pd.Series]:
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    try:
        rb_list = sorted(d for d in dates if as_of is None or d <= as_of)
        if not rb_list:
            return {}
        warmup_start = (
            pd.to_datetime(min(rb_list)) - pd.Timedelta(days=vol_days * 3)
        ).strftime("%Y%m%d")
        cutoff = as_of or max(rb_list)
        bars = pd.read_sql_query(
            "SELECT ts_code, date, close FROM stock_bars WHERE date>=? AND date<=? "
            "ORDER BY date, ts_code",
            conn, params=(warmup_start, cutoff),
        )
        if bars.empty:
            return {}
        close = bars.pivot(index="date", columns="ts_code", values="close")
        vol = close.pct_change(fill_method=None).rolling(vol_days).std() * math.sqrt(252)
        out: dict[str, pd.Series] = {}
        for d in rb_list:
            dv = pd.read_sql_query(
                "SELECT v.ts_code, v.dv_ratio FROM stock_valuation v "
                "WHERE v.date=(SELECT MAX(date) FROM stock_valuation WHERE date<=?)",
                conn, params=(d,),
            ).set_index("ts_code")["dv_ratio"]
            dv = dv[dv.index.isin(_universe_members(conn, d))].dropna()
            if dv.empty or d not in vol.index:
                continue
            v60 = vol.loc[d].reindex(dv.index).dropna()
            common = dv.index.intersection(v60.index)
            if len(common) < MIN_NAMES:
                continue
            score = _zscore(_winsorize(dv.loc[common])) - _zscore(_winsorize(v60.loc[common]))
            out[d] = score.dropna()
        return out
    finally:
        conn.close()
