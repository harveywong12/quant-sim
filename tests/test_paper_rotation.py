from __future__ import annotations

import numpy as np
import pandas as pd

from qsim.engine.rotation_runner import run_rotation_backtest
from qsim.services.paper_daily import COST, _run_rotation
from qsim.storage.paper import get_conn, init_schema
from qsim.strategies.rotation import TrendFallback

SYM_A = "512100"
SYM_B = "000300"


def _bars(dates: pd.DatetimeIndex, closes: np.ndarray) -> pd.DataFrame:
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    return pd.DataFrame(
        {
            "date": dates,
            "open": opens,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": 1e8,
        }
    )


def _fixture():
    dates = pd.date_range("2026-01-02", periods=160, freq="B")
    base = np.arange(160, dtype=float)
    a = 10.0 + np.where(base < 90, base * 0.05, 4.5 - (base - 90) * 0.06)
    b = 20.0 + base * 0.01
    days = [d.strftime("%Y%m%d") for d in dates]
    return _bars(dates, a), _bars(dates, b), days


def _seed_account(db_path) -> tuple:
    conn = get_conn(str(db_path))
    init_schema(conn)
    conn.execute(
        "INSERT INTO qsim_accounts "
        "(strategy,symbol,initial_capital,cash,shares,avg_cost,code_hash,status,created_date) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("trend_fallback", None, 100000.0, 100000.0, 0, 0.0, "", "active", "20260901"),
    )
    conn.commit()
    account_id = conn.execute(
        "SELECT id FROM qsim_accounts WHERE strategy='trend_fallback'"
    ).fetchone()["id"]
    return conn, account_id


def test_paper_rotation_matches_shared_runner(tmp_path):
    bars_a, bars_b, days = _fixture()
    expected = run_rotation_backtest(
        {SYM_A: bars_a, SYM_B: bars_b},
        TrendFallback(symbol=SYM_A, fallback=SYM_B),
        100000.0,
        COST,
        start=days[0],
        end=days[-1],
    )
    assert expected["trades"], "fixture should trigger at least one rotation"

    conn, account_id = _seed_account(tmp_path / "paper.db")
    try:
        n = _run_rotation(conn, "trend_fallback", bars_a, bars_b,
                          SYM_A, SYM_B, days, account_id)
        conn.commit()
        assert n == len(expected["trades"])

        trades = conn.execute(
            "SELECT date,symbol,side,price,shares,amount,fee,reason,code_hash "
            "FROM qsim_trades ORDER BY date,symbol,side"
        ).fetchall()
        exp_trades = sorted(
            (t["date"], t["symbol"], t["side"], t["price"], t["shares"],
             t["amount"], t["fee"])
            for t in expected["trades"]
        )
        assert [tuple(r[:7]) for r in trades] == exp_trades
        assert all(r["reason"] == "rotation" for r in trades)

        snaps = conn.execute(
            "SELECT date,total_value,cash,position_value FROM qsim_snapshots ORDER BY date"
        ).fetchall()
        exp_snaps = [
            (r["date"], round(float(r["total_value"]), 2), round(float(r["cash"]), 2),
             round(float(r["position_value"]), 2))
            for _, r in expected["equity"].iterrows()
        ]
        assert [tuple(r) for r in snaps] == exp_snaps

        acct = conn.execute(
            "SELECT cash,shares,symbol,avg_cost FROM qsim_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        assert acct["cash"] == round(float(expected["cash"]), 2)
        assert acct["shares"] == int(expected["shares"])
        assert acct["symbol"] == expected["symbol"]
        assert acct["avg_cost"] == round(float(expected["avg_cost"]), 6)

        positions = conn.execute(
            "SELECT ts_code,shares,avg_cost,updated_date FROM qsim_positions WHERE account_id=?",
            (account_id,),
        ).fetchall()
        if expected["symbol"] is None:
            assert positions == []
        else:
            assert [(p["ts_code"], p["shares"], p["avg_cost"]) for p in positions] == [
                (expected["symbol"], int(expected["shares"]),
                 round(float(expected["avg_cost"]), 6))
            ]
    finally:
        conn.close()


def test_paper_rotation_uses_cost_model_slippage(tmp_path):
    bars_a, bars_b, days = _fixture()
    conn, account_id = _seed_account(tmp_path / "paper.db")
    try:
        _run_rotation(conn, "trend_fallback", bars_a, bars_b,
                      SYM_A, SYM_B, days, account_id)
        conn.commit()
        buys = conn.execute(
            "SELECT symbol,price FROM qsim_trades WHERE side='BUY' ORDER BY date"
        ).fetchall()
        assert buys, "fixture should produce a BUY"
        first = buys[0]
        assert first["symbol"] == SYM_B
        raw_open = float(bars_b["open"].iloc[1])
        assert first["price"] == COST.fill_price(raw_open, "BUY", SYM_B)
        assert first["price"] > raw_open
    finally:
        conn.close()
