"""Multi-factor score panel computed from the legacy quant.db snapshot.

This is a first migration of the old ``app/factor.py`` pipeline.  It uses the
core six factors (momentum, low-vol, liquidity, PE, PB, dividend yield) and
keeps the PIT discipline: valuation data is shifted by one day and ST/young/
low-price names are filtered.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

FACTOR_SIGNS = {
    "mom_20": 1,
    "vol_20": -1,
    "liq_20": 1,
    "pe_ttm": -1,
    "pb": -1,
    "dv": 1,
}

CORE_FACTORS = list(FACTOR_SIGNS)


def _load_panels(db_path: str | Path) -> dict[str, pd.DataFrame]:
    path = Path(db_path)
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        bars = pd.read_sql_query(
            "SELECT ts_code, date, open, close, vol FROM stock_bars", conn
        )
        val = pd.read_sql_query(
            "SELECT ts_code, date, pe_ttm, pb, dv_ratio, total_mv FROM stock_valuation", conn
        )
        info = pd.read_sql_query(
            "SELECT ts_code, name, industry, list_date FROM stock_info", conn
        ).set_index("ts_code")
        universe_history = pd.read_sql_query(
            "SELECT trade_date, ts_code FROM stock_universe_history", conn
        )
    finally:
        conn.close()

    close = bars.pivot(index="date", columns="ts_code", values="close")
    open_p = bars.pivot(index="date", columns="ts_code", values="open")
    vol = bars.pivot(index="date", columns="ts_code", values="vol")
    liq = vol * close

    panels = {
        "close": close,
        "open": open_p,
        "liq": liq,
        "pe_ttm": val.pivot(index="date", columns="ts_code", values="pe_ttm").shift(1),
        "pb": val.pivot(index="date", columns="ts_code", values="pb").shift(1),
        "dv": val.pivot(index="date", columns="ts_code", values="dv_ratio").shift(1),
        "total_mv": val.pivot(index="date", columns="ts_code", values="total_mv").shift(1),
        "_info": info,
        "_universe_history": universe_history,
    }
    panels["mom_20"] = close / close.shift(20) - 1
    # Legacy currently runs on pandas 3.x, whose pct_change default is
    # ``fill_method=None``.  Pin the behavior explicitly so the migration
    # does not depend on the target venv's pandas default (2.x forward-fills
    # here and changes vol_20, IC history, and ultimately tied rankings).
    panels["vol_20"] = close.pct_change(fill_method=None).rolling(20).std()
    panels["liq_20"] = liq.rolling(20).mean()
    return panels


def _winsorize(s: pd.Series, pct: float = 0.01) -> pd.Series:
    s = s.astype(float)
    lo, hi = s.quantile(pct), s.quantile(1 - pct)
    return s.clip(lo, hi)


def _zscore(s: pd.Series) -> pd.Series:
    s = s.astype(float)
    valid = s[s.notna()]
    if len(valid) < 5 or valid.std() == 0:
        return s * float("nan")
    return (s - valid.median()) / valid.std()


def _neutralize(s: pd.Series, log_mv: pd.Series, industry: pd.Series) -> pd.Series:
    df = pd.DataFrame({"f": s, "log_mv": log_mv, "ind": industry}).dropna()
    if len(df) < 30:
        return s
    dummies = pd.get_dummies(df["ind"], prefix="i", drop_first=True)
    X = np.column_stack([np.ones(len(df)), df["log_mv"], dummies.values])
    y = df["f"].values
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    out = pd.Series(np.nan, index=s.index)
    out.loc[df.index] = resid
    return out


def _universe_codes(history: pd.DataFrame, date: str) -> set[str]:
    known = history[history["trade_date"] <= date]
    if known.empty:
        return set()
    snapshot_date = known["trade_date"].max()
    return set(known.loc[known["trade_date"] == snapshot_date, "ts_code"].astype(str))


def _filter_codes(panels: dict[str, pd.DataFrame], date: str) -> set[str]:
    close = panels["close"]
    if date not in close.index:
        return set()
    valid = close.loc[date].dropna()
    codes = set(valid.index)
    info = panels["_info"]
    history = panels["_universe_history"]
    if history is not None and not history.empty:
        codes &= _universe_codes(history, date)
    st = set(info.index[info["name"].astype(str).str.contains("ST", na=False)])
    low = set(valid[valid <= 1.2].index)
    min_list = (pd.to_datetime(date, format="%Y%m%d") - pd.Timedelta(days=365)).strftime("%Y%m%d")
    listed = pd.to_datetime(info["list_date"], format="%Y%m%d", errors="coerce")
    young = set(info.index[listed.notna() & (listed > pd.Timestamp(min_list))])
    return codes - st - low - young


def score_date(
    panels: dict[str, pd.DataFrame],
    date: str,
    factors: list[str] | None = None,
) -> pd.Series:
    factors = factors or CORE_FACTORS
    ff = pd.concat(
        [panels[f].loc[date].rename(f) for f in factors if date in panels[f].index],
        axis=1,
    ).dropna(how="all")
    valid = _filter_codes(panels, date)
    ff = ff.loc[list(valid & set(ff.index))]

    log_mv = (
        np.log(panels["total_mv"].loc[date].astype(float))
        if date in panels["total_mv"].index
        else None
    )
    industry = panels["_info"]["industry"]

    processed: dict[str, pd.Series] = {}
    for f in factors:
        if f not in ff.columns:
            continue
        col = _winsorize(ff[f])
        col = _zscore(col)
        if log_mv is not None:
            col = _neutralize(col, log_mv, industry)
        processed[f] = col * FACTOR_SIGNS.get(f, 1)

    out = pd.DataFrame(processed)
    if out.empty:
        return pd.Series(dtype=float)
    return out.mean(axis=1).dropna()


def compute_factor_scores(
    db_path: str | Path,
    dates: Iterable[str],
) -> pd.DataFrame:
    panels = _load_panels(db_path)
    rows: dict[str, pd.Series] = {}
    for date in dates:
        rows[date] = score_date(panels, date)
    return pd.DataFrame(rows).T


def _features_on_date(
    panels: dict[str, pd.DataFrame],
    date: str,
    factors: list[str] | None = None,
) -> pd.DataFrame:
    factors = factors or CORE_FACTORS
    ff = pd.concat(
        [panels[f].loc[date].rename(f) for f in factors if date in panels[f].index],
        axis=1,
    ).dropna(how="all")
    valid = _filter_codes(panels, date)
    ff = ff.loc[list(valid & set(ff.index))]
    out = pd.DataFrame(index=ff.index)
    for col in ff.columns:
        if col not in factors:
            continue
        out[col] = _zscore(_winsorize(ff[col], 0.01))
    return out


def compute_factor_ic_scores(
    db_path: str | Path,
    dates: Iterable[str],
    lookback_ic: int = 12,
    label_days: int = 20,
    as_of: str | None = None,
    legacy_parity: bool = False,
) -> dict[str, pd.Series]:
    """Rolling IC scores; only matured labels enter IC estimation.

    Decision-date eligibility never depends on a future label by default.
    ``legacy_parity=True`` explicitly reproduces archived, biased sampling;
    it is for forensic comparisons only and cannot support real acceptance.
    Current ST/industry snapshots still prevent a full historical PIT claim.
    """
    panels = _load_panels(db_path)
    close = panels["close"]
    date_list = list(close.index)
    pos_map = {d: i for i, d in enumerate(date_list)}
    rb_list = sorted(d for d in dates if as_of is None or d <= as_of)
    live_safe = not legacy_parity or as_of is not None
    ic_history: dict[str, list[float]] = {f: [] for f in CORE_FACTORS}
    results: dict[str, pd.Series] = {}
    pending: list[tuple[str, pd.DataFrame]] = []

    for date in rb_list:
        current_pos = pos_map[date]
        remaining: list[tuple[str, pd.DataFrame]] = []
        for signal_date, feats in pending:
            signal_pos = pos_map[signal_date]
            if signal_pos + label_days > current_pos:
                remaining.append((signal_date, feats))
                continue
            label_date = date_list[signal_pos + label_days]
            label = (
                close.loc[label_date].reindex(feats.index)
                / close.loc[signal_date].reindex(feats.index)
                - 1
            ).dropna()
            common = feats.index.intersection(label.index)
            if len(common) >= 30:
                for f in CORE_FACTORS:
                    if f in feats.columns:
                        ic = feats[f].loc[common].corr(label.loc[common], method="spearman")
                        if ic == ic:
                            ic_history[f].append(ic)
        pending = remaining

        feats = _features_on_date(panels, date)
        if feats.empty:
            continue
        if not live_safe:
            # Legacy ml_engine.build_dataset keeps only symbols with a valid
            # forward label on every signal date.  Reuse that same sample set
            # for the historical ranking, not only for IC settlement.
            if current_pos + label_days >= len(date_list):
                continue
            label_date = date_list[current_pos + label_days]
            label = (
                close.loc[label_date].reindex(feats.index)
                / close.loc[date].reindex(feats.index)
                - 1
            ).dropna()
            common = feats.index.intersection(label.index)
            if len(common) < 50:
                continue
            feats = feats.loc[common]
        weights: dict[str, float] = {}
        for f in CORE_FACTORS:
            hist = ic_history[f][-lookback_ic:]
            if hist:
                weights[f] = max(0.0, sum(hist) / len(hist))
        cols = [f for f in CORE_FACTORS if f in feats.columns]
        if not cols:
            continue
        if weights:
            # Keep the legacy arithmetic order exactly.  The old pipeline
            # starts from a zero Series and adds factors in ``cols`` order;
            # changing this to Python's ``sum`` can change the last bits and
            # therefore break a deterministic tie policy.
            score = pd.Series(0.0, index=feats.index)
            for f in cols:
                if f in weights:
                    score += weights[f] * feats[f]
        else:
            score = feats[cols].mean(axis=1)
        # Scores that are equal within floating-point noise must not change
        # portfolio order between Python processes.  Legacy rankings are
        # effectively stable on the source row order, so normalize only the
        # noise-level tail and use a stable sort.
        normalized = score.round(12)
        ordered = sorted(
            normalized.index,
            key=lambda symbol: (-float(normalized.loc[symbol]), str(symbol)),
        )
        results[date] = normalized.loc[ordered]
        pending.append((date, feats))

    return results


def compute_factor_ic_scores_asof(
    db_path: str | Path,
    dates: Iterable[str],
    as_of: str,
    lookback_ic: int = 12,
    label_days: int = 20,
) -> dict[str, pd.Series]:
    """Compute multi-factor scores using information available through ``as_of``."""
    return compute_factor_ic_scores(
        db_path,
        dates,
        lookback_ic=lookback_ic,
        label_days=label_days,
        as_of=as_of,
    )
