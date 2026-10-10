"""qsim 自己的数据聚合：只读 artifacts/ + API 状态。

qsim 引擎不写数据库——它的产出是：
  - artifacts/shadow/*.json       每日影子对账（qsim vs 旧系统）
  - artifacts/experiments.jsonl   回测实验（nested OOS/敏感性/扫描）
  - API 状态（mode/risk/scheduler）
本模块只聚合这些 qsim 自有数据，绝不读取旧系统的账户/交易/行情表。
"""

from __future__ import annotations

import bisect
import csv
import datetime as dt
import json
from collections import defaultdict, deque
from pathlib import Path
from zoneinfo import ZoneInfo
from qsim.services.web.return_status import account_return_state, date_key

ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = ROOT / "artifacts"
SHADOW_DIR = ARTIFACTS / "shadow"
EXPERIMENTS = ARTIFACTS / "experiments.jsonl"
ADAPTIVE_REPLAY = ARTIFACTS / "adaptive_replay" / "latest.json"
REBASELINE_DIR = ARTIFACTS / "rebaseline"
PROVISIONAL_PAPER = ARTIFACTS / "provisional_paper" / "latest.json"
META_SHADOW = ARTIFACTS / "meta_shadow_v1" / "latest.json"
BACKTEST_DIR = ARTIFACTS / "experiments" / "market_gate_ten_year_20260831"
STRATEGY_BACKTEST_DIR = ARTIFACTS / "experiments" / "strategy_curves_20160815_20260814"

BACKTEST_DEFINITIONS = (
    ("baseline_full", "基线 · 满仓", "baseline_full_equity.csv"),
    ("trend_skip", "趋势门禁", "trend_skip_equity.csv"),
    ("trend_breadth_skip", "趋势 + 宽度", "trend_breadth_skip_equity.csv"),
    ("trend_breadth_vol", "趋势 + 宽度 + 波动率缩放", "trend_breadth_vol_equity.csv"),
)


# ---------- 影子对账 ----------

def latest_shadow() -> dict | None:
    files = sorted(SHADOW_DIR.glob("*.json")) if SHADOW_DIR.exists() else []
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def shadow_history(limit: int = 30) -> list[dict]:
    """最近 N 天的对账历史（all_ok 序列）。"""
    files = sorted(SHADOW_DIR.glob("*.json")) if SHADOW_DIR.exists() else []
    out = []
    for f in files[-limit:]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"date": d.get("date"), "all_ok": d.get("all_ok"),
                        "generated_at": d.get("generated_at")})
        except Exception:
            continue
    return out


def adaptive_replay() -> dict | None:
    """Latest isolated capital-adaptive replay; never a qsim_* ledger read."""
    if not ADAPTIVE_REPLAY.exists():
        return None
    try:
        payload = json.loads(ADAPTIVE_REPLAY.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if payload.get("mode") != "capital_adaptive_replay":
        return None
    return payload


def latest_rebaseline() -> dict | None:
    """Most recent operator-authorized paper baseline replacement record."""
    files = sorted(REBASELINE_DIR.glob("*.json")) if REBASELINE_DIR.exists() else []
    if not files:
        return None
    try:
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if payload.get("kind") == "operator_capital_adaptive_rebaseline" else None


def provisional_paper() -> dict | None:
    """Latest daily ledger for strategies still pending migration verification."""
    if not PROVISIONAL_PAPER.exists():
        return None
    try:
        payload = json.loads(PROVISIONAL_PAPER.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if payload.get("mode") == "provisional_paper" else None


def meta_shadow() -> dict | None:
    """Latest read-only meta-shadow artifact; never an account or execution source."""
    if not META_SHADOW.exists():
        return None
    try:
        payload = json.loads(META_SHADOW.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if payload.get("mode") != "meta_shadow_v1" or payload.get("state") != "shadow_only":
        return None
    return payload


def strategy_screening() -> dict | None:
    path = ARTIFACTS / "strategy_screening" / "latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("mode") == "chronological_screening" and payload.get("state") == "research_only":
        return payload
    return None


def trend_fallback_research() -> dict | None:
    path = ARTIFACTS / "trend_fallback_research" / "latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("mode") == "trend_fallback_research" and payload.get("state") == "research_only":
        return payload
    return None


def market_gate_curves() -> list[dict]:
    """Four frozen market-gate experiment curves, kept as secondary evidence.

    This is intentionally separate from qsim_* paper NAVs: a curve is shown
    only when its equity CSV and metrics JSON come from the same experiment.
    """
    metrics_path = BACKTEST_DIR / "metrics.json"
    if not metrics_path.exists():
        return []
    try:
        metrics_payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        metric_rows = metrics_payload.get("metrics", {})
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    out = []
    for key, display_name, filename in BACKTEST_DEFINITIONS:
        equity_path = BACKTEST_DIR / filename
        if not equity_path.exists() or key not in metric_rows:
            continue
        data = []
        try:
            with equity_path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    value = float(row.get("total_value") or 0)
                    if value > 0:
                        data.append({"date": str(row.get("date") or ""), "total_value": value})
        except (OSError, ValueError, csv.Error):
            continue
        full = metric_rows[key].get("full") or {}
        oos = metric_rows[key].get("oos") or {}
        if not data:
            continue
        out.append({
            "key": key, "name": display_name,
            "run": "market_gate_ten_year_20260831",
            "data": data,
            "metrics": {
                "initial": full.get("initial"), "final_value": full.get("final_value"),
                "total_return": full.get("total_return", 0),
                "annual_return": full.get("annual_return", 0),
                "max_drawdown": full.get("max_drawdown", 0),
                "sharpe": full.get("sharpe", 0),
                "turnover": full.get("turnover", 0),
                "cost_drag": full.get("cost_drag", 0),
                "num_trades": full.get("num_trades", 0),
                "days": full.get("days", len(data)),
                "mean_invested": full.get("mean_invested"),
                "window_start": full.get("window_start"),
                "window_end": full.get("window_end"),
                "oos_return": oos.get("total_return"),
                "oos_sharpe": oos.get("sharpe"),
            },
            "contract": metrics_payload.get("run_contract") or {},
        })
    return out


def strategy_backtest_curves() -> list[dict]:
    """Nine strategy curves: eight migrated strategies plus multi_factor v3."""
    summary_path = STRATEGY_BACKTEST_DIR / "summary.json"
    if not summary_path.exists():
        return []
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    out = []
    for item in payload.get("strategies") or []:
        key = str(item.get("key") or "")
        equity_path = STRATEGY_BACKTEST_DIR / f"{key}_equity.csv"
        if not key or not equity_path.exists():
            continue
        data = []
        try:
            with equity_path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    value = float(row.get("total_value") or 0)
                    if value > 0:
                        data.append({
                            "date": str(row.get("date") or ""),
                            "total_value": value,
                        })
        except (OSError, ValueError, csv.Error):
            continue
        if not data:
            continue
        out.append({
            "key": key, "name": item.get("name") or key,
            "run": payload.get("manifest", {}).get("run_id"),
            "data": data, "metrics": item.get("metrics") or {},
            "sample": item.get("sample") or {},
            "contract": payload.get("manifest") or {},
        })
    return out


def backtest_curves() -> list[dict]:
    """Primary backtest-tab data: one curve for each displayed strategy."""
    return strategy_backtest_curves()


# ---------- 回测实验 ----------

def _load_experiments() -> list[dict]:
    if not EXPERIMENTS.exists():
        return []
    rows = []
    for line in EXPERIMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def experiments() -> dict:
    rows = _load_experiments()
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r.get("kind", "?"), []).append(r)
    return {
        "total": len(rows),
        "kinds": {k: len(v) for k, v in by_kind.items()},
        "nested_final": by_kind.get("nested_final", []),
        "nested_train": by_kind.get("nested_train", []),
        "nested_validation": by_kind.get("nested_validation", []),
        "nested_sensitivity": by_kind.get("nested_sensitivity", []),
        "auto_scan": by_kind.get("auto_scan", []),
        "auto_scan_summary": by_kind.get("auto_scan_summary", []),
        "cost_scan_summary": by_kind.get("cost_scan_summary", []),
        "trend_cross_scan": by_kind.get("trend_cross_scan", []),
        "aggressive_scan": by_kind.get("aggressive_scan", []),
    }


def configs_summary() -> list[dict]:
    """各配置的最新 nested_final 汇总（对比用）。"""
    exp = experiments()
    by_config: dict[str, dict] = {}
    for r in exp["nested_final"]:
        cfg = r.get("config")
        if not cfg:
            continue
        # 同配置取最新
        if cfg not in by_config or r.get("ts", "") > by_config[cfg].get("ts", ""):
            by_config[cfg] = r
    out = []
    for cfg, r in sorted(by_config.items()):
        out.append({
            "config": cfg,
            "total_return": r.get("total_return", 0),
            "max_drawdown": r.get("max_drawdown", 0),
            "sharpe": r.get("sharpe", 0),
            "num_days": r.get("num_days", 0),
        })
    return out


def oos_years_summary() -> list[dict]:
    """auto_scan 按 config 分组，展示各 OOS 年份表现。"""
    exp = experiments()
    by_cfg: dict[str, list[dict]] = {}
    for r in exp["auto_scan"]:
        by_cfg.setdefault(r.get("config", "?"), []).append(r)
    out = []
    for cfg, items in sorted(by_cfg.items()):
        for r in sorted(items, key=lambda x: x.get("oos_year", 0)):
            out.append({
                "config": cfg,
                "year": r.get("oos_year"),
                "return": r.get("return", 0),
                "sharpe": r.get("sharpe", 0),
                "max_drawdown": r.get("max_drawdown", 0),
                "trades": r.get("trades", 0),
            })
    return out


def collect() -> dict:
    """qsim 自有数据聚合（不含任何旧系统表）。"""
    return {
        "shadow": latest_shadow(),
        "shadow_history": shadow_history(),
        "experiments": experiments(),
        "configs": configs_summary(),
        "oos_years": oos_years_summary(),
        "adaptive_replay": adaptive_replay(),
        "rebaseline": latest_rebaseline(),
        "provisional_paper": provisional_paper(),
        "meta_shadow": meta_shadow(),
        "backtest_curves": backtest_curves(),
        "market_gate_curves": market_gate_curves(),
    }


if __name__ == "__main__":
    d = collect()
    print("shadow:", bool(d["shadow"]), "| shadow_days:", len(d["shadow_history"]))
    print("experiments:", d["experiments"]["total"], "| kinds:", d["experiments"]["kinds"])
    print("configs:", len(d["configs"]))
    print("oos_years:", len(d["oos_years"]))


# ---------- 模拟盘数据（qsim_* 表，只读） ----------

def ai_report() -> dict | None:
    """Latest AI daily review row from the shared ai_reports table."""
    conn = _paper_conn()
    try:
        row = conn.execute(
            "SELECT date, report_json, model, created_at FROM ai_reports"
            " ORDER BY date DESC, created_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        import json as _json
        report = _json.loads(row["report_json"])
        if isinstance(report.get("summary"), str):
            nested = report["summary"].strip()
            if nested.startswith("{"):
                try:
                    inner = _json.loads(nested)
                except _json.JSONDecodeError:
                    inner = None
                if isinstance(inner, dict):
                    for key in ("summary", "issues", "suggestions", "hypotheses"):
                        if not report.get(key) and inner.get(key):
                            report[key] = inner[key]
        return {"date": str(row["date"]), "model": row["model"],
                "created_at": row["created_at"], "report": report}
    finally:
        conn.close()


def _paper_conn():
    import sqlite3
    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'quant.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def paper_accounts() -> list[dict]:
    """8 策略账户状态（含持仓市值估值）。"""
    with _paper_conn() as c:
        rows = c.execute(
            "SELECT a.strategy, a.symbol, a.initial_capital, a.cash, a.shares, "
            "a.avg_cost, a.status "
            "FROM qsim_accounts a ORDER BY a.id"
        ).fetchall()
    return [dict(r) for r in rows]


def paper_positions() -> list[dict]:
    """每股持仓（策略/标的/股数/成本），与 snapshots 最新估值合并。"""
    with _paper_conn() as c:
        rows = c.execute(
            "SELECT a.strategy, p.ts_code, p.shares, p.avg_cost, p.updated_date "
            "FROM qsim_positions p JOIN qsim_accounts a ON a.id=p.account_id "
            "ORDER BY a.strategy, p.ts_code"
        ).fetchall()
    return [dict(r) for r in rows]


def paper_snapshots(strategy: str | None = None) -> list[dict]:
    """每日净值序列（画曲线用）。"""
    with _paper_conn() as c:
        if strategy:
            rows = c.execute(
                "SELECT date, total_value, cash, position_value FROM qsim_snapshots "
                "WHERE strategy=? ORDER BY date", (strategy,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT date, strategy, total_value, cash, position_value "
                "FROM qsim_snapshots ORDER BY date, strategy"
            ).fetchall()
    return [dict(r) for r in rows]


def paper_trades(strategy: str | None = None, limit: int = 50) -> list[dict]:
    """成交记录。"""
    with _paper_conn() as c:
        has_provenance = c.execute("SELECT 1 FROM sqlite_master WHERE name='qsim_trade_provenance'").fetchone()
        extra = (", p.run_id, json_extract(r.metadata_json, '$.code_config_hash') AS execution_version" if has_provenance else "")
        joins = (" LEFT JOIN qsim_trade_provenance p ON p.trade_id=t.id LEFT JOIN qsim_settlement_runs r ON r.run_id=p.run_id" if has_provenance else "")
        where = " WHERE t.strategy=?" if strategy else ""
        params = (strategy, limit) if strategy else (limit,)
        rows = c.execute("SELECT t.date,t.strategy,t.symbol,t.side,t.price,t.shares,t.amount,t.fee,t.reason,t.code_hash" + extra +
                         " FROM qsim_trades t" + joins + where + " ORDER BY t.date DESC,t.id DESC LIMIT ?", params).fetchall()
    return [dict(r) for r in rows]


def paper_latest_nav() -> dict[str, dict]:
    """各策略各自最新净值（总览速览）。

    策略可能因迁移门禁在不同日期结算，不能用全库的单一最大日期
    过滤，否则较早快照的策略会从总览资产统计中消失。
    """
    with _paper_conn() as c:
        rows = c.execute(
            "SELECT s.strategy, s.date, s.total_value, s.cash, s.position_value "
            "FROM qsim_snapshots s JOIN ("
            "  SELECT strategy, MAX(date) AS date FROM qsim_snapshots GROUP BY strategy"
            ") latest ON latest.strategy=s.strategy AND latest.date=s.date"
        ).fetchall()
    return {r["strategy"]: dict(r) for r in rows}


def latest_market_data_date() -> str | None:
    """Return the latest synced market date from the adapter manifest table."""
    import json

    try:
        with _paper_conn() as c:
            rows = c.execute(
                "SELECT dataset, source_table_watermarks "
                "FROM qsim_data_sync_runs WHERE status='success' "
                "ORDER BY id DESC"
            ).fetchall()
        wanted = {
            ("quant.db", "bars", "date"),
            ("quant.db", "stock_bars", "date"),
            ("v2_market.db", "stk_close_daily", "trade_date"),
            ("v2_market.db", "cb_daily_raw", "trade_date"),
        }
        dates: list[str] = []
        for row in rows:
            try:
                watermarks = json.loads(row[1] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for dataset, table, column in wanted:
                if row[0] == dataset:
                    value = watermarks.get(table, {}).get(column)
                    if value:
                        dates.append(str(value).replace("-", ""))
        return max(dates) if dates else None
    except Exception:
        return None


def hot_zone_snapshot() -> dict[str, str]:
    """热区策略内容哈希（hotzone.snapshot）。"""
    try:
        from qsim.strategies import hotzone
        return hotzone.snapshot()
    except Exception:
        return {}


def data_version(now: dt.datetime | None = None) -> str:
    """轻量版本号：模拟盘/热区/实验/对账任一变化 → 页面自动 reload。

    含：qsim_* 表最大时间戳+行数、热区策略哈希、experiments/shadow 文件。
    """
    import json as _json
    import sqlite3 as _sq
    parts = []
    now = now or dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai")) if now.tzinfo is None else now.astimezone(ZoneInfo("Asia/Shanghai"))
    # Date/session/expiry changes must refresh an open tab even when no new
    # market row arrives. These are state transitions, not periodic page reloads.
    from qsim.services import realtime as _realtime
    parts.append(f"display:{now.date().isoformat()}:{_realtime.is_trading_session(now)}")
    # qsim_* 模拟盘表
    try:
        c = _sq.connect(f"file:{ROOT / 'data' / 'quant.db'}?mode=ro", uri=True)
        for table, col in (("qsim_snapshots", "date"), ("qsim_trades", "date"),
                           ("qsim_accounts", "created_date"),
                           ("qsim_realtime", "updated_at")):
            row = c.execute(f"SELECT MAX({col}), COUNT(*) FROM {table}").fetchone()
            parts.append(f"{table}:{row[0]}:{row[1]}")
            if table == "qsim_realtime":
                try:
                    stamp = dt.datetime.strptime(str(row[0]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                    parts.append(f"quote_fresh:{stamp.date()==now.date() and 0 <= (now-stamp).total_seconds() <= 600}")
                except ValueError:
                    parts.append("quote_fresh:False")
        # 行情同步可能只更新 qsim_data_sync_runs，而 paper gate 暂时不产出新快照；
        # 将同步运行号纳入版本号，页面才能在行情时点推进后自动重载。
        row = c.execute(
            "SELECT MAX(id), COUNT(*) FROM qsim_data_sync_runs"
        ).fetchone()
        parts.append(f"market_sync:{row[0]}:{row[1]}")
        c.close()
    except Exception:
        pass
    for name in ("data_reconcile_state.json", "native_market_state.json", "settlement_state.json"):
        try:
            info = (ARTIFACTS / name).stat()
            parts.append(f"state:{name}:{info.st_mtime_ns}:{info.st_size}")
        except OSError:
            pass
    # 热区策略哈希（热插拔响应）
    try:
        snap = hot_zone_snapshot()
        if snap:
            parts.append("hot:" + _json.dumps(snap, sort_keys=True)[:200])
        else:
            parts.append("hot:builtin")
    except Exception:
        parts.append("hot:?")
    # 实验/对账文件
    try:
        if EXPERIMENTS.exists():
            st = EXPERIMENTS.stat()
            parts.append(f"exp:{st.st_mtime_ns}:{st.st_size}")
        sh_files = sorted(SHADOW_DIR.glob("*.json")) if SHADOW_DIR.exists() else []
        if sh_files:
            st = sh_files[-1].stat()
            parts.append(f"shadow:{st.st_mtime_ns}")
        if ADAPTIVE_REPLAY.exists():
            st = ADAPTIVE_REPLAY.stat()
            parts.append(f"adaptive_replay:{st.st_mtime_ns}:{st.st_size}")
        if PROVISIONAL_PAPER.exists():
            st = PROVISIONAL_PAPER.stat()
            parts.append(f"provisional:{st.st_mtime_ns}:{st.st_size}")
        if META_SHADOW.exists():
            st = META_SHADOW.stat()
            parts.append(f"meta_shadow:{st.st_mtime_ns}:{st.st_size}")
        if BACKTEST_DIR.exists():
            for path in (BACKTEST_DIR / "metrics.json", *(BACKTEST_DIR / name for _, _, name in BACKTEST_DEFINITIONS)):
                if path.exists():
                    st = path.stat()
                    parts.append(f"backtest:{path.name}:{st.st_mtime_ns}:{st.st_size}")
        if STRATEGY_BACKTEST_DIR.exists():
            for path in STRATEGY_BACKTEST_DIR.iterdir():
                if path.is_file():
                    st = path.stat()
                    parts.append(f"strategy_backtest:{path.name}:{st.st_mtime_ns}:{st.st_size}")
        rebase_files = sorted(REBASELINE_DIR.glob("*.json")) if REBASELINE_DIR.exists() else []
        if rebase_files:
            st = rebase_files[-1].stat()
            parts.append(f"rebaseline:{st.st_mtime_ns}:{st.st_size}")
    except Exception:
        pass
    return "|".join(parts)


def paper_positions_detail() -> list[dict]:
    """每股持仓明细（qsim_positions 权威数据源，含成本）。"""
    with _paper_conn() as c:
        rows = c.execute(
            "SELECT a.strategy, p.ts_code, p.shares, p.avg_cost, p.updated_date "
            "FROM qsim_positions p JOIN qsim_accounts a ON a.id=p.account_id "
            "WHERE p.shares>0 ORDER BY a.strategy, p.ts_code"
        ).fetchall()
    return [dict(r) for r in rows]


def paper_realtime_prices() -> dict[str, dict]:
    """模拟盘持仓实时价（qsim_realtime 表）。"""
    try:
        import sqlite3 as _sq
        c = _sq.connect(f"file:{ROOT / 'data' / 'quant.db'}?mode=ro", uri=True)
        columns = {row[1] for row in c.execute("PRAGMA table_info(qsim_realtime)")}
        quote_date = "quote_date" if "quote_date" in columns else "NULL"
        rows = c.execute(f"SELECT symbol, price, pre_close, name, updated_at, {quote_date} FROM qsim_realtime").fetchall()
        c.close()
        return {str(r[0]): {"price": r[1], "pre_close": r[2], "name": r[3], "updated_at": r[4], "quote_date": r[5]} for r in rows}
    except Exception:
        return {}


def paper_realtime_status(
    max_age_minutes: int = 10,
    now: dt.datetime | None = None,
) -> dict:
    """Quote-cache freshness for the web/API; stale marks are never "live"."""
    from qsim.services import realtime as _realtime

    now = now or dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    else:
        now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    prices = paper_realtime_prices()
    updated_at = max(
        (str(item.get("updated_at") or "") for item in prices.values()),
        default="",
    )
    updated = None
    if updated_at:
        try:
            updated = dt.datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            )
        except ValueError:
            pass
    age_seconds = max(0.0, (now - updated).total_seconds()) if updated else None
    in_session = _realtime.is_trading_session(now)
    fresh = bool(
        in_session and updated is not None and age_seconds is not None
        and updated.date() == now.date() and updated <= now
        and age_seconds <= max_age_minutes * 60
    )
    # The market is intentionally marked closed during lunch/after close, but
    # the latest quote from today's session remains valid for presentation.
    today_data = bool(updated is not None and updated.date() == now.date() and updated <= now)
    state = "fresh" if fresh else (
        "stale" if in_session and updated is not None else
        "unavailable" if not updated else "closed"
    )
    return {
        "state": state,
        "fresh": fresh,
        "today_data": today_data,
        "in_session": in_session,
        "quote_count": len(prices),
        "updated_at": updated_at or None,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "max_age_seconds": max_age_minutes * 60,
    }


def paper_realtime_nav_history(strategy: str, date: str | None = None) -> list[dict]:
    """New-system intraday NAV series; never read legacy ``realtime_nav``."""
    try:
        with _paper_conn() as c:
            if date:
                rows = c.execute(
                    "SELECT ts, total_value, cash, position_value, source "
                    "FROM qsim_realtime_nav WHERE strategy=? AND ts LIKE ? ORDER BY ts",
                    (strategy, f"{date}%"),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT ts, total_value, cash, position_value, source "
                    "FROM qsim_realtime_nav WHERE strategy=? ORDER BY ts",
                    (strategy,),
                ).fetchall()
        return [{
            "date": str(row["ts"]).replace("-", "").replace(" ", "").replace(":", ""),
            "total_value": float(row["total_value"]),
            "cash": float(row["cash"]),
            "position_value": float(row["position_value"]),
            "source": str(row["source"]),
            "intraday": True,
        } for row in rows]
    except Exception:
        return []


def paper_realtime_nav() -> dict[str, dict]:
    """各策略实时净值（现金 + 持仓×实时价，盘中随行情变化）。

    返回 {strategy: {total_value, cash, position_value}}。
    无实时价时回退最新快照。
    """
    accounts = {a["strategy"]: a for a in paper_accounts()}
    positions = paper_positions_detail()
    # A previous trading day's cache must not be presented as a live mark in
    # an open session.  Fall back to the settled snapshot until the next
    # successful five-minute refresh arrives.
    status = paper_realtime_status()
    rt = paper_realtime_prices() if (status["fresh"] or status.get("today_data")) else {}
    latest = paper_latest_nav()
    out: dict[str, dict] = {}
    marks = _mark_positions(positions, rt)
    for strat, acct in accounts.items():
        cash = float(acct.get("cash") or 0)
        strat_marks = [m for m in marks if m["strategy"] == strat]
        if strat_marks and all(m["price"] is not None for m in strat_marks):
            pos_value = sum(m["market_value"] for m in strat_marks)
        else:
            # 任何一只持仓没有可靠现价时，整体回退到最近结算快照，
            # 避免把缺价标的当成 0 元而夸大现金/净值。
            pos_value = float(latest.get(strat, {}).get("position_value") or 0)
        total = cash + pos_value
        out[strat] = {"total_value": round(total, 2), "cash": round(cash, 2),
                      "position_value": round(pos_value, 2)}
    return out


# ---------- 总览聚合（市场状态 + 现账户） ----------

def _mark_positions(positions: list[dict], rt: dict[str, dict] | None = None) -> list[dict]:
    """给 qsim 持仓打标：实时价优先，收盘行情兜底；缺价返回 None。"""
    import sqlite3

    rt = rt or {}
    out: list[dict] = []
    with _paper_conn() as c:
        for p in positions:
            code6 = str(p["ts_code"])[:6]
            quote = rt.get(code6) or rt.get(str(p["ts_code"]))
            price = quote.get("price") if quote else None
            price_date = str(quote.get("updated_at", ""))[:10].replace("-", "") if quote else ""
            price_source = "realtime" if quote and date_key(quote.get("quote_date")) == date_key(price_date) else "realtime_unverified"
            if not price:
                price_source = "close"
                row = c.execute(
                    "SELECT close, date FROM bars WHERE symbol=? ORDER BY date DESC LIMIT 1",
                    (code6,),
                ).fetchone()
                if not row:
                    full_code = str(p["ts_code"])
                    if "." in full_code:
                        row = c.execute(
                            "SELECT close,date FROM stock_bars WHERE ts_code=? ORDER BY date DESC LIMIT 1",
                            (full_code,),
                        ).fetchone()
                    else:
                        row = c.execute(
                            "SELECT close,date FROM stock_bars WHERE ts_code IN (?,?,?) ORDER BY date DESC LIMIT 1",
                            (f"{code6}.SH", f"{code6}.SZ", f"{code6}.BJ"),
                        ).fetchone()
                if row:
                    price, price_date = row[0], str(row[1])
            shares = int(p.get("shares") or 0)
            px = float(price) if price is not None else None
            out.append({
                **p,
                "price": px,
                "price_date": price_date or None,
                "price_source": price_source,
                "market_value": round(shares * px, 2) if px is not None else None,
            })
    return out


def _market_regime(c) -> tuple[str, str | None]:
    """复制旧系统 512100 MA20/MA60 + 5 日斜率的确定性部分。"""
    rows = c.execute(
        "SELECT date, close FROM bars WHERE symbol='512100' ORDER BY date"
    ).fetchall()
    closes = [float(r[1]) for r in rows if r[1] is not None]
    as_of = str(rows[-1][0]) if rows else None
    if len(closes) < 65:
        return "insufficient", as_of
    fast = sum(closes[-20:]) / 20
    slow = sum(closes[-60:]) / 60
    prior_fast = sum(closes[-25:-5]) / 20
    slope = fast / prior_fast - 1 if prior_fast else 0.0
    if fast > slow and slope > 0:
        return "bull", as_of
    if fast < slow and slope < 0:
        return "bear", as_of
    return "range", as_of


def _latest_breadth(c, source_date: str | None) -> tuple[float | None, str | None, int]:
    """点时沪深 300 成分中站上 MA60 的比例（至少 100 个有效成分）。"""
    params = ["000300.SH"]
    cutoff = ""
    if source_date:
        cutoff = " AND trade_date<=?"
        params.append(source_date)
    snap_rows = c.execute(
        "SELECT trade_date, ts_code FROM stock_universe_history "
        f"WHERE index_code=?{cutoff} ORDER BY trade_date, ts_code", params
    ).fetchall()
    if not snap_rows:
        return None, None, 0
    members_by_date: dict[str, set[str]] = defaultdict(set)
    for row in snap_rows:
        members_by_date[str(row[0])].add(str(row[1]))
    snap_dates = sorted(members_by_date)
    codes = sorted(set().union(*members_by_date.values()))
    if not codes:
        return None, None, 0

    date_rows = c.execute(
        "SELECT DISTINCT date FROM stock_bars ORDER BY date DESC LIMIT 75"
    ).fetchall()
    dates = sorted(str(r[0]) for r in date_rows)
    if not dates:
        return None, None, 0
    ph = ",".join("?" for _ in codes)
    rows = c.execute(
        "SELECT ts_code, date, close FROM stock_bars "
        f"WHERE date>=? AND ts_code IN ({ph}) ORDER BY ts_code, date",
        [dates[0], *codes],
    ).fetchall()
    by_code: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row in rows:
        if row[2] is not None:
            by_code[str(row[0])].append((str(row[1]), float(row[2])))

    above_by_date: dict[str, list[float]] = defaultdict(list)
    for code, series in by_code.items():
        window: deque[float] = deque(maxlen=60)
        for date, close in series:
            window.append(close)
            # 与旧系统 latest_breadth 一致：近期窗口不足 60 时，至少 30 个样本可用。
            if len(window) < 30:
                continue
            j = bisect.bisect_right(snap_dates, date) - 1
            if j < 0 or code not in members_by_date[snap_dates[j]]:
                continue
            above_by_date[date].append(1.0 if close > sum(window) / len(window) else 0.0)

    valid = [(date, values) for date, values in above_by_date.items() if len(values) >= 100]
    if not valid:
        return None, None, 0
    date, values = max(valid, key=lambda item: item[0])
    return round(sum(values) / len(values), 4), date, len(values)


def market_status() -> dict:
    """总览市场卡的数据契约；不包含旧系统 A/B/C 元账户净值。"""
    labels = {
        "bull": "上行·趋势",
        "range": "震荡·均衡",
        "bear": "下行·防御",
        "insufficient": "数据不足",
    }
    try:
        with _paper_conn() as c:
            regime, regime_date = _market_regime(c)
            breadth, breadth_date, breadth_count = _latest_breadth(c, regime_date)
    except Exception:
        regime, regime_date, breadth, breadth_date, breadth_count = (
            "insufficient", None, None, None, 0
        )
    # source_market_as_of 是同步水位；signal/breadth 各自保留实际计算日期。
    as_of = latest_market_data_date() or regime_date or breadth_date
    if breadth is not None and breadth < 1.0 / 3.0 and regime != "bear":
        regime = "bear"
    return {
        "regime": regime,
        "regime_label": labels.get(regime, "数据不足"),
        "breadth": breadth,
        "breadth_pct": round(breadth * 100, 1) if breadth is not None else None,
        "breadth_threshold": round(100 / 3, 1),
        "breadth_valid_count": breadth_count,
        "as_of": as_of,
        "regime_as_of": regime_date,
        "breadth_as_of": breadth_date,
        "source": "512100 MA20/MA60 + 5日斜率；000300 点时成分 MA60 宽度",
        "risk_rule": "regime=512100均线+斜率与宽度破位并联 · 熔断=回撤15%无条件减半权益 · 每日15:35结算",
    }


def strategy_overview(now: dt.datetime | None = None) -> list[dict]:
    """8 个 qsim 账户的统一总览行；只读 qsim_* 与迁移元数据。"""
    try:
        from qsim.strategies.migration import ACTIVE_MIGRATIONS, migration_for
        from qsim.storage.paper import PAPER_VERSIONED_CANDIDATES
        migrations = {item.name: migration_for(item.name) for item in ACTIVE_MIGRATIONS}
        candidates = PAPER_VERSIONED_CANDIDATES
        names = [item.name for item in ACTIVE_MIGRATIONS] + list(candidates)
    except Exception:
        migrations = {}
        candidates = {}
        names = sorted({a["strategy"] for a in paper_accounts()})

    accounts = {a["strategy"]: a for a in paper_accounts()}
    now = now or dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai")) if now.tzinfo is None else now.astimezone(ZoneInfo("Asia/Shanghai"))
    quote_status = paper_realtime_status(now=now)
    realtime_prices = (
        paper_realtime_prices()
        if (quote_status["fresh"] or quote_status.get("today_data"))
        else {}
    )
    positions = _mark_positions(paper_positions_detail(), realtime_prices)
    positions_by_strategy: dict[str, list[dict]] = defaultdict(list)
    for item in positions:
        positions_by_strategy[item["strategy"]].append(item)
    trades = paper_trades(limit=500)
    trades_by_strategy: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        trades_by_strategy[trade["strategy"]].append(trade)

    market = market_status()
    market_date = market.get("as_of")
    provisional = provisional_paper() or {}
    provisional_by_strategy = {
        str(item.get("strategy")): item for item in provisional.get("strategies") or []
    }
    rows: list[dict] = []
    with _paper_conn() as c:
        for name in names:
            account = accounts.get(name, {})
            candidate = candidates.get(name, {})
            state = migrations[name].state.value if name in migrations else str(candidate.get("state") or "unknown")
            family = migrations[name].family if name in migrations else ("cross_sectional" if candidate else "—")
            execution = migrations[name].execution.value if name in migrations else ("cross_sectional" if candidate else "—")
            provisional_row = provisional_by_strategy.get(name)
            if state == "parity_pending" and provisional_row:
                snapshot_rows = provisional_row.get("snapshots") or []
                settled = snapshot_rows[-1] if snapshot_rows else {}
                settled_date = date_key(settled.get("date"))
                raw_positions = provisional_row.get("positions") or []
                strat_positions = _mark_positions(
                    [dict(position, strategy=name) for position in raw_positions],
                    realtime_prices,
                )
                latest_trade = list(reversed(provisional_row.get("trades") or []))
                initial = float(provisional_row.get("initial_capital") or 100000)
                cash = float(provisional_row.get("cash") or 0)
                if (quote_status["fresh"] and strat_positions
                        and all(item.get("price") is not None for item in strat_positions)):
                    position_value = sum(float(item.get("market_value") or 0) for item in strat_positions)
                    total_value = cash + position_value
                    day_return = (
                        total_value / float(settled["total_value"]) - 1
                        if settled.get("total_value") else None
                    )
                    as_of = quote_status.get("updated_at")
                else:
                    position_value = float(provisional_row.get("position_value") or 0)
                    total_value = float(provisional_row.get("total_value") or initial)
                    day_return = provisional_row.get("day_return") if settled_date == market_date else None
                    as_of = settled_date
                rows.append({
                    "strategy": name,
                    "display_name": candidate.get("display_name", name),
                    "family": family,
                    "execution": execution,
                    "state": state,
                    "ledger": "provisional",
                    "initial_capital": initial,
                    "total_value": round(total_value, 2),
                    "cash": round(cash, 2),
                    "position_value": round(position_value, 2),
                    "total_return": total_value / initial - 1 if initial else 0.0,
                    "day_return": day_return,
                    "since_settled_return": day_return if quote_status["fresh"] else 0.0,
                    "as_of": as_of,
                    "settled_date": settled_date,
                    "signal": str(provisional_row.get("signal") or "空"),
                    "holdings_count": sum(1 for item in strat_positions if int(item.get("shares") or 0) > 0),
                    "star_board_holdings_count": sum(
                        1 for item in strat_positions
                        if int(item.get("shares") or 0) > 0
                        and str(item.get("ts_code") or "").split(".", 1)[0].startswith(("688", "689"))
                    ),
                    "latest_trade_date": str(latest_trade[0]["date"]) if latest_trade else None,
                    "positions": strat_positions,
                    "trades": latest_trade[:5],
                    **account_return_state(settled, snapshot_rows[-2] if len(snapshot_rows)>1 else {},
                                           total_value, strat_positions, quote_status, now, ROOT),
                })
                continue
            snapshots = c.execute(
                "SELECT date, total_value, cash, position_value FROM qsim_snapshots "
                "WHERE strategy=? ORDER BY date DESC LIMIT 2", (name,)
            ).fetchall()
            settled = dict(snapshots[0]) if snapshots else {}
            previous = dict(snapshots[1]) if len(snapshots) > 1 else {}
            strat_positions = positions_by_strategy.get(name, [])
            if strat_positions and all(p["price"] is not None for p in strat_positions):
                position_value = sum(p["market_value"] for p in strat_positions)
            else:
                position_value = float(settled.get("position_value") or 0)
            cash = float(account.get("cash", settled.get("cash", 0)) or 0)
            total_value = round(cash + position_value, 2)
            initial = float(account.get("initial_capital") or 100000)
            settled_date = str(settled.get("date")) if settled.get("date") else None
            return_state = account_return_state(settled, previous, total_value, strat_positions, quote_status, now, ROOT)
            price_dates = [date_key(p.get("price_date")) for p in strat_positions]
            valuation_date = min(price_dates) if price_dates and all(price_dates) else settled_date
            since_settled = (
                total_value / float(settled["total_value"]) - 1
                if settled.get("total_value") else None
            )
            latest_trade = trades_by_strategy.get(name, [])
            rows.append({
                "strategy": name,
                "display_name": candidate.get("display_name", name),
                "family": family,
                "execution": execution,
                "state": state,
                "ledger": "official",
                "initial_capital": initial,
                "total_value": total_value,
                "cash": round(cash, 2),
                "position_value": round(position_value, 2),
                "total_return": total_value / initial - 1 if initial else 0.0,
                **return_state,
                "since_settled_return": since_settled,
                "as_of": valuation_date,
                "settled_date": settled_date,
                "signal": "持" if any(p["shares"] > 0 for p in strat_positions) else "空",
                "holdings_count": sum(1 for p in strat_positions if p["shares"] > 0),
                "star_board_holdings_count": sum(
                    1 for p in strat_positions
                    if p["shares"] > 0
                    and str(p.get("ts_code") or "").split(".", 1)[0].startswith(("688", "689"))
                ),
                "latest_trade_date": str(latest_trade[0]["date"]) if latest_trade else None,
                "positions": strat_positions,
                "trades": latest_trade[:5],
            })
    from qsim.services.web.performance import evaluate, activation_records
    activations = activation_records(ROOT)
    with _paper_conn() as c:
        for row in rows:
            name = row["strategy"]
            if row.get("ledger") == "provisional":
                source = provisional_by_strategy.get(name) or {}
                snapshots = list(source.get("snapshots") or [])
                all_trades = list(source.get("trades") or [])
            else:
                snapshots = [dict(s) for s in c.execute(
                    "SELECT date,total_value,cash,position_value FROM qsim_snapshots "
                    "WHERE strategy=? ORDER BY date", (name,))]
                all_trades = [dict(t) for t in c.execute(
                    "SELECT date,fee FROM qsim_trades WHERE strategy=? ORDER BY date", (name,))]
            try:
                row["performance"] = evaluate(c, row, snapshots, all_trades, market_date, activations.get(name))
            except ValueError:
                # Preserve the engine's rejection of invalid NAV/metric
                # inputs, and make it visible instead of losing the whole
                # overview or silently treating an invalid return as zero.
                row["performance"] = {"status": "invalid_settlement_data",
                    "warnings": ["结算数据无效，收益与绩效未验证；请检查账本"],
                    "observation_note": "结算数据验收失败", "first_date": None, "last_date": None,
                    "total_return": None, "benchmark_return": None, "excess_return": None,
                    "max_drawdown": None, "sharpe": None, "annual_return": None}
    return rows


def _curve_metrics(snapshots: list[dict], initial: float) -> dict:
    """Small, deterministic metrics for the strategy-page live cards."""
    from qsim.services.web.performance import curve_metrics
    return curve_metrics(snapshots, initial)


def _position_price_history(code: str) -> list[tuple[str, float]]:
    """Close history for a displayed holding, using qsim market tables only."""
    code = str(code)
    code6 = code[:6]
    try:
        with _paper_conn() as c:
            rows = c.execute(
                "SELECT date, close FROM bars WHERE symbol=? ORDER BY date", (code6,)
            ).fetchall()
            if not rows:
                rows = c.execute(
                    "SELECT date, close FROM stock_bars "
                    "WHERE ts_code IN (?, ?, ?) ORDER BY date",
                    (f"{code6}.SH", f"{code6}.SZ", f"{code6}.BJ"),
                ).fetchall()
        return [(str(row[0]), float(row[1])) for row in rows if row[1] is not None and float(row[1]) > 0]
    except Exception:
        return []


def _holding_periods(price: float, shares: int, history: list[tuple[str, float]],
                     account_start: str | None) -> dict[str, dict]:
    """Same period semantics as the legacy heatmap: area=current value, color=return."""
    def one(label: str, base: float, base_date: str) -> dict:
        base = float(base or price)
        pct = price / base - 1 if base else 0.0
        return {
            "label": label, "pct": pct, "pnl": (price - base) * shares,
            "base_price": base, "base_date": base_date,
        }

    def historical(label: str, sessions: int) -> dict:
        if not history:
            return one(label, price, "—")
        index = max(0, len(history) - sessions) if sessions else 0
        if account_start:
            first_account = next(
                (i for i, (date, _) in enumerate(history) if date >= account_start),
                len(history) - 1,
            )
            index = max(index, first_account)
        date, base = history[index]
        return one(label, base, date)

    previous = history[-2] if len(history) >= 2 else (history[-1] if history else ("—", price))
    return {
        "today": one("今日", previous[1], previous[0]),
        "3d": historical("3日", 3),
        "1m": historical("1月", 22),
        "all": historical("全部", 0),
    }


def strategy_visuals() -> list[dict]:
    """Current effective ledger data for the 8503-style strategy cards.

    Pending strategies read their daily provisional ledger; paper-enabled
    strategies read qsim_* directly.  This is a presentation selector only,
    not a promotion or execution decision.
    """
    overview = strategy_overview()
    provisional = provisional_paper() or {}
    provisional_by_strategy = {
        str(item.get("strategy")): item for item in provisional.get("strategies") or []
    }
    visual_status = paper_realtime_status()
    marked = _mark_positions(
        [dict(position, strategy=row["strategy"])
         for row in overview for position in row.get("positions") or []],
        paper_realtime_prices()
        if (visual_status["fresh"] or visual_status.get("today_data"))
        else {},
    )
    marked_by_strategy: dict[str, list[dict]] = defaultdict(list)
    for item in marked:
        marked_by_strategy[str(item["strategy"])].append(item)

    out: list[dict] = []
    quote_status = paper_realtime_status()
    quote_date = str(quote_status.get("updated_at") or "")[:10]
    for row in overview:
        name = str(row["strategy"])
        if str(row.get("state") or "") in {"retired", "historical_only"}:
            continue
        if row.get("ledger") == "provisional":
            snapshots = list((provisional_by_strategy.get(name) or {}).get("snapshots") or [])
        else:
            snapshots = paper_snapshots(name)
        snapshots = [dict(snapshot) for snapshot in snapshots]
        daily_snapshots = list(snapshots)
        # Evaluation curves contain settled points only. Current marks remain
        # visible in holdings/overview and never change historical risk metrics.
        initial = float(row.get("initial_capital") or 100000)
        cells = []
        for position in marked_by_strategy.get(name, []):
            price = position.get("price")
            shares = int(position.get("shares") or 0)
            if price is None or shares <= 0:
                continue
            price_value = float(price)
            history = _position_price_history(str(position.get("ts_code") or ""))
            periods = _holding_periods(
                price_value, shares, history,
                str(snapshots[0]["date"]) if snapshots else None,
            )
            cells.append({
                "code": str(position.get("ts_code") or ""),
                "shares": shares,
                "price": price_value,
                "market_value": round(price_value * shares, 2),
                "avg_cost": float(position.get("avg_cost") or 0),
                "periods": periods,
            })
        metrics = _curve_metrics(daily_snapshots, initial)
        out.append({
            "strategy": name,
            "family": row.get("family"),
            "ledger": row.get("ledger", "official"),
            "state": row.get("state"),
            "initial_capital": initial,
            "snapshots": snapshots,
            "holdings": cells,
            "metrics": metrics,
            "performance": row.get("performance") or {},
            "has_intraday_points": False,
        })
    return out
