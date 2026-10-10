"""Read-only evaluation of settled paper NAVs and same-date benchmarks."""
from __future__ import annotations

import json
import math
import statistics

BENCHMARKS = {
    "tsmom_vol": ("513100", "纳指ETF"),
    "trend_fallback": ("518880", "黄金ETF（主资产参照）"),
    "rsi_boll": ("518880", "黄金ETF"),
    "grid_atr": ("512100", "中证1000ETF"),
    "cb_dual_low": ("511380", "可转债ETF"),
}


def curve_metrics(snapshots: list[dict], initial: float) -> dict:
    if not snapshots or initial <= 0:
        return {"total_return": None, "max_drawdown": None, "sharpe": None}
    import pandas as pd
    from qsim.engine.metrics import portfolio_metrics
    metrics = portfolio_metrics(pd.DataFrame(snapshots), initial, 0)
    return {key: metrics[key] for key in ("total_return", "max_drawdown", "sharpe")}


def evaluate(conn, row: dict, snapshots: list[dict], trades: list[dict],
             market_date: str | None, activation: dict | None = None) -> dict:
    initial = float(row.get("initial_capital") or 100000)
    name = row["strategy"]
    first = snapshots[0]["date"] if snapshots else None
    last = snapshots[-1]["date"] if snapshots else None
    code, label = BENCHMARKS.get(name, ("510300", "沪深300ETF"))
    # Multi-asset allocation has no unique passive replica: show an explicitly
    # named equity reference, never imply it is a risk-matched benchmark.
    if name == "multi_asset_mom":
        label = "沪深300ETF（权益参照，非风险匹配）"
    curves = []
    benchmark_return = None
    benchmark_note = "至少需要两个结算点"
    if len(snapshots) >= 2:
        prices = dict(conn.execute(
            "SELECT date,close FROM bars WHERE symbol=? AND date>=? AND date<=?",
            (code, first, last)))
        if all(float(prices.get(r["date"]) or 0) > 0 for r in snapshots):
            base = float(prices[first])
            curves = [{"date": r["date"], "total_value": initial * float(prices[r["date"]]) / base}
                      for r in snapshots]
            benchmark_return = float(prices[last]) / base - 1
            benchmark_note = "同结算日期收盘价格收益；未计分红、手续费、滑点"
        else:
            benchmark_note = "基准缺少同日期行情，暂停计算超额"
    retired = row.get("state") in {"retired", "historical_only"}
    stale = bool(last and market_date and last < market_date and not retired)
    activity = "已成交" if trades else "尚未成交"
    warnings = []
    if stale:
        warnings.append(f"结算截至 {last}，落后行情 {market_date}；当前估值不能代替日结算")
    if len(snapshots) < 20:
        warnings.append("样本不足20个交易日，风险指标仅供观察")
    if not trades:
        warnings.append("尚未成交，零收益不代表策略有效")
    metrics = curve_metrics(snapshots, initial)
    promotion = (activation or {}).get("promotion", {}).get("promoted_at")
    rebaseline = (activation or {}).get("rebaseline", {}).get("authorized_at")
    evidence = None
    try:
        # Account creation dates and promotion dates are not proof of live NAV.
        evidence = conn.execute(
            "SELECT MIN(executed_at) FROM qsim_order_intents "
            "WHERE strategy=? AND status='executed' AND executed_at IS NOT NULL", (name,)).fetchone()[0]
    except Exception:
        pass
    observation_note = (f"开盘执行记录始于 {evidence[:10]}；日结算曲线仍须单独核验"
                        if evidence else "逐日实时运行区间尚无完整证据；账本含历史回填")
    total = float(snapshots[-1]["total_value"]) if snapshots else None
    exposure = [float(s.get("position_value") or 0) / float(s["total_value"])
                for s in snapshots if float(s["total_value"]) > 0]
    return {**metrics, "first_date": first, "last_date": last, "days": len(snapshots),
            "settled_value": total, "stale": stale, "activity": activity,
            "trade_count": len(trades), "fees": sum(float(t.get("fee") or 0) for t in trades),
            "invested": float(snapshots[-1].get("position_value") or 0) / total if total else None,
            "mean_invested": sum(exposure) / len(exposure) if exposure else None,
            "benchmark_code": code, "benchmark_name": label,
            "benchmark_return": benchmark_return, "benchmark_curve": curves,
            "benchmark_note": benchmark_note,
            "excess_return": metrics["total_return"] - benchmark_return
                if metrics["total_return"] is not None and benchmark_return is not None else None,
            "activation_date": (promotion or rebaseline or "")[:10] or None,
            "observation_note": observation_note, "warnings": warnings}


def activation_records(root) -> dict:
    try:
        payload = json.loads((root / "config" / "strategy-migration.json").read_text())
        return {item["name"]: item for item in payload.get("strategies", [])}
    except (OSError, ValueError, KeyError):
        return {}
