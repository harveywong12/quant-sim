"""Durable close-signal -> next-open paper orders for multi_factor v3."""

from __future__ import annotations
from qsim.services.mutation_lock import serialized_mutation


import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Callable

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.cross_sectional_runner import (
    affordable_equal_weight_targets,
    monthly_rebalance_dates,
    target_shares_for_budget,
)
from qsim.engine.matcher import LOT_SIZE, buy_exact, sell_exact
from qsim.features.factor import compute_factor_ic_scores_asof
from qsim.services import realtime
from qsim.storage.paper import QUANT_DB, get_conn, init_schema
from qsim.strategies.market_gate import multi_factor_v3_allocations
from qsim.strategies.cross_sectional import is_star_board

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = "multi_factor_v3"
STRATEGY_SPECS = {
    "multi_factor_v3": {
        "config": "multi-factor-v3.json",
        "exclude_star": False,
        "version": "v3",
    },
    "multi_factor_v4": {
        "config": "multi-factor-v4.json",
        "exclude_star": True,
        "version": "v4",
        "paper_start_date": "20260902",
    },
}
DESIRED_COUNT = 10
CANDIDATE_MULTIPLIER = 5
COST = CostModel(round_fee=False, historical_rates=True)
SHANGHAI_TZ = realtime.SHANGHAI_TZ


def _now(value: dt.datetime | None = None) -> dt.datetime:
    value = value or dt.datetime.now(SHANGHAI_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def _spec(strategy: str) -> dict:
    try:
        return STRATEGY_SPECS[strategy]
    except KeyError as exc:
        raise ValueError(f"unsupported paper order strategy: {strategy}") from exc


def _model_hash(strategy: str = STRATEGY) -> str:
    """Hash every source file that can alter a frozen versioned order."""
    spec = _spec(strategy)
    files = [
        ROOT / "qsim" / "features" / "factor.py",
        ROOT / "qsim" / "strategies" / "market_gate.py",
        ROOT / "qsim" / "strategies" / "cross_sectional.py",
        ROOT / "qsim" / "engine" / "cross_sectional_runner.py",
        ROOT / "qsim" / "engine" / "costs.py",
        ROOT / "qsim" / "services" / "paper_orders.py",
        ROOT / "config" / spec["config"],
    ]
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _canonical_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _dates(conn: sqlite3.Connection, as_of: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT REPLACE(date,'-','') AS d FROM stock_bars "
            "WHERE REPLACE(date,'-','')<=? ORDER BY d",
            (as_of,),
        )
    ]


@serialized_mutation
def freeze_order_intent(
    as_of: str | None = None,
    *,
    strategy: str = STRATEGY,
    db_path: str | Path = QUANT_DB,
    now: dt.datetime | None = None,
    replace_pending: bool = False,
) -> dict:
    """Freeze a month-start versioned signal without overwriting evidence."""
    if os.environ.get("QSIM_FORMAL_PAPER", "0") == "1":
        return {"ok": False, "error": "formal paper freezes decisions atomically with end-of-day settlement"}
    if os.environ.get("QSIM_PAPER_AUTORUN", "1") == "0":
        return {"ok": False, "error": "paper execution has not passed admission; ledger preserved"}
    spec = _spec(strategy)
    conn = get_conn(db_path)
    try:
        init_schema(conn)
        latest = conn.execute(
            "SELECT MAX(REPLACE(date,'-','')) FROM stock_bars"
        ).fetchone()[0]
        signal_date = str(as_of or latest or "").replace("-", "")
        if len(signal_date) != 8:
            return {"ok": False, "error": "no market as-of date"}
        if signal_date < str(spec.get("paper_start_date") or ""):
            return {
                "ok": True,
                "skipped": "before-paper-start",
                "strategy": strategy,
                "signal_date": signal_date,
            }
        days = _dates(conn, signal_date)
        if not days or days[-1] != signal_date:
            return {"ok": False, "error": "signal date is not available", "signal_date": signal_date}
        rebalances = monthly_rebalance_dates(days)
        if signal_date not in rebalances:
            return {"ok": True, "skipped": "not-month-start", "signal_date": signal_date}

        score_map = compute_factor_ic_scores_asof(db_path, rebalances, as_of=signal_date)
        scores = score_map.get(signal_date)
        if scores is None or scores.empty:
            return {"ok": False, "error": "factor score unavailable", "signal_date": signal_date}
        ranked = sorted(
            scores.dropna().index.astype(str),
            key=lambda symbol: (-float(scores.loc[symbol]), symbol),
        )
        if spec["exclude_star"]:
            ranked = [symbol for symbol in ranked if not is_star_board(symbol)]
        candidates = [
            {"code": code, "score": float(scores.loc[code]), "rank": rank}
            for rank, code in enumerate(ranked[: DESIRED_COUNT * CANDIDATE_MULTIPLIER], 1)
        ]
        allocations, gate_records = multi_factor_v3_allocations(
            conn, days, [signal_date],
        )
        allocation = float(allocations.get(signal_date, 0.0))
        gate = gate_records[-1] if gate_records else {}
        execution_day = realtime.next_trading_day(
            dt.datetime.strptime(signal_date, "%Y%m%d").date()
        )
        if execution_day is None:
            return {"ok": False, "error": "next trading day unavailable"}
        model_hash = _model_hash(strategy)
        frozen = {
            "strategy": strategy,
            "signal_date": signal_date,
            "execution_date": execution_day.strftime("%Y%m%d"),
            "allocation": allocation,
            "desired_count": DESIRED_COUNT,
            "candidates": candidates,
            "gate": gate,
            "source_market_as_of": signal_date,
            "model_hash": model_hash,
        }
        signal_hash = _canonical_hash(frozen)
        created_at = _now(now).isoformat()
        inserted = conn.execute(
            "INSERT OR IGNORE INTO qsim_order_intents "
            "(strategy,signal_date,execution_date,status,allocation,desired_count,"
            "candidates_json,gate_json,source_market_as_of,model_hash,signal_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                strategy, signal_date, frozen["execution_date"], "pending",
                allocation, DESIRED_COUNT,
                json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
                json.dumps(gate, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                signal_date, model_hash, signal_hash, created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM qsim_order_intents WHERE strategy=? AND signal_date=?",
            (strategy, signal_date),
        ).fetchone()
        revised = False
        if row["signal_hash"] != signal_hash:
            if not replace_pending or row["status"] != "pending":
                conn.commit()
                return {
                    "ok": False,
                    "error": "frozen signal conflict; existing evidence preserved",
                    "signal_date": signal_date,
                    "existing_signal_hash": row["signal_hash"],
                    "computed_signal_hash": signal_hash,
                }
            # Only an explicitly requested, not-yet-executed refreeze may
            # replace a draft. Preserve the original row verbatim first.
            conn.execute(
                "INSERT INTO qsim_order_intent_revisions "
                "(intent_id,strategy,reason,prior_payload_json,revised_at) VALUES (?,?,?,?,?)",
                (
                    int(row["id"]), strategy,
                    "pre-execution refreeze requested",
                    json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    created_at,
                ),
            )
            conn.execute(
                "UPDATE qsim_order_intents SET execution_date=?,allocation=?,desired_count=?,"
                "candidates_json=?,gate_json=?,source_market_as_of=?,model_hash=?,signal_hash=?,"
                "created_at=?,last_error=NULL WHERE id=? AND status='pending'",
                (
                    frozen["execution_date"], allocation, DESIRED_COUNT,
                    json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(gate, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    signal_date, model_hash, signal_hash, created_at, int(row["id"]),
                ),
            )
            row = conn.execute(
                "SELECT * FROM qsim_order_intents WHERE id=?", (int(row["id"]),)
            ).fetchone()
            revised = True
        conn.commit()
        return {
            "ok": True,
            "created": bool(inserted.rowcount),
            "revised": revised,
            "id": int(row["id"]),
            "status": row["status"],
            "signal_date": signal_date,
            "execution_date": row["execution_date"],
            "allocation": float(row["allocation"]),
            "candidate_count": len(candidates),
            "signal_hash": signal_hash,
            "model_hash": model_hash,
        }
    finally:
        conn.close()


def freeze_v3_intent(as_of: str | None = None, **kwargs) -> dict:
    """Compatibility wrapper for the existing v3 API and tests."""
    return freeze_order_intent(as_of, strategy="multi_factor_v3", **kwargs)


def freeze_v4_intent(as_of: str | None = None, **kwargs) -> dict:
    """Freeze the account-constrained non-STAR v4 basket."""
    return freeze_order_intent(as_of, strategy="multi_factor_v4", **kwargs)


def _intent_error(db_path: str | Path, intent_id: int, message: str) -> dict:
    conn = get_conn(db_path)
    try:
        conn.execute(
            "UPDATE qsim_order_intents SET last_error=? WHERE id=? AND status='pending'",
            (message[:500], intent_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": False, "status": "pending", "intent_id": intent_id, "error": message}


def _quote_value(quotes: dict[str, dict], code: str, field: str) -> float | None:
    quote = quotes.get(str(code)[:6])
    if not quote:
        return None
    try:
        value = float(quote.get(field) or 0.0)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _quote_is_current(quotes: dict[str, dict], code: str, execution_date: str) -> bool:
    quote = quotes.get(str(code)[:6]) or {}
    quote_date = str(quote.get("quote_date") or "").replace("-", "")
    return quote_date == execution_date


@serialized_mutation
def execute_order_open(
    *,
    strategy: str = STRATEGY,
    now: dt.datetime | None = None,
    db_path: str | Path = QUANT_DB,
    quote_loader: Callable[[list[str]], dict[str, dict]] | None = None,
) -> dict:
    """Execute one frozen versioned intent during 09:30-09:35, exactly once."""
    if os.environ.get("QSIM_FORMAL_PAPER", "0") == "1":
        return {"ok": False, "error": "formal paper uses immutable close signals and official daily prices; intraday execution disabled"}
    if os.environ.get("QSIM_PAPER_AUTORUN", "1") == "0":
        return {"ok": False, "error": "paper execution has not passed admission; ledger preserved"}
    spec = _spec(strategy)
    timestamp = _now(now)
    date = timestamp.strftime("%Y%m%d")
    minute = timestamp.hour * 60 + timestamp.minute
    if not realtime._is_trading_day(timestamp.date()):
        return {"ok": True, "skipped": "not-trading-day", "date": date}
    if minute < 9 * 60 + 30 or minute > 9 * 60 + 35:
        return {"ok": True, "skipped": "outside-open-window", "date": date}

    conn = get_conn(db_path)
    try:
        init_schema(conn)
        intent = conn.execute(
            "SELECT * FROM qsim_order_intents WHERE strategy=? AND execution_date=? "
            "AND status='pending' ORDER BY id LIMIT 1",
            (strategy, date),
        ).fetchone()
        if not intent:
            return {"ok": True, "skipped": "no-pending-intent", "date": date}
        intent = dict(intent)
        account = conn.execute(
            "SELECT * FROM qsim_accounts WHERE strategy=?", (strategy,)
        ).fetchone()
        if not account:
            return _intent_error(db_path, int(intent["id"]), f"{strategy} account unavailable")
        positions = conn.execute(
            "SELECT ts_code,shares,avg_cost FROM qsim_positions WHERE account_id=?",
            (int(account["id"]),),
        ).fetchall()
    finally:
        conn.close()

    if intent["model_hash"] != _model_hash(strategy):
        return _intent_error(
            db_path, int(intent["id"]), "model changed after signal freeze; execution blocked"
        )
    candidate_rows = json.loads(intent["candidates_json"])
    candidates = [str(item["code"]) for item in candidate_rows]
    held = {str(row["ts_code"]): (int(row["shares"]), float(row["avg_cost"])) for row in positions}
    quote_codes = list(dict.fromkeys([*candidates, *held]))
    quotes = (quote_loader or realtime.fetch_realtime_quotes)(quote_codes)
    missing_held = [
        code for code in held
        if not _quote_is_current(quotes, code, date)
        or _quote_value(quotes, code, "open_price") is None
    ]
    if missing_held:
        return _intent_error(
            db_path, int(intent["id"]), f"missing held-symbol opening quotes: {missing_held}"
        )

    executable_prices: dict[str, float] = {}
    blocked: dict[str, str] = {}
    for code in candidates:
        if not _quote_is_current(quotes, code, date):
            continue
        opening = _quote_value(quotes, code, "open_price")
        previous = _quote_value(quotes, code, "pre_close")
        if opening is None:
            continue
        reason = COST.limit_blocked("BUY", opening, previous, code)
        if reason:
            blocked[code] = reason
            continue
        executable_prices[code] = opening

    cash = float(account["cash"])
    opening_value = cash + sum(
        shares * float(_quote_value(quotes, code, "open_price") or 0.0)
        for code, (shares, _avg) in held.items()
    )
    allocation = min(max(float(intent["allocation"]), 0.0), 1.0)
    selected = affordable_equal_weight_targets(
        candidates,
        pd.Series(executable_prices, dtype=float),
        opening_value * allocation,
        int(intent["desired_count"]),
        COST,
    ) if allocation > 0 else []
    if allocation > 0 and not selected:
        return _intent_error(db_path, int(intent["id"]), "no executable affordable candidates")

    target_value = opening_value * allocation / len(selected) if selected else 0.0
    target_shares = {
        code: target_shares_for_budget(target_value, executable_prices[code], code, COST)
        for code in selected
    }
    if any(value < LOT_SIZE for value in target_shares.values()):
        return _intent_error(db_path, int(intent["id"]), "selected basket contains a zero-lot target")

    # Preflight every required sale before mutating the account.
    for code, (shares, _avg) in held.items():
        target = target_shares.get(code, 0)
        if shares <= target:
            continue
        opening = float(_quote_value(quotes, code, "open_price") or 0.0)
        previous = _quote_value(quotes, code, "pre_close")
        reason = COST.limit_blocked("SELL", opening, previous, code)
        if reason:
            return _intent_error(db_path, int(intent["id"]), f"{code}: {reason}")

    working = {code: [shares, avg] for code, (shares, avg) in held.items()}
    trades: list[dict] = []
    # Sell removed and overweight positions first.
    for code in list(working):
        current, avg = working[code]
        target = target_shares.get(code, 0)
        difference = current - target
        if difference < LOT_SIZE:
            continue
        opening = float(_quote_value(quotes, code, "open_price") or 0.0)
        previous = _quote_value(quotes, code, "pre_close")
        cash, remaining, trade, reason = sell_exact(
            cash, current, opening, date, COST, code, difference,
            prev_close=previous, avg_cost=avg,
        )
        if reason or trade is None:
            return _intent_error(db_path, int(intent["id"]), f"{code}: {reason or 'sell failed'}")
        trades.append(trade.to_dict())
        if remaining:
            working[code][0] = remaining
        else:
            working.pop(code, None)

    # Buy underweight targets using the frozen ranking and opening benchmark.
    for code in selected:
        current, avg = working.get(code, [0, 0.0])
        difference = target_shares[code] - int(current)
        if difference < LOT_SIZE:
            continue
        opening = executable_prices[code]
        previous = _quote_value(quotes, code, "pre_close")
        cash, updated, trade, reason = buy_exact(
            cash, int(current), opening, date, COST, code, difference,
            prev_close=previous,
        )
        if reason or trade is None:
            return _intent_error(db_path, int(intent["id"]), f"{code}: {reason or 'buy failed'}")
        new_avg = (
            (float(avg) * int(current) + float(trade.amount) + float(trade.fee)) / updated
            if updated else 0.0
        )
        working[code] = [updated, new_avg]
        trades.append(trade.to_dict())

    executed_at = timestamp.isoformat()
    version = f"{spec['version']}-order:{intent['model_hash'][:16]}"
    execution = {
        "intent_id": int(intent["id"]),
        "strategy": strategy,
        "signal_date": intent["signal_date"],
        "execution_date": date,
        "benchmark": "Sina official open",
        "slippage": COST.slippage,
        "allocation": allocation,
        "opening_value": opening_value,
        "selected": selected,
        "target_shares": target_shares,
        "blocked_candidates": blocked,
        "fills": trades,
        "executed_at": executed_at,
    }

    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        still_pending = conn.execute(
            "SELECT status FROM qsim_order_intents WHERE id=?", (int(intent["id"]),)
        ).fetchone()
        if not still_pending or still_pending["status"] != "pending":
            conn.rollback()
            return {"ok": True, "skipped": "already-executed", "intent_id": int(intent["id"])}
        for trade in trades:
            conn.execute(
                "INSERT INTO qsim_trades "
                "(date,strategy,symbol,side,price,shares,amount,fee,reason,code_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    date, strategy, trade["symbol"], trade["side"], trade["price"],
                    trade["shares"], trade["amount"], trade["fee"],
                    f"{strategy}:frozen_open", version,
                ),
            )
        account_id = int(account["id"])
        conn.execute("DELETE FROM qsim_positions WHERE account_id=?", (account_id,))
        for code, (shares, avg) in working.items():
            if shares <= 0:
                continue
            conn.execute(
                "INSERT INTO qsim_positions "
                "(account_id,ts_code,shares,avg_cost,updated_date) VALUES (?,?,?,?,?)",
                (account_id, code, int(shares), float(avg), date),
            )
        conn.execute(
            "UPDATE qsim_accounts SET cash=?,shares=?,avg_cost=0,symbol=NULL,code_hash=? "
            "WHERE id=?",
            (round(cash, 2), sum(int(value[0]) for value in working.values()), version, account_id),
        )
        position_value = sum(
            int(shares) * float(_quote_value(quotes, code, "open_price") or 0.0)
            for code, (shares, _avg) in working.items()
        )
        # qsim_snapshots contains settled closing NAV only. The opening
        # valuation/fills remain in execution_json; EOD is still unsettled.
        conn.execute(
            "UPDATE qsim_order_intents SET status='executed',executed_at=?,"
            "execution_json=?,last_error=NULL WHERE id=?",
            (
                executed_at,
                json.dumps(execution, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                int(intent["id"]),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "status": "executed",
        "intent_id": int(intent["id"]),
        "signal_date": intent["signal_date"],
        "execution_date": date,
        "selected": selected,
        "trade_count": len(trades),
        "cash": round(cash, 2),
    }


def execute_v3_open(**kwargs) -> dict:
    """Compatibility wrapper for the existing v3 scheduler and API."""
    return execute_order_open(strategy="multi_factor_v3", **kwargs)


def execute_v4_open(**kwargs) -> dict:
    """Execute the frozen non-STAR v4 order, if one is due."""
    return execute_order_open(strategy="multi_factor_v4", **kwargs)


def list_order_intents(
    *, strategy: str = STRATEGY, db_path: str | Path = QUANT_DB, limit: int = 20,
) -> list[dict]:
    _spec(strategy)
    conn = get_conn(db_path)
    try:
        init_schema(conn)
        rows = conn.execute(
            "SELECT * FROM qsim_order_intents WHERE strategy=? ORDER BY signal_date DESC LIMIT ?",
            (strategy, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_v3_intents(**kwargs) -> list[dict]:
    return list_order_intents(strategy="multi_factor_v3", **kwargs)


def list_v4_intents(**kwargs) -> list[dict]:
    return list_order_intents(strategy="multi_factor_v4", **kwargs)
