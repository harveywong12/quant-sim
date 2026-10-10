from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from qsim.engine.account import Account
from qsim.engine.costs import CostModel
from qsim.engine.ledger import Ledger
from qsim.engine.matcher import buy, sell
from qsim.engine.runner import run_backtest


def test_fill_price_and_fee() -> None:
    cost = CostModel()
    assert cost.fill_price(10.0, "BUY", "512100") == 10.005
    assert cost.fill_price(10.0, "SELL", "512100") == 9.995
    assert cost.calc_fee(10.005, 1000, "BUY", "512100") == 5.0


def test_limit_blocked() -> None:
    cost = CostModel()
    assert cost.limit_blocked("BUY", 11.0, 10.0, "600000") is not None
    assert cost.limit_blocked("SELL", 11.0, 10.0, "600000") is None
    assert cost.limit_blocked("BUY", 11.0, 10.0, "300001") is None


def test_buy_full_cash() -> None:
    cost = CostModel()
    cash, shares, trade, reason = buy(
        cash=100000.0,
        shares=0,
        open_price=10.0,
        date="20260101",
        cost=cost,
        symbol="600000",
    )
    assert reason is None
    assert trade is not None
    assert trade.side == "BUY"
    assert trade.shares == 9900
    assert trade.cash_after > 0


def test_sell_with_avg_cost() -> None:
    cost = CostModel()
    cash, shares, trade, reason = sell(
        cash=0.0,
        shares=1000,
        open_price=10.0,
        date="20260102",
        cost=cost,
        symbol="600000",
        avg_cost=9.0,
    )
    assert reason is None
    assert trade is not None
    assert trade.side == "SELL"
    assert trade.shares == 1000
    assert trade.profit is not None and trade.profit > 0


def test_ledger_appends_entries() -> None:
    ledger = Ledger(initial_cash=100000)
    ledger.record("20260101", "SNAPSHOT", {"cash": 100000})
    ledger.record("20260102", "BUY", {"shares": 100})
    assert [e.seq for e in ledger.entries] == [1, 2]
    assert len(ledger.trades()) == 1
    assert len(ledger.snapshots()) == 1


class QueueStrategy:
    def __init__(self, signals: list) -> None:
        self.signals = signals
        self.name = "queue"

    def generate(self, df: pd.DataFrame):
        return self.signals.pop(0)


def _bars(days: list[tuple[dt.date, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": day, "open": o, "high": max(o, c), "low": min(o, c), "close": c, "volume": 0.0}
            for day, o, c in days
        ]
    )


def test_runner_buy_then_sell() -> None:
    bars = _bars(
        [
            (dt.date(2026, 1, 1), 10.0, 10.0),
            (dt.date(2026, 1, 2), 10.0, 11.0),
            (dt.date(2026, 1, 3), 11.0, 12.0),
        ]
    )
    result = run_backtest(
        bars,
        QueueStrategy(["BUY", "SELL"]),
        initial_cash=100000.0,
        cost=CostModel(),
        symbol="600000",
    )
    assert len(result["trades"]) == 2
    assert [t["side"] for t in result["trades"]] == ["BUY", "SELL"]
    assert len(result["equity"]) == 3
    assert result["metrics"]["final_value"] > 0


def test_runner_skips_limit_up_buy() -> None:
    bars = _bars(
        [
            (dt.date(2026, 1, 1), 10.0, 10.0),
            (dt.date(2026, 1, 2), 11.0, 11.0),
        ]
    )
    result = run_backtest(
        bars,
        QueueStrategy(["BUY", "HOLD"]),
        initial_cash=100000.0,
        cost=CostModel(),
        symbol="600000",
    )
    assert result["trades"] == []
    assert len(result["skips"]) == 1
    assert "涨停" in result["skips"][0]["reason"]


def test_account_position_value() -> None:
    account = Account(symbol="600000", cash=50000, shares=1000)
    assert account.position_value(10.0) == 10000
    assert account.total_value(10.0) == 60000
