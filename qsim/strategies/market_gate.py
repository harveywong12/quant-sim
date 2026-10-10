"""Point-in-time market gate used only by the isolated multi_factor v3 paper sleeve."""

from __future__ import annotations

import math
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from typing import Iterable

import pandas as pd

BREADTH_THRESHOLD = 1.0 / 3.0
TARGET_ANNUAL_VOL = 0.20


def _regime(history: pd.Series) -> tuple[str | None, float | None]:
    history = history.dropna()
    if len(history) < 65:
        return None, None
    fast = float(history.iloc[-20:].mean())
    slow = float(history.iloc[-60:].mean())
    prior_fast = float(history.iloc[-25:-5].mean())
    slope = fast / prior_fast - 1 if prior_fast else 0.0
    if fast > slow and slope > 0:
        return "bull", slope
    if fast < slow and slope < 0:
        return "bear", slope
    return "range", slope


def _breadth(
    date: str, close: pd.DataFrame, members: dict[str, set[str]], snapshots: list[str],
) -> tuple[float | None, int]:
    index = bisect_right(snapshots, date) - 1
    if index < 0:
        return None, 0
    codes = sorted(members[snapshots[index]])
    history = close.loc[:date].tail(60).reindex(columns=codes)
    if history.empty:
        return None, 0
    valid_codes = list(history.count()[lambda values: values >= 30].index)
    if len(valid_codes) < 100:
        return None, len(valid_codes)
    history = history[valid_codes]
    current, mean = history.iloc[-1], history.mean()
    valid = current.notna() & mean.notna()
    current, mean = current[valid], mean[valid]
    if len(current) < 100:
        return None, len(current)
    return float((current > mean).mean()), len(current)


def multi_factor_v3_allocations(
    conn: sqlite3.Connection, history_days: Iterable[str], signal_dates: Iterable[str],
) -> tuple[dict[str, float], list[dict[str, object]]]:
    """Return next-open target allocations using information at each signal close.

    A missing warm-up window leaves the sleeve at 100%; it never turns missing
    history into a bearish signal.  All database queries are bounded by the
    latest requested signal date, so later observations cannot enter a gate.
    """
    days = list(history_days)
    signals = sorted(set(signal_dates))
    if not days or not signals:
        return {}, []
    as_of = signals[-1]
    stock = pd.read_sql_query(
        "SELECT ts_code,date,close FROM stock_bars WHERE date<=? ORDER BY date,ts_code",
        conn, params=[as_of],
    )
    if stock.empty:
        return {date: 1.0 for date in signals}, []
    close = stock.pivot(index="date", columns="ts_code", values="close")
    etf = pd.read_sql_query(
        "SELECT date,close FROM bars WHERE symbol='512100' AND date<=? ORDER BY date",
        conn, params=[as_of],
    )
    etf_close = etf.set_index("date").close.reindex(days).ffill() if not etf.empty else pd.Series(dtype=float)
    rows = conn.execute(
        "SELECT trade_date,ts_code FROM stock_universe_history "
        "WHERE index_code='000300.SH' AND trade_date<=? ORDER BY trade_date,ts_code",
        [as_of],
    ).fetchall()
    members: dict[str, set[str]] = defaultdict(set)
    for snapshot, code in rows:
        members[str(snapshot)].add(str(code))
    snapshots = sorted(members)
    allocations: dict[str, float] = {}
    records: list[dict[str, object]] = []
    for date in signals:
        history = etf_close.loc[:date] if not etf_close.empty else pd.Series(dtype=float)
        regime, slope = _regime(history)
        breadth, valid_count = _breadth(date, close, members, snapshots) if snapshots else (None, 0)
        returns = history.pct_change(fill_method=None).dropna().tail(20)
        annual_vol = float(returns.std() * math.sqrt(252)) if len(returns) >= 20 else None
        trend_ok = regime != "bear" if regime is not None else True
        breadth_ok = breadth >= BREADTH_THRESHOLD if breadth is not None else True
        vol_weight = min(1.0, TARGET_ANNUAL_VOL / annual_vol) if annual_vol and annual_vol > 0 else 1.0
        allocation = vol_weight if trend_ok and breadth_ok else 0.0
        allocations[date] = allocation
        records.append({
            "signal_date": date, "regime": regime or "warmup", "slope_5d": slope,
            "breadth": breadth, "breadth_valid_count": valid_count,
            "annual_vol_20d": annual_vol, "target_allocation": allocation,
            "trend_ok": trend_ok, "breadth_ok": breadth_ok,
        })
    return allocations, records
