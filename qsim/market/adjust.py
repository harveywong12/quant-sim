"""Adjustment factor access.

Step 2 only exposes the factors stored in the v2 market database.  The exact
qfq/hfq formula belongs to the engine layer where it can be tested against the
legacy implementation.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from qsim.domain.bars import parse_date


@dataclass(frozen=True, slots=True)
class AdjFactor:
    ts_code: str
    trade_date: dt.date
    factor: float


def load_adj_factors(
    db_path: str | Path,
    ts_code: str | None = None,
) -> list[AdjFactor]:
    path = Path(db_path)
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        sql = "SELECT ts_code, trade_date, adj_factor FROM adj_factor"
        params: list[str] = []
        if ts_code is not None:
            sql += " WHERE ts_code = ?"
            params.append(ts_code)
        sql += " ORDER BY trade_date"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    return [
        AdjFactor(
            ts_code=row[0],
            trade_date=parse_date(row[1]),
            factor=float(row[2]),
        )
        for row in rows
    ]
