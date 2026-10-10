"""qsim 展示网页：聚合数据端点（只读）。

从 quant.db / artifacts/ 聚合以下数据供 dashboard 页面消费：
  - meta_nav          : a/b/c 三种模式的净值曲线
  - strategy_accounts : 各策略模拟账户摘要
  - snapshots         : 组合快照
  - artifacts/shadow  : 每日影子对账结果
  - artifacts/experiments.jsonl : 回测实验指标
  - system status     : 当前 mode / risk / scheduler jobs

所有读取均为只读（mode=ro），不修改任何生产数据。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
QUANT_DB = ROOT / "data" / "quant.db"
ARTIFACTS = ROOT / "artifacts"


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{QUANT_DB}?mode=ro", uri=True)


def _meta_nav() -> dict[str, list[list]]:
    """a/b/c 三模式净值曲线 [{mode: [[date, nav], ...]}]. """
    out: dict[str, list[list]] = {}
    with _conn() as c:
        for mode in ("a", "b", "c"):
            rows = c.execute(
                "SELECT date, nav FROM meta_nav WHERE mode=? ORDER BY date",
                (mode,),
            ).fetchall()
            out[mode] = [[d, round(float(n), 2)] for d, n in rows]
    return out


def _accounts() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, strategy_name, symbol, initial_capital, cash, shares "
            "FROM strategy_accounts ORDER BY id"
        ).fetchall()
    return [
        {
            "id": r[0],
            "strategy": r[1],
            "symbol": r[2],
            "initial": round(float(r[3]), 2),
            "cash": round(float(r[4]), 2),
            "shares": int(r[5] or 0),
        }
        for r in rows
    ]


def _snapshots(limit: int = 10) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT date, symbol, cash, position_value, total_value "
            "FROM snapshots ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "date": r[0],
            "symbol": r[1],
            "cash": round(float(r[2]), 2),
            "position": round(float(r[3]), 2),
            "total": round(float(r[4]), 2),
        }
        for r in rows
    ]


def _latest_shadow() -> dict | None:
    files = sorted(ARTIFACTS.glob("shadow/*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def _experiments() -> dict:
    """聚合 experiments.jsonl：最新 nested_final + 各 config 的扫描汇总。"""
    path = ARTIFACTS / "experiments.jsonl"
    if not path.exists():
        return {}
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    nested_final = [r for r in rows if r.get("kind") == "nested_final"]
    auto_summary = [r for r in rows if r.get("kind") == "auto_scan_summary"]
    return {
        "latest_nested": nested_final[-1] if nested_final else None,
        "scan_summaries": auto_summary,
        "total": len(rows),
    }


def collect() -> dict:
    """一次聚合 dashboard 需要的全部数据。"""
    shadow = _latest_shadow()
    return {
        "meta_nav": _meta_nav(),
        "accounts": _accounts(),
        "snapshots": _snapshots(),
        "shadow": shadow,
        "experiments": _experiments(),
    }


if __name__ == "__main__":
    data = collect()
    print(json.dumps(data, ensure_ascii=False, indent=1)[:2000])
