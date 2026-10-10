from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from qsim.market.calendar import TradingCalendar
from qsim.market.catalog import SqliteMarketCatalog
from qsim.market.data_source import SqliteDataSource

ROOT = Path(__file__).resolve().parents[1]
QUANT_DB = ROOT / "data" / "quant.db"

pytestmark = pytest.mark.skipif(
    not QUANT_DB.is_file(),
    reason="data/quant.db is required for market data tests",
)


def test_sqlite_data_source_etf_bars() -> None:
    source = SqliteDataSource(QUANT_DB, table="bars", symbol_column="symbol")
    df = source.get_bars("512100", start="20240102", end="20240110")
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume", "amount"]
    assert len(df) > 0
    assert df["date"].iloc[0] == dt.date(2024, 1, 2)
    assert df["date"].is_monotonic_increasing


def test_sqlite_data_source_stock_bars() -> None:
    source = SqliteDataSource(
        QUANT_DB,
        table="stock_bars",
        symbol_column="ts_code",
        volume_column="vol",
        amount_column="amount",
    )
    df = source.get_bars("000001.SZ", start="20260801", end="20260810")
    assert len(df) > 0
    assert "volume" in df.columns
    assert "amount" in df.columns


def test_sqlite_data_source_empty() -> None:
    source = SqliteDataSource(QUANT_DB, table="bars", symbol_column="symbol")
    df = source.get_bars("512100", start="20990101", end="20991231")
    assert df.empty
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume", "amount"]


def test_trading_calendar_from_db() -> None:
    calendar = TradingCalendar.from_db(QUANT_DB, table="bars")
    assert len(calendar.dates) > 100
    assert calendar.is_trading_day(dt.date(2026, 8, 25))
    previous = calendar.previous(dt.date(2026, 8, 25))
    assert previous is not None and previous < dt.date(2026, 8, 25)


def test_sqlite_market_catalog() -> None:
    catalog = SqliteMarketCatalog(QUANT_DB, table="bars")
    df = catalog.get_bars("512100", start="20240102", end="20240105")
    assert len(df) > 0
    assert len(catalog.calendar.dates) > 100
