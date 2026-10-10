from __future__ import annotations

import datetime as dt

from qsim.domain.bars import Bar, parse_date
from qsim.domain.instruments import Instrument, venue_from_symbol
from qsim.domain.portfolio import PortfolioSnapshot, Position, TargetPortfolio


def test_instrument_key_and_venue_inference() -> None:
    instrument = Instrument(symbol="512100", name="中证1000ETF")
    assert instrument.venue == "XSHG"
    assert instrument.key == "XSHG:512100"


def test_venue_from_symbol() -> None:
    assert venue_from_symbol("600000") == "XSHG"
    assert venue_from_symbol("000001") == "XSHE"
    assert venue_from_symbol("512100") == "XSHG"


def test_parse_date() -> None:
    assert parse_date("20260825") == dt.date(2026, 8, 25)
    assert parse_date("2026-08-25") == dt.date(2026, 8, 25)


def test_bar_to_dict() -> None:
    bar = Bar(
        date=dt.date(2026, 8, 25),
        open=1.0,
        high=1.1,
        low=0.9,
        close=1.05,
        volume=1000.0,
        amount=1050.0,
    )
    assert bar.to_dict()["date"] == "2026-08-25"


def test_portfolio_snapshot() -> None:
    position = Position(symbol="512100", qty=100, avg_cost=1.0)
    snapshot = PortfolioSnapshot(
        date=dt.date(2026, 8, 25),
        cash=1000.0,
        positions=(position,),
        total_value=1105.0,
    )
    assert snapshot.to_dict()["positions"][0]["symbol"] == "512100"


def test_target_portfolio() -> None:
    target = TargetPortfolio(date=dt.date(2026, 8, 25), weights={"512100": 1.0})
    assert target.weights["512100"] == 1.0
