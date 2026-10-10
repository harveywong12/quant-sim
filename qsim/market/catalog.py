"""Small read-only market catalog combining bars and calendar."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from qsim.market.calendar import TradingCalendar
from qsim.market.data_source import SqliteDataSource


class SqliteMarketCatalog:
    def __init__(
        self,
        db_path: str | Path,
        table: str = "bars",
        symbol_column: str = "symbol",
        date_column: str = "date",
        calendar_table: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.data_source = SqliteDataSource(
            db_path,
            table=table,
            symbol_column=symbol_column,
            date_column=date_column,
        )
        calendar_table = calendar_table or table
        self._calendar = TradingCalendar.from_db(
            db_path,
            table=calendar_table,
            date_column=date_column,
        )

    def get_bars(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        return self.data_source.get_bars(symbol, start, end)

    @property
    def calendar(self) -> TradingCalendar:
        return self._calendar
