"""多资产 ETF 动量因子：63+252 日混合动量，511880 货币 ETF 计 0 分做现金代理。

score_map 契约：{date: Series(symbol -> 混合动量)}。货币代理计 0 分，
仅当正动量资产不足 top_n 时自然入选，实现"绝对动量为负则避险"。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import pandas as pd

MOM_UNIVERSE = ["510300", "510500", "512100", "518880",
                "511010", "513100", "513030", "159915"]
CASH_PROXY = "511880"
MOM_SYMBOLS = MOM_UNIVERSE + [CASH_PROXY]
LOOKBACKS = (63, 252)


def compute_momentum_scores(
    db_path: str | Path,
    dates: Iterable[str],
    lookbacks: tuple[int, ...] = LOOKBACKS,
    as_of: str | None = None,
) -> dict[str, pd.Series]:
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    try:
        rb_list = sorted(d for d in dates if as_of is None or d <= as_of)
        if not rb_list:
            return {}
        warmup_start = (
            pd.to_datetime(min(rb_list)) - pd.Timedelta(days=max(lookbacks) * 2)
        ).strftime("%Y%m%d")
        cutoff = as_of or max(rb_list)
        marks = ",".join("?" for _ in MOM_SYMBOLS)
        bars = pd.read_sql_query(
            f"SELECT symbol, date, close FROM bars WHERE symbol IN ({marks}) "
            "AND date>=? AND date<=? ORDER BY date, symbol",
            conn, params=[*MOM_SYMBOLS, warmup_start, cutoff],
        )
        if bars.empty:
            return {}
        close = bars.pivot(index="date", columns="symbol", values="close")
        mom = sum(close / close.shift(lb) - 1 for lb in lookbacks) / len(lookbacks)
        out: dict[str, pd.Series] = {}
        for d in rb_list:
            if d not in mom.index:
                continue
            row = mom.loc[d].dropna()
            if row.empty:
                continue
            row[CASH_PROXY] = 0.0
            out[d] = row
        return out
    finally:
        conn.close()
