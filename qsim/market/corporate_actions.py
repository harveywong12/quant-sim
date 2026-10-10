"""Corporate action store backed by the v2 market database."""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CorporateAction:
    id: int
    venue: str
    symbol: str
    ex_date: dt.date
    kind: str
    ratio: Decimal | None
    cash_per_share: Decimal | None
    currency: str
    note: str


def load_corporate_actions(db_path: str | Path) -> list[CorporateAction]:
    path = Path(db_path)
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, venue, symbol, ex_date, kind, ratio, cash_per_share, "
            "currency, note FROM corporate_actions ORDER BY ex_date"
        ).fetchall()
    finally:
        conn.close()

    actions: list[CorporateAction] = []
    for row in rows:
        actions.append(
            CorporateAction(
                id=row[0],
                venue=row[1],
                symbol=row[2],
                ex_date=dt.date.fromisoformat(row[3]),
                kind=row[4],
                ratio=Decimal(row[5]) if row[5] is not None else None,
                cash_per_share=Decimal(row[6]) if row[6] is not None else None,
                currency=row[7],
                note=row[8],
            )
        )
    return actions
