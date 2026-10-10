"""Market data source abstraction and deterministic SQLite implementation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from qsim.domain.bars import parse_date

STANDARD_COLUMNS = ("date", "open", "high", "low", "close", "volume", "amount")


class DataSource(Protocol):
    def get_bars(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame: ...


class SqliteDataSource:
    """Read bars from a read-only SQLite database.

    Expected tables:

    * ETF table ``bars``: symbol/date/open/high/low/close/volume
    * Stock table ``stock_bars``: ts_code/date/open/high/low/close/vol/amount
    """

    def __init__(
        self,
        db_path: str | Path,
        table: str = "bars",
        symbol_column: str = "symbol",
        date_column: str = "date",
        volume_column: str | None = None,
        amount_column: str | None = None,
        volume_multiplier: float = 1.0,
        amount_multiplier: float = 1.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.table = table
        self.symbol_column = symbol_column
        self.date_column = date_column
        self.volume_column = volume_column
        self.amount_column = amount_column
        self.volume_multiplier = volume_multiplier
        self.amount_multiplier = amount_multiplier

    def _columns(self) -> list[str]:
        conn = sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True)
        try:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({self.table})")]
        finally:
            conn.close()

    def _select_expr(self, cols: list[str]) -> str:
        mapping = {
            "date": self.date_column,
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": self.volume_column or ("volume" if "volume" in cols else "vol"),
            "amount": self.amount_column or ("amount" if "amount" in cols else None),
        }
        parts = [f"{column} AS {alias}" for alias, column in mapping.items() if column in cols]
        return ", ".join(parts) or "*"

    def get_bars(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        cols = self._columns()
        select_expr = self._select_expr(cols)
        sql = (
            f"SELECT {select_expr} FROM {self.table} "
            f"WHERE {self.symbol_column} = ?"
        )
        params: list[Any] = [symbol]
        if start is not None:
            sql += f" AND REPLACE({self.date_column},'-','') >= ?"
            params.append(str(start).replace("-", ""))
        if end is not None:
            sql += f" AND REPLACE({self.date_column},'-','') <= ?"
            params.append(str(end).replace("-", ""))
        sql += f" ORDER BY REPLACE({self.date_column},'-','')"

        conn = sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True)
        try:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
        finally:
            conn.close()

        if not rows:
            return pd.DataFrame(columns=STANDARD_COLUMNS)
        df = pd.DataFrame(rows, columns=columns)
        df["date"] = df["date"].map(parse_date)
        for column in STANDARD_COLUMNS:
            if column not in df.columns:
                df[column] = 0.0 if column in ("volume", "amount") else None
        df["volume"] = df["volume"].astype(float) * self.volume_multiplier
        df["amount"] = df["amount"].astype(float) * self.amount_multiplier
        return df[list(STANDARD_COLUMNS)]
