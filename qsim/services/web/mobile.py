"""qsim 展示网页：5-Tab（总览/模拟盘/实验/对账/AI[复盘+对话+系统]）。

数据来源（全部 qsim 自有）：
  - 模拟盘   : qsim_* 表（策略账户/持仓/净值/成交）
  - 实验     : artifacts/experiments.jsonl + artifacts/equity/
  - 对账     : artifacts/shadow/
  - 系统     : API 状态 + 策略池 + 热区快照

设计：量化终端风（近黑底 / 琥珀金交互 / 等宽数字 / SSE 推送自动刷新）。
"""

from __future__ import annotations

import datetime as dt
import time
import threading
import html as _html
import math
import hashlib

from qsim.services.web import qsim_data as q
from qsim.services.web.page_template import PAGE
from qsim.services.web.render import (
    ACCENT, DANGER, WARN, BG_CARD, LINE, TEXT, DIM, BLUE,
    _stat_card, _tcard, _data_table, _curve_echarts, _html_safe,
    _fmt_date,
)

STATUS = {"mode": "meta_shadow_v1", "risk": "fixed_tiered_drawdown",
          "scheduler_jobs": ["sync_refactor_data", "paper_daily", "meta_shadow", "shadow_diff",
                             "realtime_quotes_5m", "realtime_quotes_close"]}

ETF_NAMES = {
    "512100": "中证1000ETF", "510300": "沪深300ETF", "510500": "中证500ETF",
    "518880": "黄金ETF", "513100": "纳指ETF", "511380": "可转债ETF",
    "513030": "德国DAX", "511880": "银华日利", "511010": "5年国债ETF",
    "512480": "半导体ETF", "159915": "创业板ETF", "588000": "科创50ETF",
}


def _stock_name(code: str) -> str:
    """名称：ETF 用内置映射，股票用 qsim stock_info（有则查）。"""
    code6 = str(code)[:6]
    if code6 in ETF_NAMES:
        return ETF_NAMES[code6]
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{q.ROOT / 'data' / 'quant.db'}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT name FROM stock_info WHERE ts_code LIKE ? LIMIT 1",
            (f"{code6}%",),
        ).fetchone()
        conn.close()
        if row:
            return str(row[0])
    except Exception:
        pass
    return code6


# ═══════════════ Tab 1: 总览 ═══════════════

def _pct(value: float | None, *, dash: str = "—") -> str:
    if value is None or not math.isfinite(value):
        return f'<span class="mut" title="当前没有与行情时点一致的结算快照">{dash}</span>'
    cls = "up" if value >= 0 else "down"
    return f'<span class="{cls}">{value * 100:+.2f}%</span>'


def _market_state_card() -> str:
    """8611 的统一市场卡：保留状态/宽度/风控规则，不展示旧 A/B/C 元净值。"""
    market = q.market_status()
    regime = _html_safe(market.get("regime_label") or "数据不足")
    regime_tone = "down" if market.get("regime") == "bear" else "up"
    breadth = market.get("breadth_pct")
    breadth_text = f"{breadth:.1f}%" if breadth is not None else "—"
    as_of = _fmt_date(str(market.get("as_of") or "—"))
    regime_as_of = _fmt_date(str(market.get("regime_as_of") or "—"))
    breadth_as_of = _fmt_date(str(market.get("breadth_as_of") or "—"))
    body = (
        '<div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;'
        'font-size:17px;margin-bottom:16px;">'
        '<span class="mut">市场状态</span>'
        f'<b class="{regime_tone}">{regime}</b>'
        '<span class="mut">市场宽度</span>'
        f'<b class="up">{breadth_text}</b>'
        '<span class="mut" style="font-size:12px;">(300 成分股站上 MA60 占比，&lt;33% 破位)</span>'
        '</div>'
        '<div style="border-top:1px solid var(--q-line);padding-top:12px;'
        'color:var(--q-dim);line-height:1.7;">'
        f'行情时点 <b style="color:var(--q-text)">{as_of}</b>'
        f' · 状态计算 {regime_as_of}'
        f' · 宽度计算 {breadth_as_of}'
        f' · 有效成分 {market.get("breadth_valid_count") or 0}'
        '</div>'
        f'<div class="note">{_html_safe(market.get("risk_rule") or "")}</div>'
        f'<div class="note">来源：{_html_safe(market.get("source") or "qsim 同步行情")}</div>'
    )
    return _tcard("🌡️ 市场状态 · 风控层", body, col="col-12")


def _strategy_type(row: dict) -> str:
    return {
        "trend": "趋势",
        "mean_reversion": "均值回归",
        "defensive": "防御",
        "cross_sectional": "横截面",
    }.get(str(row.get("family")), str(row.get("family") or "—"))


def _strategy_detail(row: dict) -> str:
    positions = row.get("positions") or []
    trades = row.get("trades") or []
    pos_lines = []
    for p in positions:
        if int(p.get("shares") or 0) <= 0:
            continue
        px = p.get("price")
        value = p.get("market_value")
        price_text = f"现价 {float(px):.3f}" if px is not None else "现价 —"
        value_text = f"市值 {float(value):,.0f}" if value is not None else "市值 —"
        pos_lines.append(
            f'<div class="q-detail-line"><span>{_html_safe(p.get("ts_code"))}</span>'
            f'<span class="q-detail-val">{int(p.get("shares") or 0):,} 股 · '
            f'成本 {float(p.get("avg_cost") or 0):.3f} · {price_text} · {value_text} · '
            f'报价 {_fmt_date(str(p.get("price_date") or "—"))}</span></div>'
        )
    trade_lines = []
    for t in trades:
        side = "买入" if str(t.get("side", "")).upper() == "BUY" else "卖出"
        side_cls = "up" if side == "买入" else "down"
        trade_lines.append(
            f'<div class="q-detail-line"><span class="{side_cls}">{side}</span>'
            f'<span class="q-detail-val">{_fmt_date(str(t.get("date") or "—"))} · '
            f'{_html_safe(t.get("symbol"))} · {int(t.get("shares") or 0):,} 股 · '
            f'{float(t.get("price") or 0):.3f}</span></div>'
            f'<div class="note">费用 {float(t.get("fee") or 0):.2f} · '
            f'依据 {_html_safe(t.get("reason") or "历史记录未保存信号依据")} · '
            f'版本 {_html_safe((str(t["execution_version"])[:12] if t.get("execution_version") else t.get("code_hash")) or "历史未记录")} · 批次 {_html_safe(t.get("run_id") or "历史未记录")}</div>'

        )
    if not pos_lines:
        pos_lines = ['<div class="sub">当前无持仓</div>']
    if not trade_lines:
        trade_lines = ['<div class="sub">暂无成交</div>']
    settled = _fmt_date(str(row.get("settled_date") or "—"))
    as_of = _fmt_date(str(row.get("as_of") or "—"))
    since = _pct(row.get("since_settled_return"))
    star_note = (
        f' · 科创板 {int(row.get("star_board_holdings_count") or 0)}'
        if str(row.get("strategy")) == "multi_factor_v4" else ""
    )
    return (
        f'<summary>查看持仓与成交 · 持仓 {int(row.get("holdings_count") or 0)}{star_note} · 成交 {int((row.get("performance") or {}).get("trade_count", len(trades)))} · '
        f'数据 {as_of}</summary>'
        '<div class="q-detail-inner">'
        f'<div class="q-detail-line"><span>结算快照</span><span class="q-detail-val">{settled} · '
        f'快照后估值 {since}</span></div>'
        f'<div class="q-detail-sec">持仓明细</div>{"".join(pos_lines)}'
        f'<div class="q-detail-sec">最近成交</div>{"".join(trade_lines)}'
        '</div>'
    )


def _strategy_overview_card() -> str:
    rows = []
    details = {}
    detail_ids = {}
    overview = q.strategy_overview()
    for i, item in enumerate(overview):
        # Account identity remains stable when rows change order on a refresh.
        identity = str(item.get("ledger") or "official") + "\0" + str(item.get("strategy") or "")
        detail_id = "qsim-account-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        detail_ids[i] = detail_id
        day = item.get("day_return")
        day_cell = _pct(day)
        day_label = {"settled": "今日已结算", "intraday_estimate": "今日盘中估算",
                     "awaiting_today_settlement": "今日待结算", "previous_settlement_missing": "前一交易日结算缺失",
                     "quotes_incomplete": "持仓报价不完整", "valuation_components_missing": "估值组成未齐",
                     "calendar_unavailable": "交易日历未验证", "snapshot_unavailable": "结算快照不可用",
                     "market_closed": "非交易日"}.get(item.get("day_return_status"), "收益日期未验证")
        day_cell += f'<br><span class="mut">{_html_safe(day_label)}</span>'
        if (item.get("performance") or {}).get("status") == "invalid_settlement_data":
            day_cell += '<br><span class="down">结算数据无效 · 未验证</span>'
        if item.get("settled_return_date"):
            period_dates = f'{_fmt_date(str(item.get("settled_return_from_date") or "—"))} → {_fmt_date(str(item["settled_return_date"]))}'
            day_cell += (f'<br><span class="mut">最近结算区间 {_html_safe(period_dates)}</span><br>'
                         + _pct(item.get("settled_period_return")))
        state = str(item.get("state") or "—")
        state_label = {"paper_enabled": "正式", "paper_candidate": (
                           "候选 · 未提升"),
                       "shadowing": "影子", "parity_pending": "待验证",
                       "retired": "已退休", "historical_only": "仅历史"}.get(state, state)
        state_cls = "tag-g" if state == "paper_enabled" else "tag-y"
        rows.append([
            f'<b>{_html_safe(item.get("display_name") or item.get("strategy"))}</b>',
            f'<span class="tag {state_cls}" title="迁移状态：{_html_safe(state_label)}；账本：{_html_safe(item.get("ledger") or "official")}；执行：{_html_safe(item.get("execution") or "—")}">'
            f'{_html_safe(_strategy_type(item))}</span><br><span class="mut">{_html_safe(state_label)}</span>',
            f'{float(item.get("total_value") or 0):,.0f}',
            day_cell,
            _pct((item.get("performance") or {}).get("total_return")),
            f'<button type="button" class="q-holdings-button" data-qsim-detail-target="{detail_id}" '
            f'aria-controls="{detail_id}" aria-expanded="false" '
            f'aria-label="查看{_html_safe(item.get("display_name") or item.get("strategy"))}的持仓与成交">'
            f'查看持仓 · {int(item.get("holdings_count") or 0)} 只'
            f'{" / 科创板 " + str(int(item.get("star_board_holdings_count") or 0)) if str(item.get("strategy")) == "multi_factor_v4" else ""}'
            f'</button><span class="q-holdings-cash">现金 {float(item.get("cash") or 0):,.0f} 元</span>',
            f'<span class="tag tag-y">{_html_safe(item.get("signal") or "—")}</span>',
            _fmt_date(str(item.get("latest_trade_date") or "—")),
        ])
        details[i] = _strategy_detail(item)
    if not rows:
        return _tcard("策略总览 · 现账户", '<div class="sub">暂无 qsim 账户数据</div>', col="col-12")
    quote_status = q.paper_realtime_status()
    quote_at = _fmt_date(str(quote_status.get("updated_at") or "—"))
    if quote_status.get("fresh"):
        quote_note = (f'<span class="tag tag-g">报价缓存更新 · {quote_at}</span> '
                      '每行分别核对持仓报价和收益日期。')
    elif quote_status.get("in_session"):
        quote_note = (f'<span class="tag tag-y">盘中行情过期 · {quote_at}</span> '
                      '估算资产采用最近可用价格，报价日期见持仓明细。')
    else:
        quote_note = (f'<span class="tag tag-y">非交易时段 · 最近报价 {quote_at}</span> '
                      '估算资产采用最近可用价格，报价日期见持仓明细。')
    note = ('<div class="note" style="padding:10px 14px 0;">'
            + quote_note + '<br>'
            '估算资产与持仓采用最近可用报价；已结算收益只读取日结算账本，与下方评价面板及曲线一致。'
            '今日收益明确区分已结算、盘中估算和待结算；最近结算区间收益显示实际日期，直接取账本快照。'
            '盘中报价未启用或数据未齐时不生成今日收益。各策略起止日期不同，不能按累计收益直接排名。</div>')
    table = _data_table(
        ["策略", "类型", "估算资产", "今日收益 / 状态", "已结算累计收益", "持仓 / 现金", "信号", "最新交易"],
        rows, right=(2, 3, 4, 5, 7), cls="t-strat", details=details, detail_ids=detail_ids,
    )
    return _tcard("策略总览 · 现账户", table + note, col="col-12", pad=False)


def _performance_card() -> str:
    rows = []
    for row in q.strategy_overview():
        if row.get("state") in {"retired", "historical_only"}:
            continue
        p = row.get("performance") or {}
        dates = f'{_fmt_date(str(p.get("first_date") or "—"))} → {_fmt_date(str(p.get("last_date") or "—"))}'
        warnings = "；".join(p.get("warnings") or [])
        dates += f'<br><span class="mut">{int(p.get("days") or 0)} 个结算点 · 含回填</span>'
        if warnings:
            dates += f'<br><span class="down">{_html_safe(warnings)}</span>'
        observation = _html_safe(p.get("observation_note") or "实时区间未核验")
        if p.get("activation_date"):
            observation += f'<br>启用记录 {_html_safe(p["activation_date"])}（不代表实时净值起点）'
        rows.append([
            f'<b>{_html_safe(row.get("display_name") or row["strategy"])}</b>', dates,
            _pct(p.get("total_return")),
            f'{_html_safe(p.get("benchmark_name"))} ({_html_safe(p.get("benchmark_code"))})<br>{_pct(p.get("benchmark_return"))}',
            _pct(p.get("excess_return")), _pct(p.get("max_drawdown")),
            f'{_pct(p.get("invested"))}<br><span class="mut">均值 {_pct(p.get("mean_invested"))}</span>',
            f'{int(p.get("trade_count") or 0)} 笔 · {_html_safe(p.get("activity"))}<br>手续费 {float(p.get("fees") or 0):.2f}',
            observation,
        ])
    note = ('<div class="note" style="padding:12px 14px;">收益、回撤、仓位均使用已结算日净值；回撤包含初始本金。'
            '超额 = 策略收益 − 基准收益（百分点），基准按相同结算日期的首日收盘价归一；未计分红、手续费、滑点。'
            '黄金是趋势回退的主资产参照，多资产策略的沪深300仅为权益参照，均非风险匹配基准。'
            '回填区间与实时运行证据分别标识；候选、短样本、未成交及结算落后账户不能据此直接排名。</div>')
    return _tcard("策略评价 · 已结算与同期间基准", _data_table(
        ["策略", "账本区间", "收益", "基准", "超额（百分点）", "最大回撤", "仓位", "成交 / 费用", "运行证据"],
        rows, right=(2, 4, 5, 6, 7)) + note, col="col-12", pad=False)


def _meta_shadow_card() -> str:
    """Explain the independent meta evidence without presenting it as an account."""
    payload = q.meta_shadow()
    if not payload:
        return _tcard("🧭 Meta 组合 · 影子验证", '<div class="sub">尚未生成影子组合证据</div>', col="col-12")
    latest = payload.get("latest") or {}
    risk = latest.get("risk") or {}
    weights = latest.get("weights_effective") or {}
    sleeves = latest.get("sleeve_weights_effective") or latest.get("target_sleeve_weights") or {}
    status = str(risk.get("status") or "shadow_warming_up")
    ready = status == "shadow_ready"
    state = "观测中" if ready else "预热中"
    state_cls = "tag-g" if ready else "tag-y"
    weight_bars = []
    for name, weight in sorted(weights.items()):
        pct = float(weight) * 100
        weight_bars.append(
            f'<div class="q-wrow"><span class="n">{_html_safe(name)}</span>'
            f'<span class="bar"><i style="width:{pct:.1f}%"></i></span>'
            f'<span class="p">{pct:.1f}%</span></div>')
    if not weight_bars:
        weight_bars.append(
            '<div class="q-wrow"><span class="n">现金</span>'
            '<span class="bar"><i style="width:100%"></i></span>'
            '<span class="p">100.0%</span></div>')
    eligibility = risk.get("eligibility") or {}
    member_notes = []
    for name, detail in sorted(eligibility.items()):
        member_notes.append(
            f'{_html_safe(name)}：{_html_safe(str(detail.get("reason") or "—"))}'
        )
    nav = float(latest.get("nav") or payload.get("initial_capital") or 0)
    drawdown = float(latest.get("drawdown") or 0) * 100
    cash_weight = float(latest.get("cash_weight_effective") or 0) * 100
    forecast = risk.get("forecast_vol_annual")
    forecast_text = f"{float(forecast) * 100:.1f}%" if forecast is not None else "—"
    sleeve_text = " · ".join(
        f"{_html_safe(name)} {float(weight) * 100:.1f}%" for name, weight in sorted(sleeves.items())
    ) or "当前尚无已生效 sleeve 权重"
    body = (
        '<div style="display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;margin-bottom:12px;">'
        f'<span class="tag {state_cls}">{state}</span>'
        f'<span class="mut">数据至 {_fmt_date(str(payload.get("source_as_of") or "—"))}</span>'
        f'<span class="mut">影子 NAV <b class="q-num q-num-lg" style="color:var(--q-text)">{nav:,.0f}</b></span>'
        f'<span class="mut">回撤 <b class="q-num">{drawdown:.2f}%</b></span>'
        f'<span class="mut">预测年化波动 <b class="q-num">{forecast_text}</b></span>'
        '</div>'
        f'<div class="note" style="padding:0 0 10px;">sleeve：{sleeve_text} · 现金 {cash_weight:.1f}%</div>'
        '<div class="q-detail-sec">策略权重（已生效）</div>'
        + "".join(weight_bars)
        + '<div class="note" style="padding:10px 0 0;">'
        '只读影子组合：权重在收盘后生成、下一交易日才生效；不创建账户，不写入任一策略的持仓、成交或净值。</div>'
        f'<details class="q-detail-box" style="margin-top:6px;">'
        f'<summary>成员门禁明细（{len(member_notes)}）</summary>'
        f'<div class="note" style="padding:4px 0 0;">{_html_safe("；".join(member_notes))}</div>'
        '</details>'
    )
    return _tcard("🧭 Meta 组合 · 影子验证", body, col="col-12")


def _operations_card() -> str:
    from qsim.services.operations import status
    state = status()
    expected = _fmt_date(str(state.get("expected_trade_date") or "—"))
    dates = [day for day in state.get("paper_dates", {}).values() if day]
    settled = _fmt_date(min(dates)) if dates else "—"
    next_check = next((item.get("next_run") for item in state["timers"]
                       if item["unit"] == "qsim-data-reconcile.timer"), None)
    task = state["task"]
    task_name = str(task.get("job") or "")
    task_label = "行情同步" if "sync" in task_name else "模拟盘结算" if "paper" in task_name else "数据更新"
    running = task_label + "进行中" if task.get("running") else "待命"
    problems = "；".join(state["issues"]) if state["issues"] else "行情与结算已达到预期交易日"
    tone = "up" if state["ok"] else "down"
    calendar = state["calendar"]
    warning = (" · 日历即将到期，请更新" if calendar["status"] == "expiring" else "")
    def local_time(value):
        if not value:
            return "未完成"
        try:
            return dt.datetime.fromisoformat(value).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return str(value)
    backup = state.get("backup") or {}
    drill = state.get("restore_drill") or {}
    quality = state.get("quality") or {}
    adjustment = quality.get("details", {}).get("adjustment", {})
    data_note = (f"复权因子 {_fmt_date(str(adjustment.get('latest') or '—'))} · "
                 f"覆盖 {adjustment.get('covered', 0)}/{adjustment.get('required', 0)}" if adjustment else "")
    collector_note = "新系统直接采集" if state.get("native_collector") else "旧任务采集"
    enrichment_failures = state.get("native_market_state", {}).get("enrichment_failed_codes") or []
    enrichment_note = f" · {len(enrichment_failures)} 只股票基本面更新待重试" if enrichment_failures else ""
    quality_warnings = "；".join(quality.get("warnings") or [])
    offsite = state.get("offsite_backup") or {}
    offsite_note = (local_time(offsite.get("finished_at")) if offsite.get("ok") else "待配置备份目标" if not offsite else "传输失败，等待重试")
    body = (
        f'<div class="note">{collector_note} · {_html_safe(data_note + enrichment_note)}</div>'
        f'<div class="note">{_html_safe(quality_warnings)}</div>'
        f'<div class="{tone}"><b>{_html_safe(problems)}</b></div>'
        f'<div class="note">预期交易日 {expected} · 模拟盘结算 {settled} · {_html_safe(running)}</div>'
        f'<div class="note">下次自动检查 {_html_safe(next_check or "尚未读取到定时任务")}</div>'
        f'<div class="note">交易日历有效至 {_html_safe(calendar.get("valid_through") or "—")}{warning}</div>'
        f'<div class="note">完整备份 {_html_safe(local_time(backup.get("finished_at")))}'
        f' · 恢复验证 {_html_safe(local_time(drill.get("finished_at")))}</div>'
        f'<div class="note">异地备份 {_html_safe(offsite_note)}</div>'
    )
    return _tcard("数据与结算状态", body, col="col-12")


def _overview() -> list[str]:
    cards: list[str] = [_operations_card()]
    sh = q.latest_shadow()
    rebaseline = q.latest_rebaseline()
    meta_shadow = q.meta_shadow()
    current_strategies = q.strategy_overview()
    exp = q.experiments()

    # 状态横幅：终端 ticker 带（标签小字 + 等宽值 + 竖分隔线）
    bits = []
    bits.append(f'<span class="q-tick"><span class="k">模式</span>'
                f'<span class="v ok">{STATUS["mode"]}</span></span>')
    bits.append(f'<span class="q-tick"><span class="k">风控</span>'
                f'<span class="v">{STATUS["risk"]}</span></span>')

    active_count = sum(
        1 for row in current_strategies
        if str(row.get("state") or "") not in {"retired", "historical_only"}
    )
    bits.append(f'<span class="q-tick"><span class="k">模拟盘</span>'
                f'<span class="v">{active_count} 策略</span></span>')
    if meta_shadow:
        status = str((meta_shadow.get("latest") or {}).get("risk", {}).get("status") or "预热")
        bits.append(f'<span class="q-tick"><span class="k">META 影子</span>'
                    f'<span class="v warn">{_html_safe(status)}</span></span>')
    if rebaseline:
        targets = "、".join(str(name) for name in rebaseline.get("targets") or [])
        bits.append(f'<span class="q-tick"><span class="k">基线重置</span>'
                    f'<span class="v warn">{targets}</span></span>')
    elif sh:
        cls = "ok" if sh.get("all_ok") else "bad"
        label = "PASS" if sh.get("all_ok") else "FAIL"
        bits.append(f'<span class="q-tick"><span class="k">对账 {sh.get("date", "?")}</span>'
                    f'<span class="v {cls}">{label}</span></span>')
    cards.append(
        '<div class="row g-3 mb-2"><div class="col-12">'
        '<div class="q-ticker">'
        + "".join(bits) + '</div></div></div>')

    # 与 8503 对齐的市场状态卡；明确不迁移三态/两态元账户净值。
    cards.append(_market_state_card())

    # A separately labelled, read-only meta evidence card.  It is intentionally
    # not included in the sum of qsim strategy account assets below.
    cards.append(_meta_shadow_card())

    # 指标卡：模拟盘资产 + 实验 + 对账
    active_rows = [row for row in current_strategies if row.get("state") not in {"retired", "historical_only"}]
    total_assets = sum(float((row.get("performance") or {}).get("settled_value") or 0) for row in active_rows)
    cards.append(
        '<div class="row g-3 mb-2">'
        + _stat_card("独立账户结算资产之和", f"{total_assets:,.0f}", "各自结算日期；非组合净值")
        + _stat_card("正式 / 候选账户", f"{sum(r.get('state') == 'paper_enabled' for r in active_rows)} / {sum(r.get('state') == 'paper_candidate' for r in active_rows)}")
        + _stat_card("实验记录", f"{exp['total']}")
        + _stat_card("对账天数", f"{len(q.shadow_history())}")
        + '</div>')

    # 统一策略现账户表：替代原来的三列速览，加入 8503 的全部业务字段。
    cards.append(_strategy_overview_card())
    cards.append(_performance_card())

    # 配置对比表
    configs = q.configs_summary()
    if configs:
        rows = []
        for c in configs:
            rows.append([c["config"], f"{c['total_return']*100:.1f}%",
                         f"{c['max_drawdown']*100:.1f}%", f"{c['sharpe']:.2f}"])
        cards.append(_tcard("配置对比 · 嵌套 OOS", _data_table(
            ["配置", "总收益", "回撤", "Sharpe"], rows, right=(1, 2, 3)), col="col-12"))
    return cards


# ═══════════════ Tab 2: 模拟盘 ═══════════════

def _paper_positions() -> list[str]:
    """8503-style live strategy view, sourced from the current effective ledger."""
    visuals = q.strategy_visuals()
    if not visuals:
        return [_tcard("实盘视图", '<div class="sub">暂无策略结算数据</div>')]
    colors = ["#26A69A", "#7B8FD4", "#E0A72E", "#C084FC",
              "#F0B90B", "#22c55e", "#ef4444", "#14B8A6"]
    curves = "".join(
        _strategy_curve_card(item, colors[index % len(colors)], index)
        for index, item in enumerate(visuals)
    )
    heatmaps = "".join(
        _strategy_heatmap_card(item, index)
        for index, item in enumerate(visuals)
    )
    switch = """<div class="card live-view-card mb-3">
  <div class="card-body live-view-control">
    <div class="live-view-copy"><span class="live-view-label">实盘视图</span><span class="live-view-hint">热力图：面积为当前持仓市值，颜色为区间涨跌</span></div>
    <div class="live-view-buttons" role="group" aria-label="实盘视图切换">
      <button type="button" id="qsim-view-curves-btn" class="view-toggle is-active" aria-pressed="true" onclick="setQsimStrategyView('curves')">收益率曲线</button>
      <button type="button" id="qsim-view-heatmap-btn" class="view-toggle" aria-pressed="false" onclick="setQsimStrategyView('heatmap')">持仓热力图</button>
    </div>
  </div>
</div>
<div id="qsim-curves-view">%(curves)s</div>
<div id="qsim-heatmap-view" style="display:none;">%(heatmaps)s</div>
<script>
function setQsimStrategyView(mode){
  var curves=document.getElementById("qsim-curves-view"),heatmaps=document.getElementById("qsim-heatmap-view");
  var cb=document.getElementById("qsim-view-curves-btn"),hb=document.getElementById("qsim-view-heatmap-btn");
  var isHeatmap=mode==="heatmap";
  curves.style.display=isHeatmap?"none":"block";heatmaps.style.display=isHeatmap?"block":"none";
  cb.classList.toggle("is-active",!isHeatmap);hb.classList.toggle("is-active",isHeatmap);
  cb.setAttribute("aria-pressed",String(!isHeatmap));hb.setAttribute("aria-pressed",String(isHeatmap));
  try{sessionStorage.setItem("qsim_strategy_view",mode);}catch(e){}
  setTimeout(function(){
    var active=isHeatmap?heatmaps:curves;
    if(isHeatmap&&window.__qsimHeatmapInit)window.__qsimHeatmapInit.forEach(function(fn){fn();});
    active.querySelectorAll(".chart-box,[id^='qsim_heatmap_']").forEach(function(el){
      var chart=typeof echarts!=="undefined"&&echarts.getInstanceByDom(el);if(chart)chart.resize();
    });
  },60);
}
try{if(sessionStorage.getItem("qsim_strategy_view")==="heatmap")setQsimStrategyView("heatmap");}catch(e){}
</script>""" % {"curves": curves, "heatmaps": heatmaps}
    return [_performance_card(), switch]


def _strategy_card_title(item: dict, suffix: str) -> str:
    migration_state = str(item.get("state") or "")
    if migration_state == "retired":
        state = "已退休"
    elif migration_state == "historical_only":
        state = "仅历史"
    elif migration_state == "paper_candidate":
        state = "候选 · 未提升"
    else:
        state = "正式" if item.get("ledger") == "official" else "待验证"
    cls = "tag-g" if state == "正式" else "tag-y"
    return (f'<span>{_html_safe(item.get("strategy"))} · {suffix}</span>'
            f'<span class="tag {cls}">{state}</span>')


def _strategy_curve_card(item: dict, color: str, index: int) -> str:
    import json

    snapshots = item.get("snapshots") or []
    if not snapshots:
        return _tcard(_strategy_card_title(item, "实盘净值"),
                      '<div class="sub">暂无结算快照</div>', pad=False, col="col-12")
    uid = f"qsim_curve_{index}"
    data = json.dumps(snapshots, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    metrics = item.get("metrics") or {}
    latest = snapshots[-1]
    ret = float(metrics.get("total_return") or 0) * 100
    ret_color = ACCENT if ret >= 0 else DANGER
    ledger_note = "qsim 日结算账本（含回填）" if item.get("ledger") == "official" else "provisional 回填账本 · 待迁移验证"
    performance = item.get("performance") or {}
    ledger_note += f' · {_fmt_date(str(performance.get("first_date") or snapshots[0]["date"]))} → {_fmt_date(str(performance.get("last_date") or latest["date"]))}'
    if performance.get("warnings"):
        ledger_note += ' · ' + '；'.join(performance["warnings"])
    benchmark = {str(x["date"]): x["total_value"] for x in performance.get("benchmark_curve") or []}
    benchmark_data = json.dumps(benchmark, separators=(",", ":"))
    body = """<div class="heatmap-range-buttons" id="%(uid)s_ranges"></div>
<div id="%(uid)s" class="chart-box" style="height:360px;"></div>
<div class="curve-metrics"><span>结算净值 <b>%(nav)s</b></span><span>收益 <b style="color:%(ret_color)s">%(ret)s</b></span><span>样本 <b>%(days)d</b> 个结算点</span><span>回撤 <b style="color:%(danger)s">%(mdd)s</b></span><span>夏普 <b>%(sharpe)s</b></span><span>基准 %(benchmark_label)s · %(benchmark_return)s</span><span>超额 %(excess)s</span></div>
<div class="note" style="padding:0 14px 14px;">%(ledger)s · 指标按完整账本区间计算，范围按钮仅缩放曲线</div>
<script>
(function(){
 var el=document.getElementById("%(uid)s"),ranges=document.getElementById("%(uid)s_ranges");
 if(!el||!ranges||typeof echarts==="undefined")return;
 var data=%(data)s,benchmark=%(benchmark_data)s,selected="all",buttons={};
 function digits(row){return String((row&&row.date)||"").replace(/\\D/g,"");}
 function label(row){var d=digits(row);return d.length>=12?d.slice(4,6)+"-"+d.slice(6,8)+" "+d.slice(8,10)+":"+d.slice(10,12):d.slice(4,6)+"-"+d.slice(6,8);}
 function choose(range){
   selected=range;Object.keys(buttons).forEach(function(key){buttons[key].classList.toggle("is-active",key===range);});
   var days=[];data.forEach(function(row){var day=digits(row).slice(0,8);if(day&&days.indexOf(day)<0)days.push(day);});
   var size=range==="today"?1:(range==="3d"?3:(range==="1m"?22:days.length));
   var shownDays=days.slice(Math.max(0,days.length-size));
   var rows=data.filter(function(row){return shownDays.indexOf(digits(row).slice(0,8))>=0;});
   var chart=echarts.getInstanceByDom(el)||echarts.init(el);
   chart.setOption({animation:false,backgroundColor:"transparent",
     tooltip:{trigger:"axis",backgroundColor:"#171B22",borderColor:"#232830",textStyle:{color:"#EBEDEC"},formatter:function(params){
       var point=rows[params[0].dataIndex]||{},pct=(Number(point.total_value||0)/%(initial)f-1)*100;
       var base=benchmark[point.date],bp=base==null?null:(Number(base)/%(initial)f-1)*100;
       return label(point)+"<br>已结算收益：<b>"+(pct>=0?"+":"")+pct.toFixed(2)+"%%</b>"+(bp==null?"":"<br>同期间基准："+bp.toFixed(2)+"%% · 超额："+(pct-bp).toFixed(2)+" 个百分点")+"<br>净值："+Number(point.total_value||0).toLocaleString("zh-CN",{maximumFractionDigits:0})+"<br>现金："+Number(point.cash||0).toLocaleString("zh-CN",{maximumFractionDigits:0})+" · 持仓："+Number(point.position_value||0).toLocaleString("zh-CN",{maximumFractionDigits:0});
     }},
     legend:{textStyle:{color:"#8A919C"}},grid:{left:54,right:20,top:36,bottom:36},
     xAxis:{type:"category",data:rows.map(label),axisLine:{lineStyle:{color:"#232830"}},axisLabel:{color:"#8A919C",fontSize:10,rotate:rows.length>30?35:0}},
     yAxis:{type:"value",scale:true,axisLabel:{color:"#8A919C",fontSize:10,formatter:function(v){return v.toFixed(1)+"%%";}},splitLine:{lineStyle:{color:"rgba(42,50,67,.4)"}},name:"累计收益",nameTextStyle:{color:"#8A919C",fontSize:10}},
     series:[{name:"策略（已结算）",type:"line",smooth:false,symbol:"none",lineStyle:{width:2,color:"%(color)s"},areaStyle:{color:"%(color)s",opacity:.10},itemStyle:{color:"%(color)s"},data:rows.map(function(x){return (Number(x.total_value||0)/%(initial)f-1)*100;})},{name:"同期间基准",type:"line",symbol:"none",connectNulls:false,lineStyle:{width:1,type:"dashed",color:"#8A919C"},data:rows.map(function(x){var b=benchmark[x.date];return b==null?null:(Number(b)/%(initial)f-1)*100;})}]
   },true);chart.resize();
 }
 [["today","今日"],["3d","3日"],["1m","1月"],["all","全部"]].forEach(function(item){
   var b=document.createElement("button");b.type="button";b.className="chart-range-btn";b.textContent=item[1];b.onclick=function(){choose(item[0]);};ranges.appendChild(b);buttons[item[0]]=b;
 });
 choose(selected);
 window.addEventListener("resize",function(){var chart=echarts.getInstanceByDom(el);if(chart)chart.resize();});
})();
</script>""" % {
        "uid": uid, "data": data, "initial": float(item.get("initial_capital") or 1),
        "nav": f"{float(latest.get('total_value') or 0):,.0f}",
        "ret": f"{ret:+.2f}%", "ret_color": ret_color, "days": len(snapshots),
        "mdd": f"{float(metrics.get('max_drawdown') or 0) * 100:.1f}%",
        "sharpe": f"{float(metrics['sharpe']):.2f}" if metrics.get('sharpe') is not None and len(snapshots) >= 20 else "—（短样本/无波动）",
        "danger": DANGER, "ledger": _html_safe(ledger_note), "color": color,
        "benchmark_data": benchmark_data,
        "benchmark_label": _html_safe(performance.get("benchmark_name") or "—"),
        "benchmark_return": _pct(performance.get("benchmark_return")),
        "excess": _pct(performance.get("excess_return")),
    }
    return _tcard(_strategy_card_title(item, "已结算净值"), body, pad=False, col="col-12")


def _strategy_heatmap_card(item: dict, index: int) -> str:
    import json

    cells = item.get("holdings") or []
    if not cells:
        return _tcard(_strategy_card_title(item, "持仓热力图"),
                      '<div class="sub" style="padding:22px 14px;">当前无持仓</div>',
                      pad=False, col="col-12")
    for cell in cells:
        cell["name"] = _stock_name(str(cell.get("code") or ""))
    uid = f"qsim_heatmap_{index}"
    data = json.dumps(cells, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    body = """<div id="%(uid)s_note" class="note" style="padding:12px 14px 8px;">面积 = 当前持仓市值 · 颜色 = 今日涨跌幅 · 共 %(count)d 个标的</div>
<div id="%(uid)s_ranges" class="heatmap-range-buttons"></div>
<div id="%(uid)s" style="height:330px;width:100%%;"></div>
<script>
(function(){
 var el=document.getElementById("%(uid)s"),note=document.getElementById("%(uid)s_note"),ranges=document.getElementById("%(uid)s_ranges");
 if(!el||!ranges||typeof echarts==="undefined")return;
 var data=%(data)s,period="today",buttons={};
 function color(value){var mag=Math.min(Math.abs(value||0)/.06,1),target=value>=0?[46,158,123]:[224,93,93],base=[47,47,47];return "rgb("+base.map(function(v,i){return Math.round(v+(target[i]-v)*mag);}).join(",")+")";}
 function money(v){return Number(v||0).toLocaleString("zh-CN",{maximumFractionDigits:0});}
 function draw(){
   if(el.clientWidth<=0)return;
   var shown=data.map(function(raw){var p=(raw.periods&&raw.periods[period])||(raw.periods&&raw.periods.today)||{};return Object.assign({},raw,{period_info:p,period_pct:Number(p.pct||0),period_pnl:Number(p.pnl||0),value:Number(raw.market_value||0),itemStyle:{color:color(Number(p.pct||0))}});});
   var chart=echarts.getInstanceByDom(el)||echarts.init(el);
   chart.setOption({animation:false,tooltip:{formatter:function(point){var d=point.data||{},p=d.period_info||{};return "<b>"+d.name+"</b> · "+d.code+"<br>"+(p.label||"今日")+"涨跌：<b>"+(d.period_pct>=0?"+":"")+(d.period_pct*100).toFixed(2)+"%%</b><br>"+(p.label||"今日")+"盈亏：<b>"+(d.period_pnl>=0?"+":"")+money(d.period_pnl)+" 元</b><br>现价："+Number(d.price||0).toFixed(3)+" · 持仓："+money(d.shares)+" 股<br>持仓市值："+money(d.market_value)+" 元";}},series:[{type:"treemap",data:shown,roam:false,nodeClick:false,breadcrumb:{show:false},label:{show:true,formatter:function(point){var d=point.data||{};return d.code+"\\n"+(d.period_pct>=0?"+":"")+(d.period_pct*100).toFixed(2)+"%%";}},itemStyle:{borderColor:"#0B0E11",borderWidth:3,gapWidth:3},levels:[{itemStyle:{borderColor:"#0B0E11",borderWidth:3,gapWidth:3}}]}]},true);chart.resize();
 }
 function setPeriod(next){period=next;Object.keys(buttons).forEach(function(key){buttons[key].classList.toggle("is-active",key===period);});var p=(data[0].periods||{})[period]||{};if(note)note.textContent="面积 = 当前持仓市值 · 颜色 = "+(p.label||"今日")+"涨跌幅 · 共 %(count)d 个标的";draw();}
 [["today","今日"],["3d","3日"],["1m","1月"],["all","全部"]].forEach(function(item){var b=document.createElement("button");b.type="button";b.className="chart-range-btn";b.textContent=item[1];b.onclick=function(){setPeriod(item[0]);};ranges.appendChild(b);buttons[item[0]]=b;});
 window.__qsimHeatmapInit=window.__qsimHeatmapInit||[];window.__qsimHeatmapInit.push(draw);setPeriod("today");
 window.addEventListener("resize",function(){var chart=echarts.getInstanceByDom(el);if(chart)chart.resize();});
})();
</script>""" % {"uid": uid, "data": data, "count": len(cells)}
    return _tcard(_strategy_card_title(item, "持仓热力图"), body, pad=False, col="col-12")


def _last_price_of(strategy: str, symbol: str) -> float | None:
    """策略持仓标的最新价：ETF 查 bars，股票查 stock_bars。"""
    import sqlite3
    code6 = str(symbol)[:6]
    try:
        conn = sqlite3.connect(f"file:{q.ROOT / 'data' / 'quant.db'}?mode=ro", uri=True)
        # 先试 ETF bars（6位代码）
        row = conn.execute(
            "SELECT close FROM bars WHERE symbol=? ORDER BY date DESC LIMIT 1",
            (code6,),
        ).fetchone()
        if not row:
            # 股票 stock_bars（ts_code 如 300628.SZ）
            row = conn.execute(
                "SELECT close FROM stock_bars WHERE ts_code LIKE ? ORDER BY date DESC LIMIT 1",
                (f"{code6}%",),
            ).fetchone()
        conn.close()
        return float(row[0]) if row else None
    except Exception:
        return None


def _paper_curves_chart(curves: list[dict]) -> str:
    """多策略净值曲线（ECharts 叠加）。"""
    import json
    series = []
    colors = ["#26A69A", "#7B8FD4", "#E0A72E", "#EF5350", "#F0B90B",
              "#22c55e", "#ef4444", "#8B5CF6"]
    for i, c in enumerate(curves):
        series.append({
            "name": c["name"], "type": "line", "smooth": True, "symbol": "none",
            "lineStyle": {"width": 2, "color": colors[i % len(colors)]},
            "data": [[p["date"], p["total_value"]] for p in c["data"]],
        })
    return f"""<div id="paper_curves" class="chart-box" style="height:300px;"></div>
<script>
(function(){{
  var el=document.getElementById("paper_curves");
  if(!el||typeof echarts==="undefined")return;
  var chart=echarts.init(el);
  chart.setOption({{
    backgroundColor:"transparent",
    tooltip:{{trigger:"axis",backgroundColor:"#171B22",borderColor:"#232830",textStyle:{{color:"#EBEDEC"}}}},
    legend:{{data:{json.dumps([c["name"] for c in curves], ensure_ascii=False)},textStyle:{{color:"#8A919C"}},top:0}},
    grid:{{left:60,right:16,top:32,bottom:28}},
    xAxis:{{type:"category",data:{json.dumps([p["date"] for p in curves[0]["data"]], ensure_ascii=False)},axisLine:{{lineStyle:{{color:"#232830"}}}},axisLabel:{{color:"#8A919C",fontSize:10}}}},
    yAxis:{{type:"value",scale:true,splitLine:{{lineStyle:{{color:"rgba(42,50,67,0.4)"}}}},axisLabel:{{color:"#8A919C",fontSize:10}}}},
    series:{json.dumps(series, ensure_ascii=False)}
  }});
  window.addEventListener("resize",function(){{chart.resize();}});
}})();
</script>"""


# ═══════════════ Tab 3: 实验 ═══════════════

def _backtest_curve_card(item: dict, index: int) -> str:
    """8503-style frozen equity card, sourced from an experiment CSV."""
    import json

    data_rows = item.get("data") or []
    if not data_rows:
        return ""
    uid = f"qsim_backtest_{index}"
    data = json.dumps(data_rows, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    metrics = item.get("metrics") or {}
    sample = item.get("sample") or {}
    contract = item.get("contract") or {}
    initial = float(metrics.get("initial") or data_rows[0].get("total_value") or 100000)
    total_return = float(metrics.get("total_return") or 0)
    full_window = bool(sample.get("is_full_window", True))
    sample_note = (
        "完整请求窗口。"
        if full_window else
        "<b style=\"color:#E0A72E\">⚠ 短样本："
        f"{_html_safe(str(sample.get('actual_start') or metrics.get('window_start') or '—'))} 起；"
        "不应与完整十年策略直接横向比较。</b>"
    )
    body = """<div id="%(uid)s_ranges" class="heatmap-range-buttons"></div>
<div id="%(uid)s" class="chart-box" style="height:360px;"></div>
<div class="curve-metrics"><span>年化 <b>%(annual)s</b></span><span>回撤 <b style="color:%(danger)s">%(mdd)s</b></span><span>夏普 <b>%(sharpe)s</b></span><span>超额 <b class="%(excess_cls)s">%(excess)s</b></span><span>IR <b>%(ir)s</b></span><span>Calmar <b>%(calmar)s</b></span><span>月胜率 <b>%(win_rate)s</b></span><span>成本 <b>%(cost)s</b></span><span>切换 <b>%(trades)d 次</b></span></div>
<div class="note" style="padding:0 14px 14px;">全区间累计 <b class="%(ret_cls)s">%(total)s</b> · %(start)s 至 %(end)s · 历史回测 · 非模拟盘；时点限制见上方筛选审查<br>%(sample_note)s</div>
<script>
(function(){
 var el=document.getElementById("%(uid)s"),ranges=document.getElementById("%(uid)s_ranges");
 if(!el||!ranges||typeof echarts==="undefined")return;
 var data=%(data)s,buttons={};
 function choose(range){
   Object.keys(buttons).forEach(function(key){buttons[key].classList.toggle("is-active",key===range);});
   var size=range==="1y"?252:(range==="3y"?756:data.length),rows=data.slice(Math.max(0,data.length-size));
   var chart=echarts.getInstanceByDom(el)||echarts.init(el);
   chart.setOption({animation:false,backgroundColor:"transparent",
     tooltip:{trigger:"axis",backgroundColor:"#171B22",borderColor:"#232830",textStyle:{color:"#EBEDEC"},formatter:function(params){
       var index=(params&&params[0]&&params[0].dataIndex),point=(typeof index==="number"&&rows[index])||{},pct=(Number(point.total_value||0)/%(initial)f-1)*100;
       return String(point.date||"")+"<br>累计收益：<b>"+(pct>=0?"+":"")+pct.toFixed(2)+"%%</b><br>净值："+Number(point.total_value||0).toLocaleString("zh-CN",{maximumFractionDigits:0});
     }},
     grid:{left:54,right:20,top:24,bottom:42},
     xAxis:{type:"category",data:rows.map(function(x){return String(x.date||"").slice(0,4)+"-"+String(x.date||"").slice(4,6);}),axisLine:{lineStyle:{color:"#232830"}},axisLabel:{color:"#8A919C",fontSize:10,rotate:35}},
     yAxis:{type:"value",scale:true,axisLabel:{color:"#8A919C",fontSize:10,formatter:function(v){return v.toFixed(0)+"%%";}},splitLine:{lineStyle:{color:"rgba(42,50,67,.4)"}},name:"累计收益",nameTextStyle:{color:"#8A919C",fontSize:10}},
     series:[{type:"line",smooth:true,symbol:"none",lineStyle:{width:2,color:"%(color)s"},areaStyle:{color:"%(color)s",opacity:.10},itemStyle:{color:"%(color)s"},data:rows.map(function(x){return (Number(x.total_value||0)/%(initial)f-1)*100;})}]
   },true);chart.resize();
 }
 [["1y","近1年"],["3y","近3年"],["all","全部"]].forEach(function(item){var b=document.createElement("button");b.type="button";b.className="chart-range-btn";b.textContent=item[1];b.onclick=function(){choose(item[0]);};ranges.appendChild(b);buttons[item[0]]=b;});
 choose("all");
 window.addEventListener("resize",function(){var chart=echarts.getInstanceByDom(el);if(chart)chart.resize();});
})();
</script>""" % {
        "uid": uid, "data": data, "initial": initial,
        "annual": f"{float(metrics.get('annual_return') or 0) * 100:.1f}%",
        "mdd": f"{float(metrics.get('max_drawdown') or 0) * 100:.1f}%",
        "sharpe": f"{float(metrics.get('sharpe') or 0):.2f}",
        "excess": (f"{float(metrics['excess_total_return']) * 100:+.1f}%"
                   if metrics.get("excess_total_return") is not None else "—"),
        "excess_cls": ("up" if float(metrics.get("excess_total_return") or 0) >= 0
                       else "down") if metrics.get("excess_total_return") is not None else "flat",
        "ir": (f"{float(metrics['information_ratio']):.2f}"
               if metrics.get("information_ratio") is not None else "—"),
        "calmar": (f"{float(metrics['calmar']):.2f}"
                   if metrics.get("calmar") is not None else "—"),
        "win_rate": (f"{float(metrics['monthly_win_rate']) * 100:.0f}%"
                     if metrics.get("monthly_win_rate") is not None else "—"),
        "cost": f"{float(metrics.get('cost_drag') or 0) * 100:.1f}%",
        "trades": int(metrics.get("num_trades") or 0),
        "total": f"{total_return * 100:+.1f}%",
        "ret_cls": "up" if total_return >= 0 else "down",
        "start": _fmt_date(str(metrics.get("window_start") or contract.get("run_start") or data_rows[0].get("date") or "—")),
        "end": _fmt_date(str(metrics.get("window_end") or contract.get("data_cutoff") or data_rows[-1].get("date") or "—")),
        "color": ["#26A69A", "#7B8FD4", "#E0A72E", "#F0B90B"][index % 4],
        "danger": DANGER, "sample_note": sample_note,
    }
    title = (f'<span>{_html_safe(item.get("name") or item.get("key"))} · 回测净值</span>'
             + ('<span class="tag tag-y">回测</span>' if full_window else
                '<span class="tag tag-y">短样本</span>'))
    return _tcard(title, body, col="col-12", pad=False)

def _experiments() -> list[str]:
    cards: list[str] = []
    exp = q.experiments()
    configs = q.configs_summary()
    cards.append(_strategy_screening_card())
    cards.append(_trend_fallback_research_card())

    # 近期嵌套 OOS 汇总与十年策略曲线是不同证据窗口，分开标识。
    if configs:
        rows = []
        for c in configs:
            rows.append([f'<b>{c["config"]}</b>',
                         f"{c['total_return']*100:.1f}%",
                         f"{c['max_drawdown']*100:.1f}%",
                         f"{c['sharpe']:.2f}",
                         f"{c['num_days']}"])
        cards.append(_tcard("🔬 配置对比 · 嵌套 OOS（近期）", _data_table(
            ["配置", "总收益", "回撤", "Sharpe", "天数"], rows, right=(1, 2, 3, 4)),
            col="col-12"))

    # 主区：迁移策略 + multi_factor v3。使用独立冻结 equity CSV。
    backtests = q.backtest_curves()
    if backtests:
        cards.append(_tcard(
            f"🔬 回测净值 · {len(backtests)} 策略",
            '<div class="note" style="padding:0 14px 12px;">每个策略独立回测；这些是旧历史回测展示，未完成上方冻结筛选的成本/数据质量门槛；因子策略月度信号后下一开盘执行。超额/IR/月胜率均为相对基准沪深300ETF（510300）。黄色“短样本”表示该策略没有覆盖完整请求窗口，不能与完整十年策略直接横比。</div>',
            col="col-12",
        ))
        cards.extend(_backtest_curve_card(item, index) for index, item in enumerate(backtests))

    # 次区：保留市场门禁四种组合实验，作为实验对照而非策略主列表。
    gate_curves = q.market_gate_curves()
    if gate_curves:
        cards.append(_tcard(
            "🔬 实验对照 · 趋势 / 宽度 / 波动率门禁",
            '<div class="note" style="padding:0 14px 12px;">这是 multi_factor v3 的机制消融与基线对照，不属于 9 个策略主列表。</div>',
            col="col-12",
        ))
        cards.extend(_backtest_curve_card(item, index + len(backtests)) for index, item in enumerate(gate_curves))
    return cards


def _strategy_screening_card() -> str:
    payload = q.strategy_screening()
    if not payload:
        return _tcard("策略筛选 · 年度前推与成本压力", '<div class="sub">尚无完成的冻结检验</div>', col="col-12")
    contract = payload.get("contract") or {}
    rows, details = [], []
    labels = {i["key"]: i["label"] for i in payload.get("strategies") or []}
    for item in payload.get("strategies") or []:
        annual = {i["year"]: i for i in item.get("annual") or []}
        stress = item.get("stress") or {}
        decision = item.get("decision") or {}
        year_cells = [_pct((annual.get(year, {}).get("metrics") or {}).get("total_return")) for year in range(2022, 2026)]
        partial = (annual.get(2026, {}).get("metrics") or {}).get("total_return")
        color = "tag-g" if decision.get("state") == "observe" else "tag-y" if decision.get("state") in ("data_blocked", "insufficient") else "tag-r"
        rows.append([_html_safe(item.get("label")),
            f'<span class="tag {color}">{_html_safe(decision.get("label"))}</span>', *year_cells,
            '<br>'.join(f'{m}倍 {_pct(stress.get(str(m), {}).get("total_return"))}' for m in (1, 2, 4)) if stress else "—",
            _pct(stress.get("1", {}).get("max_drawdown")), _pct(partial)])
        fold_rows = []
        for fold in item.get("annual") or []:
            m = fold["metrics"]
            fold_rows.append([str(fold["year"]) + (" · 补充" if not fold["decision"] else ""),
                _pct(m.get("total_return")), _pct(m.get("max_drawdown")), _pct(m.get("benchmark_return")),
                _pct(m.get("excess_return")), str(m.get("num_trades", 0)),
                f'{float(m.get("fees") or 0):.2f}', _pct(m.get("mean_invested")),
                str(m.get("skipped_orders", 0))])
        reasons = '<br>'.join(_html_safe(n) for n in decision.get("reasons") or [])
        notes = '<br>'.join(_html_safe(n) for n in item.get("notes") or [])
        stress_base = stress.get("1", {})
        cost_rows = [[f"{n}倍", _pct(stress.get(str(n), {}).get("total_return")),
                      _pct(stress.get(str(n), {}).get("max_drawdown")),
                      _pct(stress.get(str(n), {}).get("excess_return"))] for n in (1, 2, 4)] if stress else []
        cost_table = _data_table(["成本档", "四年拼接收益", "回撤", "超额"], cost_rows, right=(1, 2, 3)) if cost_rows else ""
        details.append('<details style="padding:10px 14px;border-top:1px solid var(--q-line)">' +
            f'<summary style="cursor:pointer">{_html_safe(item.get("label"))} · 依据与年度明细</summary>' +
            f'<div class="note" style="padding:8px 0">{reasons}<br>{notes}<br>' +
            f'基准 {item.get("benchmark", "—")} · 四年拼接基准收益 {_pct(stress_base.get("benchmark_return"))} · '
            f'超额 {_pct(stress_base.get("excess_return"))} · 基准回撤 {_pct(stress_base.get("benchmark_drawdown"))}</div>' +
            (_data_table(["年度", "收益", "回撤", "基准收益", "超额", "成交数", "手续费元", "平均仓位", "跳过数"],
                         fold_rows, right=tuple(range(1, 9))) if fold_rows else "") + cost_table + '</details>')
    pairs = payload.get("redundancy") or []
    redundancy = ('<div class="note" style="padding:12px 14px">未发现达到0.85阈值的高相关配对。</div>' if not pairs else
        '<details style="padding:12px 14px"><summary style="cursor:pointer">高相关策略 · 重复配置提示</summary>' +
        _data_table(["策略A", "策略B", "日收益相关", "共同天数"],
            [[_html_safe(labels.get(i["first"], i["first"])), _html_safe(labels.get(i["second"], i["second"])),
              f'{i["correlation"]:.2f}', str(i["days"])] for i in pairs], right=(2, 3)) +
        '<div class="note">按2022—2025共同日期日收益计算，至少500个共同观测；高相关提示重复敞口，不自动淘汰。</div></details>')
    note = (
        '<div class="note" style="padding:12px 14px">'
        f'规则冻结 {_html_safe(str(contract.get("frozen_at") or "")[:19])} UTC · 行情截至 {_fmt_date(str(contract.get("source_end") or ""))}<br>'
        f'{_html_safe(contract.get("history_note"))}<br>'
        '预设历史门槛：四个完整年度中至少三个盈利、至少20笔成交、拼接回撤不超过25%；1/2/4倍成本收益均为正；'
        '并且正超额或回撤较参照改善至少30%。数据或成交覆盖不足优先阻断。门槛属于研究约定，不触发自动调仓。<br>'
        '每年独立以10万元启动，保留指标历史预热；收盘信号后下一开盘执行。四年拼接是年度重启的假想净值，未承接年末持仓。'
        '压力测试同步放大佣金、最低佣金、滑点，印花税与过户费保持固定。<br>'
        '基准按同窗价格计算，以前一交易日收盘为初值，不扣交易成本；超额为策略收益减基准收益，单位是百分点。<br>'
        f'{_html_safe(contract.get("price_note"))}<br>' +
        '<br>'.join(_html_safe(n) for n in payload.get("notes") or []) +
        '<br><b>前瞻状态：本报告仅为已查看历史的冻结检验。冻结后新数据尚未形成前瞻验证结果，不能称为真实样本外通过。</b>'
        '<br>筛选结论独立于在线策略状态；未自动晋升、退休或调整正式资金。'
        f'<br>批次 {_html_safe(payload.get("run_id"))} · 合约 {_html_safe(str(payload.get("contract_sha256") or "")[:12])} · '
        f'输入 {_html_safe(str(contract.get("input_sha256") or "")[:12])}</div>'
    )
    return _tcard("策略重新筛选 · 年度前推 / 成本压力", _data_table(
        ["策略", "筛选结论", "2022", "2023", "2024", "2025", "四年拼接收益", "拼接回撤", "2026补充"], rows,
        right=tuple(range(2, 9))) + note + redundancy + ''.join(details), col="col-12", pad=False)


def _trend_fallback_research_card() -> str:
    payload = q.trend_fallback_research()
    if not payload:
        return _tcard("趋势回退 · 独立规则验证", '<div class="sub">尚未完成实验</div>', col="col-12")
    rows = []
    for item in payload.get("variants") or []:
        metrics = item.get("metrics") or {}
        full = metrics.get("full") or {}
        holdout = metrics.get("historical_holdout") or {}
        recent = metrics.get("recent") or {}
        stress = {int(s["cost_multiplier"]): s for s in item.get("stress") or []}
        rows.append([
            _html_safe(item.get("label")),
            f'{_pct(full.get("total_return"))}<br>年化 {_pct(full.get("annual_return"))}',
            _pct(full.get("max_drawdown")), _pct(full.get("mean_invested")),
            f'{int(full.get("num_trades") or 0)} / {float(full.get("fees") or 0):.2f}',
            f'{_pct(holdout.get("total_return"))}<br>回撤 {_pct(holdout.get("max_drawdown"))}',
            f'2倍 {_pct(stress.get(2, {}).get("total_return"))}<br>4倍 {_pct(stress.get(4, {}).get("total_return"))}',
            f'{_pct(recent.get("total_return"))}<br>回撤 {_pct(recent.get("max_drawdown"))}',
        ])
    contract = payload.get("contract") or {}
    first = ((payload.get("variants") or [{}])[0].get("metrics") or {}).get("full") or {}
    cold = (payload.get("cold_start") or {}).get("metrics") or {}
    note = (
        f'<div class="note" style="padding:12px 14px;">共同窗口 {_fmt_date(str(first.get("start") or "—"))} → {_fmt_date(str(first.get("end") or "—"))}；'
        '近期窗口从2026-08-03开始。每个窗口独立以10万元启动，未承接前段持仓。<br>'
        f'冷启动原执行器近期收益 {_pct(cold.get("total_return"))}。'
        f'<span class="down">{_html_safe(contract.get("warmup_issue"))}</span><br>'
        f'{_html_safe(contract.get("execution"))}<br>'
        f'{_html_safe(contract.get("holdout_note"))}<br>'
        f'{_html_safe(contract.get("price_note"))}<br>'
        '成本压力列为2023年起历史窗口的收益，佣金与滑点同时放大；现金收益按0计算。'
        '波动率目标10%、MA60、波动率窗口20日、调仓带5个百分点均固定，未按结果选参数。'
        f'<br>独立研究 · 未自动替换正式策略 · 批次 {_html_safe(payload.get("run_id"))} · 输入 {_html_safe(str(contract.get("input_sha256") or "")[:12])}</div>'
    )
    return _tcard("趋势回退 · 原规则与三种改进对照", _data_table(
        ["规则", "全窗口收益", "最大回撤", "平均仓位", "成交 / 手续费", "2023年起历史对照", "成本压力收益", "近期表现"],
        rows, right=(1, 2, 3, 4, 5, 6, 7)) + note, col="col-12", pad=False)


# ═══════════════ Tab 4: 影子对账 ═══════════════

def _shadow() -> list[str]:
    cards: list[str] = []
    sh = q.latest_shadow()
    if not sh:
        cards.append(_tcard("🛡️ 影子对账", '<div class="sub">暂无对账数据（每日 16:30 自动运行）</div>'))
        return cards
    grid = []
    for r in sh.get("results", []):
        name = r.get("name", "?")
        ok = r.get("metric_ok", False) and r.get("equity_ok", False)
        pill = ('<span style="background:rgba(34,197,94,.12);color:#22c55e;padding:2px 9px;border-radius:20px;font-size:11px;">✅ 一致</span>'
                if ok else '<span style="background:rgba(239,68,68,.12);color:#ef4444;padding:2px 9px;border-radius:20px;font-size:11px;">❌ 差异</span>')
        diff_sum = sum(abs(v) for v in r.get("metric_diffs", {}).values())
        grid.append(
            f'<div style="background:var(--q-card);border:1px solid var(--q-line);border-radius:8px;padding:12px;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
            f'<b style="font-size:13px;">{name}</b>{pill}</div>'
            f'<div style="font-size:11.5px;color:var(--q-dim);">diff {diff_sum:.4f}</div>'
            f'</div>')
    cards.append('<div class="row g-3 mb-2">' +
                 "".join(f'<div class="col-6 col-xl-3">{g}</div>' for g in grid) + '</div>')
    rows = []
    for r in sh.get("results", []):
        rows.append([r.get("name", "?"), "✅" if r.get("metric_ok") else "❌",
                     "✅" if r.get("equity_ok") else "❌", str(r.get("metric_diffs", {}))])
    cards.append(_tcard(f"🛡️ 对账明细 · {sh.get('date', '?')}", _data_table(
        ["策略", "指标", "净值", "差异"], rows), col="col-12"))
    return cards


# ═══════════════ Tab 5: 系统 ═══════════════

def _system() -> list[str]:
    cards: list[str] = []
    # 策略池
    try:
        from qsim.strategies.pool import ACTIVE_POOL, StrategyFamily
        fam_rows = []
        for fam in StrategyFamily:
            names = [n for n, f in ACTIVE_POOL.items() if f == fam]
            fam_rows.append([fam.value, "、".join(names)])
        cards.append(_tcard("⚙️ 策略池", _data_table(["家族", "策略"], fam_rows), col="col-12"))
    except Exception as e:
        cards.append(_tcard("策略池", f'<div class="sub">{e}</div>'))
    # 热区快照
    try:
        snap = q.hot_zone_snapshot()
        if snap:
            rows = [[k, v[:12]] for k, v in snap.items()]
            cards.append(_tcard("🔥 热区策略", _data_table(["文件", "哈希"], rows), col="col-12"))
        else:
            cards.append(_tcard("🔥 热区策略", '<div class="sub">无热区策略（全部用内置）</div>'))
    except Exception:
        pass
    from qsim.services.operations import status
    state = status()
    labels = {"qsim-cb-etl.timer": "可转债更新", "qsim-data-reconcile.timer": "漏跑检查",
              "qsim-backup.timer": "完整备份", "qsim-watchdog.timer": "服务健康检查",
              "qsim-recovery-drill.timer": "恢复验证"}
    rows = [[_html_safe(labels.get(item["unit"], item["unit"])), "运行" if item["active"] else "未运行",
             _html_safe(item.get("next_run") or "—")] for item in state["timers"]]
    cards.append(_tcard("自动任务", _data_table(["任务", "状态", "下次执行"], rows), col="col-12"))
    runs = [[_html_safe(item["job_id"]), _html_safe(item["status"]),
             _html_safe(item.get("finished_at") or "—"), _html_safe(item.get("error") or "—")]
            for item in state["scheduler_runs"]]
    cards.append(_tcard("最近任务结果", _data_table(["任务", "结果", "完成时间", "失败原因"], runs), col="col-12"))

    return cards


# ═══════════════ AI 复盘 + 对话（移植 legacy mobile_server AI Tab） ═══════════════

def _ai_review() -> list[str]:
    import html as _html
    from qsim.services.web import md
    report_card = ""
    try:
        row = q.ai_report()
        if row:
            rep = row["report"]

            def _li(items, label, color):
                return "".join(
                    f'<div class="qrow" style="align-items:flex-start;">'
                    f'<span class="k" style="flex:0 0 auto;min-width:44px;text-align:center;'
                    f'padding:1px 8px;border-radius:6px;font-size:12px;font-weight:600;'
                    f'color:{color};background:{color}1A;">{label}</span>'
                    f'<span style="flex:1">{md.to_html(x)}</span></div>'
                    for x in (items or [])[:5])
            fc = rep.get("flow_check", {})
            fc_row = ""
            if isinstance(fc, dict) and fc:
                ok = bool(fc.get("ok", True))
                failed = "；".join(str(x) for x in (fc.get("failed_rules") or []))
                warns = "；".join(str(x) for x in (fc.get("warn_rules") or []))
                detail_parts = []
                if failed:
                    detail_parts.append("❌ " + failed)
                if warns:
                    detail_parts.append("⚠️ " + warns)
                fc_row = ('<div class="qrow"><span class="k">'
                          + ('✅' if ok else '❌')
                          + '</span><span style="flex:1">流程核验: '
                          + _html.escape(str(fc.get("verdict", "")))
                          + ((' · ' + _html.escape("；".join(detail_parts))) if detail_parts else '')
                          + '</span></div>')

            def _memo(title, memo, questions_key="review_questions"):
                if not isinstance(memo, dict):
                    return ""
                summary = memo.get("summary")
                if not isinstance(summary, str) or not summary.strip():
                    return ""
                caveats = memo.get("caveats") or memo.get(questions_key) or []
                if not isinstance(caveats, list):
                    caveats = []
                return (f'<div class="note" style="margin-top:8px;"><b>{_html.escape(title)}</b><br>'
                        f'{md.to_html(summary)}'
                        + ("".join(f'<div>· {_html.escape(str(x))}</div>'
                                   for x in caveats[:3]) if caveats else "")
                        + '</div>')
            market_memo = _memo("市场解读", rep.get("market_review"), "caveats")
            system_memo = _memo("系统运行复核", rep.get("system_review"))
            portfolio_memo = _memo("组合与风险", rep.get("portfolio_review"))
            strategy_memos = ""
            for memo in rep.get("strategy_reviews") or []:
                if isinstance(memo, dict):
                    strategy_memos += _memo(str(memo.get("strategy") or "策略"), memo)
            followup_html = ""
            _STATUS_STYLE = {"已落实": "#26A69A", "未落实": "#EF5350",
                             "不适用": "#8B949E", "不确定": "#D29922"}
            for fu in rep.get("yesterday_followup") or []:
                if not isinstance(fu, dict):
                    continue
                st = str(fu.get("status") or "不确定")
                color = _STATUS_STYLE.get(st, "#D29922")
                followup_html += (
                    f'<div class="qrow" style="align-items:flex-start;">'
                    f'<span class="k" style="flex:0 0 auto;min-width:44px;text-align:center;'
                    f'padding:1px 8px;border-radius:6px;font-size:12px;font-weight:600;'
                    f'color:{color};background:{color}1A;">{_html.escape(st)}</span>'
                    f'<span style="flex:1">{md.to_html(str(fu.get("item") or ""))}'
                    + (f'<div class="sub">{_html.escape(str(fu.get("evidence") or ""))}</div>'
                       if fu.get("evidence") else "")
                    + '</span></div>')
            if followup_html:
                followup_html = ('<div class="note" style="margin-top:8px;"><b>昨日待办核对</b>'
                                 + followup_html + '</div>')
            moves_html = ""
            _DRIVER_CN = {"个股": "#EF5350", "行业": "#D29922", "市场": "#F0B90B"}
            for mv in rep.get("move_attribution") or []:
                if not isinstance(mv, dict):
                    continue
                driver = str(mv.get("driver") or "个股")
                color = _DRIVER_CN.get(driver, "#8B949E")
                chg = str(mv.get("change") or "")
                arrow = "🔺" if not chg.startswith("-") else "🔻"
                moves_html += (
                    f'<div class="qrow" style="align-items:flex-start;">'
                    f'<span class="k" style="flex:0 0 auto;min-width:44px;text-align:center;'
                    f'padding:1px 8px;border-radius:6px;font-size:12px;font-weight:600;'
                    f'color:{color};background:{color}1A;">{_html.escape(driver)}</span>'
                    f'<span style="flex:1">{arrow} {_html.escape(str(mv.get("code") or ""))} '
                    f'{_html.escape(str(mv.get("name") or ""))} {_html.escape(chg)}'
                    f'<div class="sub">{md.to_html(str(mv.get("analysis") or ""))}</div>'
                    '</span></div>')
            if moves_html:
                moves_html = ('<div class="note" style="margin-top:8px;"><b>持仓异动归因</b>'
                              + moves_html + '</div>')
            body = (f'<div class="sub" style="padding:4px 0 8px;">{md.to_html(rep.get("summary", ""))}</div>'
                    + fc_row
                    + market_memo
                    + system_memo
                    + portfolio_memo
                    + strategy_memos
                    + moves_html
                    + followup_html
                    + _li(rep.get("issues", []), '风险', '#E05D5D')
                    + _li(rep.get("suggestions", []), '建议', '#2E9E7B')
                    + _li(rep.get("hypotheses", []), '假设', '#2F81F7')
                    + f'<div class="note" style="margin-top:8px;">模型 {_html.escape(str(row["model"] or ""))} · {_html.escape(str(row["created_at"] or ""))}</div>')
            report_card = _tcard(
                f'🤖 AI 复盘 · {_fmt_date(str(row["date"]))}',
                body,
                actions='<span class="refresh-btn" id="ai-resummary-btn" onclick="aiReSummary()">重新总结</span>')
        else:
            report_card = _tcard(
                "🤖 AI 复盘",
                '<div class="sub">尚未生成复盘报告。<span class="refresh-btn" onclick="aiReSummary()">立即总结</span></div>')
    except Exception as e:
        report_card = _tcard("🤖 AI 复盘", f'<div class="sub">{e}</div>')
    return [report_card]


_AI_CHAT_JS = r"""(function(){
  var CHAT_KEY='qsim_ai_chat';
  var aiHist=JSON.parse(localStorage.getItem(CHAT_KEY)||'[]');
  var chips=['今天各策略表现如何？','当前最大的风险是什么？','哪只策略现在领先？','我的持仓分散度如何？'];
  var box=document.getElementById('ai-chat');
  function esc(v){var d=document.createElement('div');d.textContent=v;return d.innerHTML;}
  function inline(s){
    var t=esc(s);
    t=t.replace(/`([^`]+)`/g,'<code class="md-code">$1</code>');
    t=t.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');
    t=t.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
    t=t.replace(/\[([^\]]+)\]\((https?:[^\s)\]]+)\)/g,'<a href="$2" target="_blank" rel="noopener" style="color:#FFB547">$1</a>');
    return t;
  }
  function mdRender(src){
    var out=[],list=null,code=false,codeBuf=[];
    function closeList(){if(list){out.push('</'+list+'>');list=null;}}
    function closeCode(){if(codeBuf.length){out.push('<pre class="md-pre"><code>'+esc(codeBuf.join('\n'))+'</code></pre>');codeBuf=[];}}
    var ls=(src||'').replace(/\r\n/g,'\n').split('\n');
    for(var i=0;i<ls.length;i++){
      var raw=ls[i];
      if(code){if(/^```/.test(raw)){code=false;closeCode();}else codeBuf.push(raw);continue;}
      if(/^```/.test(raw)){closeList();code=true;codeBuf=[];continue;}
      var h=raw.match(/^(#{1,4})\s+(.*)$/);
      if(h){closeList();out.push('<h'+Math.min(h[1].length+1,5)+' style="margin:6px 0 3px;font-size:14px;color:#EBECED">'+inline(h[2])+'</h'+Math.min(h[1].length+1,5)+'>');continue;}
      if(/^\s*(---|\*\*\*|___)\s*$/.test(raw)){closeList();out.push('<hr style="border:none;border-top:1px solid rgba(42,50,67,.5);margin:6px 0;">');continue;}
      var q=raw.match(/^\s*>\s?(.*)$/);
      if(q){closeList();out.push('<blockquote>'+inline(q[1])+'</blockquote>');continue;}
      var ul=raw.match(/^\s*[-*+]\s+(.*)$/),ol=raw.match(/^\s*\d+\.\s+(.*)$/);
      if(ul||ol){var tag=ul?'ul':'ol';if(list!==tag){closeList();list=tag;out.push('<'+tag+' class="md-list">');}out.push('<li>'+inline(ul?ul[1]:ol[1])+'</li>');continue;}
      closeList();
      if(raw.trim()===''){out.push('<div class="md-blank"></div>');continue;}
      out.push('<div class="md-line">'+inline(raw)+'</div>');
    }
    closeList();closeCode();
    return out.join('');
  }
  function aiFetch(url,opts){
    opts=opts||{};
    var tok='';
    try{tok=sessionStorage.getItem('qsim_tok')||'';}catch(e){}
    opts.headers=opts.headers||{};
    if(tok)opts.headers['X-Auth-Token']=tok;
    return fetch(url,opts).then(function(r){
      if(r.status===401&&!tok){
        var t=prompt('需要 qsim 令牌（X-Auth-Token）才能继续：');
        if(t){try{sessionStorage.setItem('qsim_tok',t);}catch(e){}return aiFetch(url,opts);}
      }
      return r;
    });
  }
  function render(){
    box.innerHTML='';
    for(var i=0;i<aiHist.length;i++){
      var m=aiHist[i],row=document.createElement('div');
      row.style.cssText='max-width:85%;padding:8px 11px;border-radius:12px;font-size:13px;line-height:1.5;white-space:pre-wrap;word-break:break-word;align-self:'+(m.role==='user'?'flex-end':'flex-start')+';background:'+(m.role==='user'?'#2E9E7B':'#171B22')+';color:'+(m.role==='user'?'#0B0E11':'#E8EAED');
      row.innerHTML=(m.role==='user')?esc(m.content):mdRender(m.content);
      box.appendChild(row);
    }
    box.scrollTop=box.scrollHeight;
  }
  function typing(){
    var row=document.createElement('div');
    row.id='ai-typing';
    row.style.cssText='align-self:flex-start;padding:8px 14px;border-radius:12px;background:#171B22;font-size:13px;color:#8B95A7;';
    row.textContent='思考中…';
    box.appendChild(row);box.scrollTop=box.scrollHeight;
  }
  function send(pre){
    var input=document.getElementById('ai-input');
    var q=(pre||input.value).trim();
    if(!q)return;
    input.value='';
    aiHist.push({role:'user',content:q});
    render();typing();
    aiFetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:q,history:aiHist.slice(0,-1).slice(-8)})})
      .then(function(r){return r.json();})
      .then(function(j){
        var t=document.getElementById('ai-typing');if(t)t.remove();
        aiHist.push({role:'assistant',content:j.error?('⚠️ '+j.error):(j.answer||'')});
        localStorage.setItem(CHAT_KEY,JSON.stringify(aiHist.slice(-20)));
        render();
      })
      .catch(function(e){
        var t=document.getElementById('ai-typing');if(t)t.remove();
        aiHist.push({role:'assistant',content:'⚠️ 请求失败: '+e});
        render();
      });
  }
  var cb=document.getElementById('ai-chips');
  for(var i=0;i<chips.length;i++){
    (function(c){var s=document.createElement('span');
      s.style.cssText='cursor:pointer;padding:4px 9px;font-size:11px;background:rgba(255,181,71,.14);color:#FFB547;border-radius:12px;';
      s.textContent=c;
      s.onclick=function(){send(c);};
      cb.appendChild(s);})(chips[i]);
  }
  var inp=document.getElementById('ai-input');
  inp.addEventListener('keydown',function(e){if(e.key==='Enter')send();});
  if(aiHist.length===0){
    aiHist.push({role:'assistant',content:'我是你的量化模拟盘 AI 助手，可以问关于策略、持仓、风险的问题。'});
  }
  render();
  window.aiSend=send;
  window.aiReSummary=function(){
    var b=document.getElementById('ai-resummary-btn');if(b)b.textContent='总结中…';
    aiFetch('/api/ai-summary',{method:'POST'})
      .then(function(r){return r.json();})
      .then(function(){location.reload();})
      .catch(function(){if(b)b.textContent='重新总结';});
  };
})();"""


def _ai_chat() -> list[str]:
    body = (
        '<div id="ai-chat" style="display:flex;flex-direction:column;gap:8px;'
        'height:300px;overflow-y:auto;padding:4px 2px;"></div>'
        '<div id="ai-chips" style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0;"></div>'
        '<div style="display:flex;gap:8px;">'
        '<input id="ai-input" type="text" placeholder="问点什么…" '
        'style="flex:1;background:#171B22;border:1px solid ' + LINE + ';border-radius:10px;'
        'padding:8px 12px;color:' + TEXT + ';font-size:13px;outline:none;">'
        '<button type="button" onclick="aiSend()" '
        'style="background:' + ACCENT + ';border:none;border-radius:10px;padding:8px 16px;'
        'color:#0B0E11;font-weight:700;font-size:13px;cursor:pointer;">发送</button></div>'
        '<script>' + _AI_CHAT_JS + '</script>')
    return [_tcard("💬 AI 对话", body)]


# ═══════════════ 组装 ═══════════════

def _build_cards() -> tuple[str, str, str, str, str, str]:
    ov = net = bt = trd = ai = []
    try:
        ov = _overview()
    except Exception as e:
        ov = [_tcard("总览", f'<div class="sub">{e}</div>')]
    try:
        net = _paper_positions()
    except Exception as e:
        net = [_tcard("模拟盘", f'<div class="sub">{e}</div>')]
    try:
        bt = _experiments()
    except Exception as e:
        bt = [_tcard("实验", f'<div class="sub">{e}</div>')]
    try:
        trd = _shadow()
    except Exception as e:
        trd = [_tcard("对账", f'<div class="sub">{e}</div>')]
    ai: list[str] = []
    for builder in (_ai_review, _ai_chat, _system):
        try:
            ai.extend(builder())
        except Exception as e:
            ai.append(_tcard(builder.__name__.lstrip("_"), f'<div class="sub">{e}</div>'))
    status = f'模式 {STATUS["mode"]} · 任务状态见系统页'
    ts = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%H:%M:%S")
    return (status, ts, "".join(ov), "".join(net), "".join(bt), "".join(trd), "".join(ai))


_RENDER_LOCK = threading.Lock()
_RENDER_CACHE = {}


def _page_version():
    root = q.ROOT
    paths = [root / "data" / name for name in ("quant.db", "quant.db-wal", "v2_market.db", "v2_market.db-wal")]
    paths.extend(root / "artifacts" / name for name in (
        "offsite_backup_state.json", "native_market_state.json", "settlement_state.json", "data_reconcile_state.json", "recovery_backup_state.json", "recovery_drill_state.json"))
    paths.extend((root / "artifacts" / "trend_fallback_research" / "latest.json",
                  root / "artifacts" / "strategy_screening" / "latest.json",
                  root / "config" / "strategy-migration.json"))
    result = []
    for path in paths:
        try:
            info = path.stat()
            result.append((info.st_mtime_ns, info.st_size))
        except OSError:
            result.append(None)
    return tuple(result)


def render_page() -> str:
    # File changes invalidate immediately; time-based cards expire after ten seconds.
    version = _page_version()
    with _RENDER_LOCK:
        now = time.monotonic()
        if _RENDER_CACHE.get("version") == version and now - _RENDER_CACHE.get("at", 0) < 10:
            return _RENDER_CACHE["html"]
        page = _render_page_uncached()
        _RENDER_CACHE.update(version=version, at=time.monotonic(), html=page)
        return page


def _render_page_uncached() -> str:
    from qsim.services.operations import page_snapshot
    with page_snapshot():
        status, ts, ov, net, bt, trd, ai = _build_cards()
        return PAGE.format(status=status, ts=ts,
                           ov_cards=ov, net_cards=net, bt_cards=bt,
                           trd_cards=trd, ai_cards=ai,
                           ACCENT=ACCENT, DANGER=DANGER, WARN=WARN,
                           BG_CARD=BG_CARD, LINE=LINE, TEXT=TEXT, DIM=DIM)

if __name__ == "__main__":
    html = render_page()
    print("页面渲染:", len(html), "字符")
    for key in ("pg_overview", "pg_net", "pg_bt", "pg_trades", "pg_ai"):
        print(f"  {key}: {'✅' if key in html else '❌'}")
