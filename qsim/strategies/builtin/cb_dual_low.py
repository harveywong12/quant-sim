"""CB dual-low defensive strategy migrated from data/strategies/cb_dual_low.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from qsim.strategies.base import Strategy

DB_PATH = "data/v2_market.db"


class CbDualLow(Strategy):
    name = "cb_dual_low"

    def __init__(
        self,
        top_n: int = 15,
        max_price: float = 130.0,
        min_balance_wan: float = 3000.0,
        min_years: float = 0.5,
        momentum_days: int = 20,
    ) -> None:
        self.top_n = int(top_n)
        self.max_price = float(max_price)
        self.min_balance_wan = float(min_balance_wan)
        self.min_years = float(min_years)
        self.momentum_days = int(momentum_days)

    def _connect_ro(self) -> sqlite3.Connection:
        path = Path(DB_PATH).resolve()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _universe_ready(self) -> bool:
        path = Path(DB_PATH)
        if not path.is_file():
            return False
        try:
            conn = self._connect_ro()
            row = conn.execute("SELECT COUNT(*) FROM cb_indicator").fetchone()
            conn.close()
        except sqlite3.Error:
            return False
        return bool(row and row[0])

    def dual_low_scores(self, asof: str | None = None) -> dict[str, float]:
        cutoff = asof or "99991231"
        conn = self._connect_ro()
        try:
            rows = conn.execute(
                """
                SELECT i.ts_code, i.dual_low
                  FROM cb_indicator i
                  JOIN cb_basic_snapshot b USING (ts_code)
                 WHERE i.trade_date = (SELECT MAX(trade_date) FROM cb_indicator
                                        WHERE trade_date <= :cutoff)
                   AND (b.delist_date IS NULL OR b.delist_date='')
                   AND COALESCE(b.remain_size, 1e12) >= :min_balance
                   AND i.close BETWEEN 90 AND :max_price
                   AND COALESCE(b.maturity_date, '99991231') >=
                       date('now', :min_years_days)
                   AND NOT EXISTS (
                       SELECT 1 FROM stock_name_history n
                        WHERE n.ts_code = b.stk_code
                          AND n.name LIKE '%ST%'
                          AND n.start_date <= strftime('%Y%m%d','now')
                          AND COALESCE(NULLIF(n.end_date,''), '99991231')
                              >= strftime('%Y%m%d','now'))
                 ORDER BY i.dual_low ASC
                 """,
                {
                    "max_price": self.max_price,
                    "min_balance": self.min_balance_wan * 10000.0,
                    "min_years_days": f"-{int(self.min_years*365)} days",
                    "cutoff": cutoff,
                },
            ).fetchall()
        finally:
            conn.close()
        return {str(r["ts_code"]): float(r["dual_low"]) for r in rows}

    def _basket_momentum(self, codes: list[str], asof: str | None = None) -> float:
        cutoff = asof or "99991231"
        conn = self._connect_ro()
        try:
            placeholders = ",".join(f":p{i}" for i in range(len(codes)))
            row = conn.execute(
                f"""
                SELECT AVG(CASE WHEN close_ref > 0
                           THEN close_latest / close_ref - 1 END)
                  FROM (
                    SELECT ts_code,
                           MAX(CASE WHEN rn = 1 THEN close END) close_latest,
                           MAX(CASE WHEN rn = {self.momentum_days} THEN close END)
                               close_ref
                      FROM (SELECT ts_code, close,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY ts_code ORDER BY trade_date DESC
                                   ) rn
                              FROM cb_indicator
                             WHERE ts_code IN ({placeholders})
                               AND trade_date <= :cutoff)
                      GROUP BY ts_code)
                 """,
                {**{f"p{i}": c for i, c in enumerate(codes)}, "cutoff": cutoff},
            ).fetchone()
        finally:
            conn.close()
        return float(row[0]) if row and row[0] is not None else 0.0

    def generate(self, df: pd.DataFrame | None) -> str:
        if not self._universe_ready():
            return "HOLD"
        asof = None
        try:
            if df is not None and len(df):
                asof = str(df["date"].iloc[-1]).replace("-", "")
        except Exception:
            asof = None
        scores = self.dual_low_scores(asof)
        if len(scores) < self.top_n // 2:
            return "HOLD"
        top = sorted(scores.items(), key=lambda kv: kv[1])[: self.top_n]
        momentum = self._basket_momentum([code for code, _ in top], asof)
        return "BUY" if momentum > 0 else "SELL"
