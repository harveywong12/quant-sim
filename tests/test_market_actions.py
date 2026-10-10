from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from qsim.market.adjust import load_adj_factors
from qsim.market.corporate_actions import load_corporate_actions

ROOT = Path(__file__).resolve().parents[1]
V2_MARKET_DB = ROOT / "data" / "v2_market.db"

pytestmark = pytest.mark.skipif(
    not V2_MARKET_DB.is_file(),
    reason="data/v2_market.db is required for corporate action tests",
)


def test_load_corporate_actions_contains_known_split() -> None:
    actions = load_corporate_actions(V2_MARKET_DB)
    known = [a for a in actions if a.symbol == "513100" and a.kind == "SPLIT"]
    assert known
    assert known[0].ex_date == dt.date(2022, 1, 14)
    assert known[0].ratio is not None


def test_load_adj_factors_sorted() -> None:
    factors = load_adj_factors(V2_MARKET_DB, ts_code="000001.SZ")
    assert len(factors) > 100
    dates = [f.trade_date for f in factors]
    assert dates == sorted(dates)
