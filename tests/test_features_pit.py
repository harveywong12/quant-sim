"""White-box tests pinning the point-in-time discipline of qsim.features.factor.

A dependency upgrade (pandas/numpy) must never silently reopen a look-ahead
window in the factor pipeline, so these tests run against a synthetic
database instead of the real one.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from qsim.features.factor import (
    _filter_codes,
    _load_panels,
    compute_factor_ic_scores,
    score_date,
)

DATES = pd.bdate_range("2026-01-05", periods=60).strftime("%Y%m%d").tolist()
CODES = [f"{i:06d}" for i in range(1, 56)]
STOCK = "000001"
ST_NAME = "000099"
YOUNG = "000098"
LOW_PRICE = "000097"
UNIVERSE_DROP = "000010"
ALL_CODES = CODES + [LOW_PRICE, YOUNG, ST_NAME]


def _make_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE stock_bars (ts_code TEXT, date TEXT, open REAL, close REAL, vol REAL);
        CREATE TABLE stock_valuation (ts_code TEXT, date TEXT, pe_ttm REAL, pb REAL, dv_ratio REAL, total_mv REAL);
        CREATE TABLE stock_info (ts_code TEXT, name TEXT, industry TEXT, list_date TEXT);
        CREATE TABLE stock_universe_history (trade_date TEXT, ts_code TEXT);
        """
    )
    bar_rows = []
    val_rows = []
    for i, code in enumerate(ALL_CODES):
        growth = ((i + 1) % 5) * 0.0004
        base = 10.0 + (i % 10) * 0.5
        for t, date in enumerate(DATES):
            price = base * (1 + growth) ** t if code != LOW_PRICE else 1.0
            bar_rows.append((code, date, price, price, 1000.0))
            pe = 5.0 + (i % 7)
            if code == STOCK and date == DATES[30]:
                pe = 999.0
            val_rows.append(
                (code, date, pe, 1.0 + (i % 3) * 0.1, 2.0 + (i % 4) * 0.1, 1e8 + i * 1e6)
            )
    conn.executemany("INSERT INTO stock_bars VALUES (?,?,?,?,?)", bar_rows)
    conn.executemany("INSERT INTO stock_valuation VALUES (?,?,?,?,?,?)", val_rows)

    info_rows = [
        (code, f"S{i:03d}", "indA" if i % 2 else "indB", "20150101")
        for i, code in enumerate(ALL_CODES)
    ]
    info_rows = [
        (code, "ST测试" if code == ST_NAME else name, ind, lst)
        for code, name, ind, lst in info_rows
    ]
    info_rows = [
        (code, name, ind, DATES[45] if code == YOUNG else lst)
        for code, name, ind, lst in info_rows
    ]
    conn.executemany("INSERT INTO stock_info VALUES (?,?,?,?)", info_rows)

    conn.executemany(
        "INSERT INTO stock_universe_history VALUES (?,?)",
        [(DATES[0], code) for code in ALL_CODES]
        + [(DATES[40], code) for code in ALL_CODES if code != UNIVERSE_DROP],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "synthetic.db"
    _make_db(path)
    return path


def _rebalance_dates() -> list[str]:
    return [DATES[i] for i in range(10, 55, 5)]


def test_valuation_panels_are_shifted_one_day(db_path) -> None:
    panels = _load_panels(db_path)
    pe = panels["pe_ttm"]
    assert float(pe.loc[DATES[30], STOCK]) == 5.0
    assert float(pe.loc[DATES[31], STOCK]) == 999.0
    assert float(panels["pb"].loc[DATES[31], STOCK]) == float(
        panels["pb"].loc[DATES[30], STOCK]
    )


def test_price_factors_use_same_day_close(db_path) -> None:
    panels = _load_panels(db_path)
    close = panels["close"]
    expected = close.loc[DATES[30], STOCK] / close.loc[DATES[10], STOCK] - 1
    assert float(panels["mom_20"].loc[DATES[30], STOCK]) == pytest.approx(expected)


def test_score_date_applies_pit_filters(db_path) -> None:
    panels = _load_panels(db_path)
    before_drop = score_date(panels, DATES[30])
    assert UNIVERSE_DROP in before_drop.index

    scores = score_date(panels, DATES[50])
    assert STOCK in scores.index
    for excluded in (LOW_PRICE, YOUNG, ST_NAME, UNIVERSE_DROP):
        assert excluded not in scores.index


def test_filter_codes_st_low_price_young(db_path) -> None:
    panels = _load_panels(db_path)
    codes = _filter_codes(panels, DATES[50])
    assert STOCK in codes
    assert LOW_PRICE not in codes
    assert YOUNG not in codes
    assert ST_NAME not in codes


def test_ic_scores_live_safe_respect_as_of(db_path) -> None:
    rb = _rebalance_dates()
    as_of_early = rb[-3]
    as_of_late = rb[-1]
    early = compute_factor_ic_scores(db_path, rb, lookback_ic=4, label_days=2, as_of=as_of_early)
    late = compute_factor_ic_scores(db_path, rb, lookback_ic=4, label_days=2, as_of=as_of_late)

    assert set(early) == set(rb[: rb.index(as_of_early) + 1])
    for date, series in early.items():
        pd.testing.assert_series_equal(series, late[date], check_names=False)


def test_historical_path_skips_unsettled_label_dates(db_path) -> None:
    rb_edge = [DATES[55], DATES[58], DATES[59]]
    scores = compute_factor_ic_scores(db_path, rb_edge, lookback_ic=4, label_days=2, legacy_parity=True)
    assert set(scores) == {DATES[55]}
    assert scores[DATES[55]].nunique() > 1


def test_default_ic_path_does_not_require_future_labels(db_path) -> None:
    rb_edge = [DATES[55], DATES[58], DATES[59]]
    scores = compute_factor_ic_scores(db_path, rb_edge, lookback_ic=4, label_days=2)
    assert set(scores) == set(rb_edge)
    assert all(series.nunique() > 1 for series in scores.values())
