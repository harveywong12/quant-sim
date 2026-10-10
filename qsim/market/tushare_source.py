"""Tushare online data source adapter.

Requires a Tushare token.  Offline Step 2 tests use ``SqliteDataSource``.
"""

from __future__ import annotations

import os

import pandas as pd

from qsim.market.data_source import DataSource


class TushareDataSource:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ.get("TUSHARE_TOKEN", "")

    def get_bars(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        from qsim.market.secure_tushare import make_client
        from qsim.services.native_market import query
        if not self.token:
            raise RuntimeError("TUSHARE_TOKEN is required for TushareDataSource")
        pro = make_client(self.token)
        rows = query(pro, 'daily', ts_code=symbol,
                     start_date=(start or "19700101").replace("-", ""),
                     end_date=(end or "20500101").replace("-", ""))
        columns = ["date", "open", "high", "low", "close", "volume", "amount"]
        if not rows:
            return pd.DataFrame(columns=columns)
        df = pd.DataFrame(rows).rename(columns={"trade_date": "date", "vol": "volume"})
        df["date"] = pd.to_datetime(df["date"]).dt.date
        # Tushare daily: vol in lots; amount in thousand CNY.
        df["volume"] = df["volume"].astype(float) * 100
        df["amount"] = df["amount"].astype(float) * 1000
        if df["date"].duplicated().any():
            raise RuntimeError("provider returned duplicate trading dates")
        return df[columns].sort_values("date").reset_index(drop=True)
