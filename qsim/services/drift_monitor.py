"""Live 漂移监控：滚动窗口 live Sharpe 跌破回测同窗口分布下界即告警。

回测分布来自冻结的十年 equity CSV（与回测页同一产物），live 净值来自
qsim_snapshots；窗口默认 60 个交易日。仅在漂移状态转换时通知（开始/恢复），
状态持久化到 artifacts/drift_state.json。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "quant.db"
CURVES_DIR = ROOT / "artifacts" / "experiments" / "strategy_curves_20160815_20260814"
DEFAULT_STATE = ROOT / "artifacts" / "drift_state.json"

WINDOW = 60
FLOOR_PCT = 0.05


def _rolling_sharpe(values: list[float], window: int) -> list[float]:
    out = []
    for i in range(window, len(values) + 1):
        seg = values[i - window:i]
        rets = [seg[j] / seg[j - 1] - 1 for j in range(1, len(seg)) if seg[j - 1] > 0]
        if len(rets) < window - 1:
            continue
        mu = statistics.fmean(rets)
        sd = statistics.pstdev(rets)
        if sd > 0:
            out.append(mu / sd * math.sqrt(252))
    return out


def backtest_sharpe_floor(key: str, window: int = WINDOW, pct: float = FLOOR_PCT,
                          curves_dir: Path | None = None) -> float | None:
    """回测冻结曲线的滚动窗口 Sharpe 分布的 pct 分位下界。"""
    path = (curves_dir or CURVES_DIR) / f"{key}_equity.csv"
    if not path.is_file():
        return None
    with path.open(newline="", encoding="utf-8") as fh:
        values = [
            float(r["total_value"]) for r in csv.DictReader(fh)
            if float(r["total_value"]) > 0
        ]
    sharpes = sorted(_rolling_sharpe(values, window))
    if not sharpes:
        return None
    idx = min(int(len(sharpes) * pct), len(sharpes) - 1)
    return sharpes[idx]


def live_sharpe(conn: sqlite3.Connection, strategy: str,
                window: int = WINDOW) -> tuple[float | None, int]:
    """最近 window+1 个快照的 live Sharpe；样本不足返回 (None, 天数)。"""
    rows = conn.execute(
        "SELECT total_value FROM qsim_snapshots WHERE strategy=? ORDER BY date DESC LIMIT ?",
        (strategy, window + 1),
    ).fetchall()
    if len(rows) < window + 1:
        return None, len(rows)
    values = [float(r[0]) for r in reversed(rows)]
    sharpes = _rolling_sharpe(values, window)
    return (sharpes[0] if sharpes else None), len(rows)


def _monitored_strategies() -> list[str]:
    from qsim.storage.paper import PAPER_VERSIONED_CANDIDATES
    from qsim.strategies.migration import ACTIVE_MIGRATIONS, MigrationState, migration_for

    names = [
        item.name for item in ACTIVE_MIGRATIONS
        if migration_for(item.name).state is not MigrationState.RETIRED
    ]
    names.extend(PAPER_VERSIONED_CANDIDATES)
    return sorted(names)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"strategies": {}}


def evaluate(db_path: Path = DEFAULT_DB, state_path: Path = DEFAULT_STATE,
             notifier=None) -> dict:
    from qsim.services import notify

    send = notifier or notify.notify
    state = _load_state(state_path)
    entries = state.setdefault("strategies", {})
    events: list[dict] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for name in _monitored_strategies():
            floor = backtest_sharpe_floor(name)
            live, days = live_sharpe(conn, name)
            entry = entries.setdefault(name, {})
            if floor is None:
                entry.update(status="no_backtest", drifting=False)
                continue
            if live is None:
                entry.update(status=f"insufficient_data({days}/{WINDOW + 1})", drifting=False)
                continue
            drifting = live < floor
            was = bool(entry.get("drifting"))
            entry.update(status="ok", drifting=drifting,
                         live_sharpe=round(live, 3), floor=round(floor, 3))
            if drifting and not was:
                entry["since"] = dt.date.today().isoformat()
                send(
                    f"[qsim] 策略漂移告警: {name}",
                    f"近 {WINDOW} 日 live Sharpe {live:.2f} 跌破回测同窗口 "
                    f"{FLOOR_PCT:.0%} 分位下界 {floor:.2f}，建议复核是否衰减。",
                )
                events.append({"strategy": name, "event": "drift_start",
                               "live": live, "floor": floor})
            elif not drifting and was:
                send(
                    f"[qsim] 策略漂移恢复: {name}",
                    f"近 {WINDOW} 日 live Sharpe {live:.2f} 回到下界 {floor:.2f} 之上。",
                )
                events.append({"strategy": name, "event": "drift_recover",
                               "live": live, "floor": floor})
    finally:
        conn.close()
    state["checked_at"] = dt.datetime.now().isoformat(timespec="seconds")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"checked_at": state["checked_at"], "events": events, "strategies": entries}
