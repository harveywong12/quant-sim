"""Daily AI review for the qsim paper book.

Deterministic rules verify the flow first (qc_check); the LLM only
interprets. Empty or invalid LLM output degrades to a template report so
the daily review is never missing. Reports persist in the shared
ai_reports table and a summary is pushed via ntfy.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sqlite3
from pathlib import Path

import requests

from qsim.services import notify, qc_check

logger = logging.getLogger("qsim.ai_advisor")

ROOT = Path(__file__).resolve().parents[2]
QUANT_DB = ROOT / "data" / "quant.db"
SHANGHAI = dt.timezone(dt.timedelta(hours=8))

SYSTEM_PROMPT = """你是一名 A 股量化模拟盘的 AI 复盘顾问兼质控员。用户提供"今日数据快照"和"流程核验结果"，请基于它们输出复盘，严格遵守：
1. 只基于提供的数据，禁止编造行情、持仓、收益、事件等未提供的信息。
2. "流程核验结果"与"确定性策略总表"由确定性规则生成，是事实；你只能解读其原因、影响和优先级，不得把 fail 说成正常，也不得把 pass 说成异常。
3. 系统事件中只有"今日系统事件"才算当前风险；"历史系统事件"已过去、仅作背景参考，不得写入总结、issues 或任何"当前问题"表述。
4. 报告必须是单层 JSON。summary 字段只能是普通文本字符串，绝对禁止在 summary 中嵌套 JSON 对象、数组或转义的 JSON 字符串。
5. 你只做复盘解读，不预测明日涨跌、不推荐买卖标的、不调整策略参数、不执行任何交易操作。策略表现只能说"与数据一致/可能相关"，不能断言因果。
6. 输出必须是合法 JSON，结构：
{"summary": "仅市场摘要（2-4句）。不得写流程、系统事件、策略或 AI 服务故障；必须覆盖市场分类、广度/结构、领涨领跌、趋势和量能。",
 "flow_check": {"ok": true/false, "failed_rules": ["核验失败项名称"], "notes": ["对 fail/warn 项的解读与可能原因"]},
 "market_review": {"summary": "基于市场代理数据的当日市场解读（3-5句，无预测；依次说明广度、领涨领跌、短中期趋势、量能与结构分化）", "caveats": ["数据范围或不确定性，可空"]},
 "system_review": {"summary": "仅系统运行与流程核验解读，不得讨论市场", "review_questions": ["需要人工核实的问题，可空"]},
 "portfolio_review": {"summary": "跨策略组合风险与共同现象总结（1-3句）", "review_questions": ["需要人工核实的问题，可空"]},
 "strategy_reviews": [{"strategy": "必须与确定性策略总表名称完全一致", "summary": "该策略的当日表现解读（1-2句）", "review_questions": ["需要人工核实的问题，可空"]}],
 "issues": ["风险点/异常列表，每条一行"],
 "suggestions": ["流程、数据或风控改进建议；禁止交易指令"],
 "tomorrow": ["确定性待执行事项或人工关注点，可空"],
 "yesterday_followup": [{"item": "昨日待办原句或编号", "status": "已落实/未落实/不适用/不确定", "evidence": "核对依据（1句，必须引用今日数据或核验结果）"}],
 "move_attribution": [{"code": "6位代码", "name": "名称", "change": "+3.2%", "driver": "个股/行业/市场", "analysis": "1-2句归因，仅基于提供的确定性素材，禁止臆测未提供的消息"}]}
7. strategy_reviews 必须覆盖"确定性策略总表"中的每一个策略，strategy 名称必须完全一致；没有足够数据时写"数据不足"，不得省略。
8. 若数据不足无法判断，明确说明，不要硬编造结论。
9. 若提供了"昨日 AI 复盘待核对事项"：逐条核对写入 yesterday_followup；已落实的不得再列入 issues；未落实且仍重要的可升级写入 issues 并注明"连续 N 日未落实"；无昨日事项时 yesterday_followup 输出空数组。
10. 若提供了"今日持仓异动（确定性检测）"：逐只归因写入 move_attribution，driver 三选一（市场/行业/个股）；无异动素材时 move_attribution 输出空数组。
11. 账户中的"待执行/信号"是收盘后生成、供下一交易日执行的目标；今日成交执行的是此前信号。二者方向不同不构成执行矛盾，除非确定性核验明确判为异常。
12. "今日系统事件"是事件发生记录，不一定代表生成报告时仍未恢复；若后置流程核验已 pass，则必须表述为"同日早先发生且当前已恢复"，不得继续把旧偏差、旧缺口或旧数值列为当前问题。"""


class LlmClient:
    def __init__(self) -> None:
        raise RuntimeError("云端业务上下文接口已禁用；真实信号、持仓、净值留在本地")

    def chat(self, *args, **kwargs) -> str:
        raise RuntimeError("cloud business-context export disabled")


def _extract_json_object(text: str) -> str | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_report(raw: str) -> dict:
    blob = _extract_json_object(raw or "")
    if not blob:
        return {}
    try:
        report = json.loads(blob)
    except ValueError:
        return {}
    return report if isinstance(report, dict) else {}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(QUANT_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _strategy_table(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT a.strategy, a.status, a.cash, a.shares, a.initial_capital, "
        "MAX(s.date) AS snap_date, s.total_value "
        "FROM qsim_accounts a "
        "LEFT JOIN qsim_snapshots s ON s.strategy=a.strategy "
        "AND s.date=(SELECT MAX(date) FROM qsim_snapshots WHERE strategy=a.strategy) "
        "GROUP BY a.strategy ORDER BY a.strategy"
    ).fetchall()
    out = []
    for r in rows:
        total = float(r["total_value"]) if r["total_value"] else None
        initial = float(r["initial_capital"] or 0)
        ret = (total / initial - 1) if total and initial else None
        out.append({
            "strategy": r["strategy"],
            "status": r["status"],
            "cash": round(float(r["cash"] or 0), 2),
            "shares": int(r["shares"] or 0),
            "total_value": round(total, 2) if total else None,
            "total_return": f"{ret:+.2%}" if ret is not None else "n/a",
        })
    return out


def _today_trades(conn: sqlite3.Connection, date: str) -> list[str]:
    rows = conn.execute(
        "SELECT strategy, symbol, side, price, shares, reason FROM qsim_trades "
        "WHERE date=? ORDER BY id", (date,)).fetchall()
    return [
        f"{r['strategy']} {r['side']} {r['symbol']} "
        f"{r['shares']}股@{r['price']} ({r['reason'] or ''})"
        for r in rows
    ]


def _open_positions(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT a.strategy, p.ts_code, p.shares, p.avg_cost "
        "FROM qsim_positions p JOIN qsim_accounts a ON a.id=p.account_id "
        "WHERE p.shares > 0 ORDER BY a.strategy, p.ts_code"
    ).fetchall()
    return [
        f"{r['strategy']} {r['ts_code']} {int(r['shares'])}股 成本{r['avg_cost']}"
        for r in rows
    ]


def _today_events(conn: sqlite3.Connection, date: str) -> list[str]:
    prefix = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    rows = conn.execute(
        "SELECT ts, level, category, message FROM event_log "
        "WHERE ts LIKE ? ORDER BY id DESC LIMIT 40",
        (prefix + "%",)).fetchall()
    return [f"{r['ts']} [{r['level']}/{r['category']}] {r['message']}"
            for r in rows]


def _yesterday_todo(conn: sqlite3.Connection, date: str) -> str:
    rows = conn.execute(
        "SELECT report_json FROM ai_reports WHERE date<? ORDER BY date DESC LIMIT 1",
        (date,)).fetchall()
    if not rows:
        return ""
    try:
        report = json.loads(rows[0]["report_json"])
    except (ValueError, TypeError):
        return ""
    todos = report.get("tomorrow") or []
    if not todos:
        return ""
    return "\n".join(f"- {t}" for t in todos)


def build_context(conn: sqlite3.Connection, date: str,
                  qc_checks: list[dict]) -> str:
    strategies = _strategy_table(conn)
    lines = [
        f"# 今日数据快照（{date}）",
        "",
        "## 确定性策略总表（事实，勿修改）",
        json.dumps(strategies, ensure_ascii=False, indent=1),
        "",
        "## 今日成交",
        "\n".join(_today_trades(conn, date)) or "无成交",
        "",
        "## 当前持仓",
        "\n".join(_open_positions(conn)) or "空仓",
        "",
        "## 今日系统事件（含级别）",
        "\n".join(_today_events(conn, date)) or "无",
        "",
        "--- 流程核验结果（确定性，勿修改） ---",
        "\n".join(f"[{c['status']}] {c['check']}: {c['detail']}"
                  for c in qc_checks),
    ]
    todo = _yesterday_todo(conn, date)
    if todo:
        lines += ["", "## 昨日 AI 复盘待核对事项", todo]
    return "\n".join(lines)


def _fallback_report(qc_checks: list[dict], model: str) -> dict:
    failed = [c["check"] for c in qc_checks if c["status"] == "fail"]
    warned = [c["check"] for c in qc_checks if c["status"] == "warn"]
    verdict = qc_check.flow_verdict(qc_checks)
    issues = []
    if failed:
        issues.append("流程核验失败: " + "；".join(failed))
    if warned:
        issues.append("流程核验待关注: " + "；".join(warned))
    return {
        "summary": (f"AI 模型暂不可用，以下为确定性核验结果摘要。流程核验: {verdict}。"),
        "flow_check": {
            "ok": verdict == "ok",
            "verdict": verdict,
            "failed_rules": failed,
            "warn_rules": warned,
            "checks": [{"check": c["check"], "status": c["status"]}
                       for c in qc_checks],
        },
        "issues": issues,
        "suggestions": ["模型服务恢复后重新生成完整复盘"],
        "model": model,
    }


def _attach_flow(report: dict, qc_checks: list[dict]) -> dict:
    verdict = qc_check.flow_verdict(qc_checks)
    report["flow_check"] = {
        "ok": verdict == "ok",
        "verdict": verdict,
        "failed_rules": [c["check"] for c in qc_checks if c["status"] == "fail"],
        "warn_rules": [c["check"] for c in qc_checks if c["status"] == "warn"],
        "checks": [{"check": c["check"], "status": c["status"]}
                   for c in qc_checks],
    }
    return report


def run_daily_review(date: str | None = None) -> dict:
    """Cloud business-context export disabled by the NAS audit boundary."""
    return {"ok": True, "status": "disabled", "reason": "cloud business-context export disabled"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run_daily_review(), ensure_ascii=False))
