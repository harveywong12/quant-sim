"""Alpha158 factor panels ported from the legacy app.alpha_engine pipeline.

The factor definitions and rolling IC score path are kept compatible with the
legacy production implementation.  Equal-weight scores remain available as a
research helper, but the paper runner must use ``compute_alpha158_ic_scores``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

_WINDOW_FACTOR_KINDS = [
    "ROC", "MA", "STD", "MAX", "MIN", "RSV", "RANK", "CORR",
    "SUMP", "SUMN", "SUMD", "VMA", "VSTD", "VSUMP", "VSUMN",
    "VSUMD", "HIGH", "LOW", "AMPL", "CLOSE2HIGH", "CLOSE2LOW",
]
_WINDOWS = [5, 10, 20, 30, 60]

# Keep the complete custom factor set used by legacy ``compute_alpha158``.
# The earlier port listed only a hand-picked subset, which silently changed
# rolling-IC weights and therefore was not strict parity.
ALPHA_FACTORS = [
    f"{kind}{window}"
    for window in _WINDOWS
    for kind in _WINDOW_FACTOR_KINDS
] + ["VOL_MA_RATIO", "PRICE_POS", "RET_5_20", "HL_RANGE", "BODY"]


def _build_panels(db_path: str | Path, as_of: str | None = None) -> dict[str, pd.DataFrame]:
    """Load only information observable no later than ``as_of`` when set."""
    path = Path(db_path)
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        where = " WHERE date<=?" if as_of is not None else ""
        bars = pd.read_sql_query(
            "SELECT ts_code, date, open, high, low, close, vol, amount, vwap "
            f"FROM stock_bars{where}", conn,
            params=[as_of] if as_of is not None else None,
        )
    finally:
        conn.close()

    def p(col: str) -> pd.DataFrame:
        return bars.pivot(index="date", columns="ts_code", values=col)

    open_p, high_p, low_p, close_p = p("open"), p("high"), p("low"), p("close")
    typical = (high_p + low_p + close_p) / 3
    vwap_stored = p("vwap")
    vwap = (vwap_stored.where(vwap_stored.notna() & (vwap_stored > 0), typical)
            if not vwap_stored.empty else typical)
    amount = p("amount")
    return {
        "open": open_p,
        "high": high_p,
        "low": low_p,
        "close": close_p,
        "volume": p("vol"),
        "vwap": vwap,
        "amount": amount.where(amount.notna(), p("vol") * close_p)
        if not amount.empty else p("vol") * close_p,
    }


def compute_alpha158_panels(
    db_path: str | Path,
    max_stocks: int | None = None,
    as_of: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Full Alpha158 factor panel set, optionally truncated at ``as_of``."""
    data = _build_panels(db_path, as_of=as_of)
    if max_stocks:
        keep = list(data["close"].columns)[:max_stocks]
        data = {k: v[keep] for k, v in data.items()}
    return _factors_from_data(data)


def _factors_from_data(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    close = data["close"]
    high = data["high"]
    low = data["low"]
    vol = data["volume"]
    returns = close / close.shift(1) - 1
    factors: dict[str, pd.DataFrame] = {}
    windows = _WINDOWS

    for w in windows:
        rng = high.rolling(w).max() - low.rolling(w).min()
        factors[f"ROC{w}"] = close / close.shift(w) - 1
        factors[f"MA{w}"] = close.rolling(w).mean()
        factors[f"STD{w}"] = close.rolling(w).std()
        factors[f"MAX{w}"] = close.rolling(w).max()
        factors[f"MIN{w}"] = close.rolling(w).min()
        factors[f"RSV{w}"] = (close - low.rolling(w).min()) / rng.replace(0, pd.NA)
        factors[f"RANK{w}"] = close.rolling(w).rank(pct=True)
        factors[f"CORR{w}"] = close.rolling(w).corr(returns)
        factors[f"SUMP{w}"] = close.clip(lower=0).rolling(w).sum()
        factors[f"SUMN{w}"] = (-close.clip(upper=0)).rolling(w).sum()
        factors[f"SUMD{w}"] = close.rolling(w).sum()
        factors[f"VMA{w}"] = vol.rolling(w).mean()
        factors[f"VSTD{w}"] = vol.rolling(w).std()
        factors[f"VSUMP{w}"] = vol.clip(lower=0).rolling(w).sum()
        factors[f"VSUMN{w}"] = (-vol.clip(upper=0)).rolling(w).sum()
        factors[f"VSUMD{w}"] = vol.rolling(w).sum()
        factors[f"HIGH{w}"] = high.rolling(w).mean()
        factors[f"LOW{w}"] = low.rolling(w).mean()
        factors[f"AMPL{w}"] = (high - low) / close.replace(0, pd.NA)
        factors[f"CLOSE2HIGH{w}"] = close / high.rolling(w).max() - 1
        factors[f"CLOSE2LOW{w}"] = close / low.rolling(w).min() - 1

    factors["VOL_MA_RATIO"] = vol / vol.rolling(20).mean()
    factors["PRICE_POS"] = (close - low.rolling(60).min()) / (
        high.rolling(60).max() - low.rolling(60).min()
    ).replace(0, pd.NA)
    factors["RET_5_20"] = close / close.shift(5) - close / close.shift(20)
    factors["HL_RANGE"] = (high - low) / (high + low)
    open_p = data.get("open")
    if open_p is not None:
        factors["BODY"] = (close - open_p) / open_p.replace(0, pd.NA)
    return factors


def _zscore_series(s: pd.Series) -> pd.Series:
    s = s.astype(float)
    valid = s[s.notna()]
    if len(valid) < 5 or valid.std() == 0:
        return s * float("nan")
    return (s - valid.mean()) / valid.std()


def _score_date(factors: dict[str, pd.DataFrame], date: str) -> pd.Series:
    parts: list[pd.Series] = []
    for name in ALPHA_FACTORS:
        panel = factors.get(name)
        if panel is None or date not in panel.index:
            continue
        parts.append(_zscore_series(panel.loc[date].rename(name)))
    if not parts:
        return pd.Series(dtype=float)
    frame = pd.concat(parts, axis=1)
    return frame.mean(axis=1).dropna()


def compute_alpha158_scores(
    db_path: str | Path,
    dates: Iterable[str],
) -> pd.DataFrame:
    factors = compute_alpha158_panels(db_path)
    rows: dict[str, pd.Series] = {}
    for date in dates:
        rows[date] = _score_date(factors, date)
    return pd.DataFrame(rows).T


def _load_universe_history(db_path: str | Path, as_of: str | None = None) -> pd.DataFrame:
    path = Path(db_path)
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        where = " WHERE trade_date<=?" if as_of is not None else ""
        return pd.read_sql_query(
            f"SELECT trade_date, ts_code FROM stock_universe_history{where}", conn,
            params=[as_of] if as_of is not None else None,
        )
    finally:
        conn.close()


def compute_alpha158_ic_scores(
    db_path: str | Path,
    dates: Iterable[str],
    lookback_ic: int = 12,
    label_days: int = 20,
    as_of: str | None = None,
) -> dict[str, pd.Series]:
    """Rolling IC weighted Alpha158 scores, optionally strict point-in-time.

    ``as_of`` limits every input table and every requested signal date to the
    stated data cutoff.  This makes a historical replay mechanically unable
    to inspect later bars or later universe snapshots.
    """
    from qsim.features.factor import _winsorize, _zscore

    data = _build_panels(db_path, as_of=as_of)
    panels = _factors_from_data(data)
    close = data["close"]
    universe_history = _load_universe_history(db_path, as_of=as_of)

    rb_list = sorted(d for d in dates if as_of is None or d <= as_of)
    pos_map = {d: i for i, d in enumerate(close.index)}
    ic_history: dict[str, list[float]] = {name: [] for name in panels}
    results: dict[str, pd.Series] = {}
    pending_ic: list[tuple[str, dict[str, pd.Series]]] = []

    for date in rb_list:
        current_pos = pos_map[date]
        remaining: list[tuple[str, dict[str, pd.Series]]] = []
        for signal_date, signal_zs in pending_ic:
            signal_pos = pos_map[signal_date]
            if signal_pos + label_days > current_pos:
                remaining.append((signal_date, signal_zs))
                continue
            label_date = close.index[signal_pos + label_days]
            label = close.loc[label_date] / close.loc[signal_date] - 1
            for name, values in signal_zs.items():
                ic = values.corr(label, method="spearman")
                if ic == ic:
                    ic_history[name].append(ic)
        pending_ic = remaining

        ff: dict[str, pd.Series] = {}
        for name, panel in panels.items():
            if date in panel.index:
                ff[name] = panel.loc[date]
        if not ff:
            continue
        df = pd.DataFrame(ff)
        if not universe_history.empty:
            known = universe_history[universe_history["trade_date"] <= date]
            if not known.empty:
                snapshot_date = known["trade_date"].max()
                valid = set(known.loc[known["trade_date"] == snapshot_date, "ts_code"])
                df = df.loc[df.index.intersection(valid)]
        if df.empty:
            continue

        zs = {
            col: _zscore(_winsorize(df[col].astype(float), 0.01))
            for col in df.columns
        }
        weights: dict[str, float] = {}
        for col in zs:
            hist = ic_history[col][-lookback_ic:]
            if hist:
                weights[col] = sum(hist) / len(hist)
        if weights:
            score = pd.Series(0.0, index=df.index)
            for col, w in weights.items():
                if col in zs:
                    score += w * zs[col]
        else:
            score = pd.concat(zs.values(), axis=1).mean(axis=1)
        results[date] = score.sort_values(ascending=False)
        pending_ic.append((date, zs))

    return results
