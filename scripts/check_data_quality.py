#!/usr/bin/env python3
"""Check the local historical market data for obvious data-quality issues.

Reads only the read-only copies under ``data/`` and prints a per-symbol report.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUANT_DB = ROOT / "data" / "quant.db"
V2_MARKET_DB = ROOT / "data" / "v2_market.db"


def parse_date(value: str) -> dt.date:
    text = str(value).replace("-", "")
    return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def check_bars_table() -> None:
    conn = sqlite3.connect(f"file:{QUANT_DB}?mode=ro", uri=True)
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM bars ORDER BY symbol"
    ).fetchall()]

    print("=== bars table data quality ===")
    for symbol in symbols:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM bars "
            "WHERE symbol=? ORDER BY date",
            (symbol,),
        ).fetchall()

        duplicates = 0
        ohlc_bad = 0
        non_positive = 0
        negative_volume = 0
        long_gaps = 0
        prev_date: dt.date | None = None
        seen: set[str] = set()

        for date_str, open_, high, low, close, volume in rows:
            date = parse_date(date_str)
            if date_str in seen:
                duplicates += 1
            seen.add(date_str)

            if high < low or open_ < low or open_ > high or close < low or close > high:
                ohlc_bad += 1
            if open_ <= 0 or close <= 0:
                non_positive += 1
            if volume < 0:
                negative_volume += 1

            if prev_date is not None:
                gap_days = (date - prev_date).days
                if gap_days > 10:
                    long_gaps += 1
            prev_date = date

        print(
            f"{symbol:<8} rows={len(rows):<6} dup={duplicates:<3} "
            f"ohlc_bad={ohlc_bad:<3} non_pos={non_positive:<3} "
            f"neg_vol={negative_volume:<3} long_gaps={long_gaps:<3} "
            f"range={rows[0][0] if rows else '-'}..{rows[-1][0] if rows else '-'}"
        )
    conn.close()


def check_stock_bars_sample() -> None:
    conn = sqlite3.connect(f"file:{QUANT_DB}?mode=ro", uri=True)
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT ts_code FROM stock_bars ORDER BY ts_code LIMIT 5"
    ).fetchall()]
    print("\n=== stock_bars sample data quality ===")
    for symbol in symbols:
        rows = conn.execute(
            "SELECT date, open, high, low, close, vol FROM stock_bars "
            "WHERE ts_code=? ORDER BY date",
            (symbol,),
        ).fetchall()
        duplicates = 0
        ohlc_bad = 0
        non_positive = 0
        negative_volume = 0
        seen: set[str] = set()
        for date_str, open_, high, low, close, volume in rows:
            if date_str in seen:
                duplicates += 1
            seen.add(date_str)
            if high < low or open_ < low or open_ > high or close < low or close > high:
                ohlc_bad += 1
            if open_ <= 0 or close <= 0:
                non_positive += 1
            if volume < 0:
                negative_volume += 1
        print(
            f"{symbol:<12} rows={len(rows):<6} dup={duplicates:<3} "
            f"ohlc_bad={ohlc_bad:<3} non_pos={non_positive:<3} "
            f"neg_vol={negative_volume:<3} range={rows[0][0] if rows else '-'}..{rows[-1][0] if rows else '-'}"
        )
    conn.close()


def check_v2_market_consistency() -> None:
    conn = sqlite3.connect(f"file:{V2_MARKET_DB}?mode=ro", uri=True)
    print("\n=== v2_market.db adj_factor / corporate_actions consistency ===")
    for table in ("adj_factor", "corporate_actions", "stock_name_history", "cb_indicator"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:<22} rows={count}")
    conn.close()


if __name__ == "__main__":
    check_bars_table()
    check_stock_bars_sample()
    check_v2_market_consistency()
