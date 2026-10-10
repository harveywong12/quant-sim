"""AKShare online data source adapter.

This adapter requires network access and is intentionally not covered by the
offline Step 2 tests.  The deterministic replay source is ``SqliteDataSource``.
"""

from __future__ import annotations

import pandas as pd

from qsim.market.data_source import DataSource


def _is_etf(symbol: str) -> bool:
    return symbol.startswith(("1", "5"))


class AKShareDataSource:
    """Fetch daily bars through AKShare.

    ETF symbols use ``fund_etf_hist_em``; stock symbols use ``stock_zh_a_hist``.
    """

    def get_bars(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        import akshare as ak

        start_date = start or "19700101"
        end_date = end or "20500101"
        if _is_etf(symbol):
            raw = ak.fund_etf_hist_em(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="",
            )
        else:
            raw = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="",
            )

        rename = {
            "日期": "date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
        }
        df = raw.rename(columns=rename)
        df = df[list(rename.values())].copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df
