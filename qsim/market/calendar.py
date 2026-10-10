"""Trading calendar backed by observed bar dates.

Using observed dates from the database is more accurate for replay than a
weekday-only calendar because it naturally includes exchange holidays and
suspension-adjusted trading sessions.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterable

from qsim.domain.bars import parse_date


class TradingCalendar:
    def __init__(self, dates: Iterable[dt.date]) -> None:
        self._dates = sorted(set(dates))

    @classmethod
    def from_db(
        cls,
        db_path: str | Path,
        table: str = "bars",
        date_column: str = "date",
    ) -> "TradingCalendar":
        path = Path(db_path)
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f"SELECT DISTINCT {date_column} FROM {table} ORDER BY {date_column}"
            ).fetchall()
        finally:
            conn.close()
        return cls(parse_date(row[0]) for row in rows)

    @property
    def dates(self) -> list[dt.date]:
        return list(self._dates)

    def is_trading_day(self, day: dt.date) -> bool:
        return day in self._dates

    def previous(self, day: dt.date) -> dt.date | None:
        for candidate in reversed(self._dates):
            if candidate < day:
                return candidate
        return None

    def next(self, day: dt.date) -> dt.date | None:
        for candidate in self._dates:
            if candidate > day:
                return candidate
        return None

    def sessions_between(self, start: dt.date, end: dt.date) -> list[dt.date]:
        return [d for d in self._dates if start <= d <= end]
