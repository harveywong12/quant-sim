"""dividend_lowvol / etf_momentum 因子：PIT 宇宙过滤、合成排序、as_of 契约。"""

from __future__ import annotations

import sqlite3

from qsim.features.dividend_lowvol import compute_dividend_lowvol_scores
from qsim.features.etf_momentum import compute_momentum_scores


def _mkdb(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE stock_valuation (ts_code TEXT, date TEXT, dv_ratio REAL)")
    conn.execute("CREATE TABLE stock_universe_history (index_code TEXT, trade_date TEXT, ts_code TEXT, weight REAL)")
    conn.execute("CREATE TABLE stock_bars (ts_code TEXT, date TEXT, open REAL, close REAL)")
    conn.execute("CREATE TABLE bars (symbol TEXT, date TEXT, open REAL, close REAL)")
    return conn


def _fill_stock_db(conn):
    days = [f"202601{d:02d}" for d in range(1, 29)] + [f"202602{d:02d}" for d in range(1, 29)]
    codes = {"AAA": (100, 0.1, 5.0), "CCC": (100, 0.05, 1.0), "DDD": (100, 0.2, 3.0),
             "EEE": (100, 0.15, 2.0), "FFF": (100, 0.08, 0.5), "BBB": (100, 0.3, 9.0)}
    members = ("AAA", "CCC", "DDD", "EEE", "FFF")
    for i, d in enumerate(days):
        for code, (base, step, _dv) in codes.items():
            conn.execute("INSERT INTO stock_bars VALUES (?, ?, 1, ?)",
                         (code, d, base + i * step))
    for code in members:
        conn.execute("INSERT INTO stock_universe_history VALUES ('000300.SH', '20260101', ?, 1)", (code,))
    for code, (_base, _step, dv) in codes.items():
        conn.execute("INSERT INTO stock_valuation VALUES (?, '20260128', ?)", (code, dv))
    conn.commit()


def test_dividend_lowvol_scores_members_only_and_order(tmp_path, monkeypatch):
    import qsim.features.dividend_lowvol as dl
    monkeypatch.setattr(dl, "MIN_NAMES", 1)
    db = tmp_path / "q.db"
    conn = _mkdb(db)
    _fill_stock_db(conn)
    conn.close()
    scores = compute_dividend_lowvol_scores(db, ["20260227"], vol_days=10)
    assert "20260227" in scores
    row = scores["20260227"]
    # 5 个宇宙成员入选；BBB 股息率最高但非成员被排除
    assert "BBB" not in row.index and len(row.index) == 5
    # AAA 股息率最高且波动最低，两个因子同向，综合分必为第一
    assert row.idxmax() == "AAA"


def test_dividend_lowvol_as_of_filter(tmp_path):
    db = tmp_path / "q.db"
    conn = _mkdb(db)
    _fill_stock_db(conn)
    conn.close()
    scores = compute_dividend_lowvol_scores(db, ["20260227"], as_of="20260131")
    assert scores == {}


def _fill_etf_db(conn):
    days = [f"2026{m:02d}{d:02d}" for m in (1, 2, 3) for d in range(1, 29)]
    for i, d in enumerate(days):
        conn.execute("INSERT INTO bars VALUES ('510300', ?, 1, ?)", (d, 100 + i))
        conn.execute("INSERT INTO bars VALUES ('518880', ?, 1, ?)", (d, 200 - i * 0.5))
        conn.execute("INSERT INTO bars VALUES ('511880', ?, 1, 100)", (d,))
    conn.commit()


def test_momentum_scores_blend_and_cash_proxy(tmp_path, monkeypatch):
    import qsim.features.etf_momentum as em
    monkeypatch.setattr(em, "MOM_SYMBOLS", ["510300", "518880", "511880"])
    db = tmp_path / "q.db"
    conn = _mkdb(db)
    _fill_etf_db(conn)
    conn.close()
    scores = compute_momentum_scores(db, ["20260328"], lookbacks=(5,))
    row = scores["20260328"]
    assert row["510300"] > 0
    assert row["518880"] < 0
    assert row["511880"] == 0.0


def test_momentum_as_of_filter(tmp_path, monkeypatch):
    import qsim.features.etf_momentum as em
    monkeypatch.setattr(em, "MOM_SYMBOLS", ["510300", "518880", "511880"])
    db = tmp_path / "q.db"
    conn = _mkdb(db)
    _fill_etf_db(conn)
    conn.close()
    assert compute_momentum_scores(db, ["20260328"], lookbacks=(5,), as_of="20260115") == {}
