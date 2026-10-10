"""Read-only, next-session-effective shadow meta portfolio.

This module deliberately never creates or mutates a qsim trading ledger.  It
replays existing strategy NAV marks, emits an audit artifact, and is therefore
safe to schedule alongside the paper engine while the meta policy warms up.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "quant.db"
CONFIG_PATH = ROOT / "config" / "meta-shadow-v1.json"
MIGRATION_PATH = ROOT / "config" / "strategy-migration.json"
ARTIFACT_DIR = ROOT / "artifacts" / "meta_shadow_v1"
SHANGHAI_TZ = timezone(timedelta(hours=8))


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_strategy_navs() -> dict[str, dict[str, float]]:
    """Return source NAVs with an explicitly read-only SQLite connection."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT strategy, date, total_value FROM qsim_snapshots ORDER BY date, strategy"
        ).fetchall()
    finally:
        conn.close()
    navs: dict[str, dict[str, float]] = defaultdict(dict)
    for strategy, date, total_value in rows:
        value = float(total_value or 0)
        if value > 0:
            navs[str(strategy)][str(date)] = value
    return dict(navs)


def _migration_states() -> dict[str, str]:
    payload = _load_json(MIGRATION_PATH)
    return {str(row["name"]): str(row.get("state") or "unknown")
            for row in payload.get("strategies") or []}


def _sample_std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(variance, 0.0))


def _normalise_capped(weights: dict[str, float], cap: float) -> dict[str, float]:
    """Normalise non-negative sleeve weights without ever exceeding `cap`."""
    active = {name: max(0.0, float(value)) for name, value in weights.items()}
    if not active:
        return {}
    total = sum(active.values())
    if total <= 0:
        active = {name: 1.0 / len(active) for name in active}
    else:
        active = {name: value / total for name, value in active.items()}
    remaining = set(active)
    result = {name: 0.0 for name in active}
    budget = 1.0
    while remaining and budget > 0:
        subtotal = sum(active[name] for name in remaining)
        if subtotal <= 0:
            share = budget / len(remaining)
            for name in list(remaining):
                result[name] = min(cap, share)
            break
        saturated = []
        for name in remaining:
            proposal = budget * active[name] / subtotal
            if proposal >= cap:
                result[name] = cap
                budget -= cap
                saturated.append(name)
        if not saturated:
            for name in remaining:
                result[name] = budget * active[name] / subtotal
            break
        remaining.difference_update(saturated)
    return {name: round(value, 8) for name, value in result.items() if value > 0}


def _direct_weight_distance(left: dict[str, float], right: dict[str, float]) -> float:
    keys = set(left) | set(right)
    return 0.5 * sum(abs(float(left.get(key, 0.0)) - float(right.get(key, 0.0))) for key in keys)


def _eligible_on(
    date: str,
    navs: dict[str, dict[str, float]],
    states: dict[str, str],
    config: dict[str, Any],
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    allowed = set(config["allowed_strategy_states"])
    minimum = int(config["min_history_days"])
    detail: dict[str, dict[str, Any]] = {}
    sleeves: dict[str, list[str]] = {}
    for sleeve, members in (config.get("sleeves") or {}).items():
        active: list[str] = []
        for name in members:
            observations = sum(1 for point in navs.get(name, {}) if point <= date)
            has_mark = date in navs.get(name, {})
            state = states.get(name, "not_migrated")
            eligible = state in allowed and has_mark and observations >= minimum
            if state not in allowed:
                reason = f"migration_state={state}"
            elif not has_mark:
                reason = "no_nav_mark_on_date"
            elif observations < minimum:
                reason = f"warming_up:{observations}/{minimum}"
            else:
                reason = "eligible"
            detail[name] = {
                "state": state, "observations": observations, "has_mark": has_mark,
                "eligible": eligible, "reason": reason,
            }
            if eligible:
                active.append(name)
        if active:
            sleeves[str(sleeve)] = active
    return sleeves, detail


def _returns_until(values: dict[str, float], date: str, window: int) -> list[float]:
    rows = [(day, nav) for day, nav in values.items() if day <= date]
    rows.sort()
    returns = [rows[index][1] / rows[index - 1][1] - 1
               for index in range(1, len(rows)) if rows[index - 1][1] > 0]
    return returns[-window:]


def _target_for_close(
    date: str,
    navs: dict[str, dict[str, float]],
    states: dict[str, str],
    config: dict[str, Any],
    fuse_active: bool,
) -> tuple[dict[str, float], dict[str, float], dict[str, Any]]:
    sleeves, eligibility = _eligible_on(date, navs, states, config)
    minimum_sleeves = int(config["minimum_active_sleeves"])
    if len(sleeves) < minimum_sleeves:
        return {}, {}, {
            "status": "warming_up", "eligibility": eligibility, "active_sleeves": sleeves,
            "reason": f"active_sleeves:{len(sleeves)}/{minimum_sleeves}",
            "forecast_vol_annual": None, "risk_scale": 0.0,
        }
    allocation = config["allocation"]
    window = int(allocation["inverse_vol_window"])
    sleeve_returns: dict[str, list[float]] = {}
    sleeve_vols: dict[str, float] = {}
    for sleeve, names in sleeves.items():
        series = [_returns_until(navs[name], date, window) for name in names]
        common = min((len(row) for row in series), default=0)
        if common:
            sleeve_returns[sleeve] = [sum(row[index] for row in series) / len(series)
                                      for index in range(common)]
        else:
            sleeve_returns[sleeve] = []
        sleeve_vols[sleeve] = _sample_std(sleeve_returns[sleeve])
    equal = 1.0 / len(sleeves)
    positive_vols = [value for value in sleeve_vols.values() if value > 0]
    inverse = ({name: 1.0 / value for name, value in sleeve_vols.items()}
               if len(positive_vols) == len(sleeve_vols) else {name: 1.0 for name in sleeves})
    inv_total = sum(inverse.values())
    blend = float(allocation["inverse_vol_blend"])
    raw = {name: blend * equal + (1.0 - blend) * inverse[name] / inv_total for name in sleeves}
    sleeve_weights = _normalise_capped(raw, float(allocation["sleeve_weight_cap"]))
    # A deliberately conservative no-correlation forecast: weighted arithmetic
    # volatility instead of assuming diversification that the short sample cannot prove.
    forecast = sum(sleeve_weights[name] * sleeve_vols[name] for name in sleeve_weights) * math.sqrt(252)
    target_vol = float(allocation["portfolio_vol_target_annual"])
    risk_scale = min(1.0, target_vol / forecast) if forecast > 0 else 1.0
    if fuse_active:
        risk_scale *= float(config["drawdown_fuse"]["exposure_scale"])
    direct: dict[str, float] = {}
    for sleeve, sleeve_weight in sleeve_weights.items():
        names = sleeves[sleeve]
        for name in names:
            direct[name] = round(sleeve_weight * risk_scale / len(names), 8)
    # Keep the retained grid sleeve available as a defensive diversifier,
    # but cap its direct portfolio weight until a fresh OOS review supports
    # increasing exposure.  A cap is intentionally conservative: it does
    # not renormalise the difference back into the other strategies.
    strategy_caps = allocation.get("strategy_weight_caps") or {}
    for name, cap in strategy_caps.items():
        if name in direct:
            direct[name] = min(direct[name], max(0.0, float(cap)))
    status = "shadow_warming_up" if min(len(row) for row in sleeve_returns.values()) < window else "shadow_ready"
    return direct, sleeve_weights, {
        "status": status, "eligibility": eligibility, "active_sleeves": sleeves,
        "sleeve_vol_daily": sleeve_vols, "forecast_vol_annual": forecast,
        "risk_scale": risk_scale, "reason": "target_generated_at_close",
        "strategy_weight_caps": strategy_caps,
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def run_shadow() -> dict[str, Any]:
    config = _load_json(CONFIG_PATH)
    states = _migration_states()
    navs = _read_strategy_navs()
    dates = sorted({date for series in navs.values() for date in series})
    if not dates:
        raise RuntimeError("qsim_snapshots is empty; meta shadow has no input")
    allocation = config["allocation"]
    nav = 100000.0
    peak = nav
    current: dict[str, float] = {}
    current_sleeves: dict[str, float] = {}
    pending: dict[str, float] | None = None
    pending_meta: dict[str, Any] | None = None
    last_rebalance_date: str | None = None
    fuse_active = False
    history: list[dict[str, Any]] = []
    for index, date in enumerate(dates):
        executed_turnover = 0.0
        executed_cost = 0.0
        executed_from: str | None = None
        if pending is not None:
            executed_turnover = _direct_weight_distance(current, pending)
            executed_cost = nav * executed_turnover * float(allocation["one_way_cost"])
            nav -= executed_cost
            current = pending
            current_sleeves = dict((pending_meta or {}).get("sleeve_weights") or {})
            executed_from = str(pending_meta.get("generated_on")) if pending_meta else None
            last_rebalance_date = date
            pending = None
            pending_meta = None
        strategy_return = 0.0
        for name, weight in current.items():
            ordered = sorted(day for day in navs.get(name, {}) if day <= date)
            if len(ordered) < 2 or ordered[-1] != date:
                continue
            previous, today = ordered[-2], ordered[-1]
            old_nav, new_nav = navs[name][previous], navs[name][today]
            if old_nav > 0:
                strategy_return += weight * (new_nav / old_nav - 1.0)
        nav *= 1.0 + strategy_return
        peak = max(peak, nav)
        drawdown = nav / peak - 1.0 if peak > 0 else 0.0
        fuse = config["drawdown_fuse"]
        if not fuse_active and drawdown <= -float(fuse["trigger"]):
            fuse_active = True
        elif fuse_active and drawdown >= -float(fuse["recovery"]):
            fuse_active = False

        target, sleeve_weights, target_meta = _target_for_close(date, navs, states, config, fuse_active)
        target_cash = max(0.0, 1.0 - sum(target.values()))
        distance = _direct_weight_distance(current, target)
        days_since = index + 1 if last_rebalance_date is None else index - next(
            i for i, item in enumerate(dates) if item == last_rebalance_date
        )
        should_rebalance = (
            target_meta["status"] != "warming_up" and
            pending is None and
            (not current or distance >= float(allocation["rebalance_band"]) or
             days_since >= int(allocation["rebalance_max_days"]))
        )
        if should_rebalance:
            pending = target
            pending_meta = {"generated_on": date, "target": target, "sleeve_weights": sleeve_weights,
                            "target_meta": target_meta}
        history.append({
            "date": date,
            "nav": round(nav, 2), "daily_return": round(strategy_return, 10),
            "drawdown": round(drawdown, 10), "weights_effective": current,
            "sleeve_weights_effective": current_sleeves,
            "cash_weight_effective": round(max(0.0, 1.0 - sum(current.values())), 8),
            "trade_executed": bool(executed_from), "trade_generated_on": executed_from,
            "turnover": round(executed_turnover, 10), "cost": round(executed_cost, 6),
            "target_generated": bool(should_rebalance), "target_weights": target if should_rebalance else None,
            "target_cash_weight": round(target_cash, 8) if should_rebalance else None,
            "target_sleeve_weights": sleeve_weights if should_rebalance else None,
            "risk": target_meta, "fuse_active": fuse_active,
        })
    latest = history[-1]
    generated_at = datetime.now(SHANGHAI_TZ).isoformat()
    config_sha = hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
    payload = {
        "schema_version": 1, "mode": "meta_shadow_v1", "state": "shadow_only",
        "generated_at": generated_at, "source_as_of": dates[-1], "initial_capital": 100000.0,
        "config_sha256": config_sha, "input": {
            "database": "data/quant.db", "tables": ["qsim_snapshots"],
            "read_only": True, "strategy_states": states,
        },
        "guardrails": {
            "does_not_write_qsim_ledgers": True,
            "next_session_effective": True,
            "excluded_candidates": ["multi_factor_v3"],
            "minimum_history_days": config["min_history_days"],
        },
        "latest": latest, "history": history,
    }
    _atomic_json(ARTIFACT_DIR / f"{dates[-1]}.json", payload)
    _atomic_json(ARTIFACT_DIR / "latest.json", payload)
    return payload


if __name__ == "__main__":
    result = run_shadow()
    latest = result["latest"]
    print(json.dumps({
        "ok": True, "artifact": str(ARTIFACT_DIR / "latest.json"),
        "source_as_of": result["source_as_of"], "nav": latest["nav"],
        "status": latest["risk"]["status"], "weights": latest["weights_effective"],
    }, ensure_ascii=False))
