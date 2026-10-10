"""Frozen chronological strategy screening; historical evidence, no promotion.

Two explicit steps: freeze creates an immutable input/source/rule contract;
run verifies that contract before producing results. Only completed runs are
published. Reproduce with the archived source/ and the recorded environment.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import platform
import shutil
import sqlite3
import uuid
import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.metrics import portfolio_metrics
from qsim.engine.runner import run_daily_executor
from qsim.engine.cross_sectional_runner import monthly_rebalance_dates
from qsim.features.factor import compute_factor_ic_scores_asof
from qsim.features.alpha158 import compute_alpha158_ic_scores, ALPHA_FACTORS
from qsim.features.dividend_lowvol import compute_dividend_lowvol_scores
from qsim.features.etf_momentum import compute_momentum_scores, MOM_SYMBOLS
from qsim.strategies.builtin.tsmom import TsmomVolStrategy
from qsim.strategies.builtin.dual_ma import DualMAStrategy
from qsim.strategies.builtin.grid_atr import GridATRStrategy
from qsim.strategies.builtin.rsi_boll import RsiBollStrategy
from qsim.strategies.cross_sectional import TopNScoreStrategy, NoStarMultiFactorStrategy
from qsim.strategies.market_gate import multi_factor_v3_allocations
from qsim.research.trend_fallback import targets, simulate, PARAMETERS
from qsim.research.screening_execution import simulate_basket

ROOT = Path(__file__).resolve().parents[2]
MODE = "chronological_screening"
ETF_COST = CostModel(commission_rate=.0001, min_commission=.1, stamp_tax=0, slippage=.0005)
STOCK_COST = CostModel(round_fee=False, historical_rates=True)
# These are engineering screening defaults, fixed before any run. They are
# eligibility checks for prospective paper observation, never allocation advice.
GATES = {"complete_years": 4, "positive_years": 3, "min_trades": 20,
         "max_drawdown": .25, "drawdown_improvement": .30, "correlation_alert": .85}
SINGLES = {"tsmom_vol": ("时序动量 · 波动率", "513100", TsmomVolStrategy),
           "dual_ma": ("双均线", "510300", DualMAStrategy),
           "grid_atr": ("ATR 网格", "512100", GridATRStrategy),
           "rsi_boll": ("RSI · 布林", "518880", RsiBollStrategy)}
ROTATIONS = {"trend_fallback": ("趋势回退 · 预热原规则", "baseline_warm"),
             "trend_filter": ("趋势回退 · 双弱转现金", "fallback_filter"),
             "trend_cash": ("趋势回退 · 黄金弱转现金", "cash_exit"),
             "trend_vol": ("趋势回退 · 10%波动率", "volatility_cap")}
BASKETS = {"multi_factor": ("多因子", 10, "parity", "factor"),
           "multi_factor_v3": ("多因子 · v3", 10, "live_affordable", "factor"),
           "multi_factor_v4": ("多因子 · v4（非科创板）", 10, "live_affordable", "factor"),
           "alpha158": ("Alpha158（本地110因子）", 10, "parity", "alpha"),
           "dividend_lowvol": ("红利低波", 20, "parity", "dividend"),
           "multi_asset_mom": ("多资产 ETF 动量", 3, "parity", "momentum")}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def code_files(root):
    # Archive every Python dependency, excluding web presentation only.
    return sorted(p for p in (root / "qsim").rglob("*.py")
                  if "__pycache__" not in p.parts and not ("web" in p.parts and "services" in p.parts))


def cost_stress(cost, multiplier):
    # Increase friction, not statutory tax or transfer-fee rates. The latter
    # remain fixed in this hypothetical common cost model, not historical tax.
    return CostModel(**{**asdict(cost), "commission_rate": cost.commission_rate * multiplier,
                        "min_commission": cost.min_commission * multiplier,
                        "slippage": cost.slippage * multiplier})


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    temporary.replace(path)


def freeze(db, output, root=ROOT):
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = output / run_id
    run_dir.mkdir(parents=True, mode=0o700)
    snapshot = run_dir / "input.db"
    with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as source:
        with sqlite3.connect(snapshot) as dest:
            source.backup(dest)
    snapshot.chmod(0o400)
    with sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as conn:
        cutoff = conn.execute("SELECT MAX(date) FROM bars WHERE symbol='510300'").fetchone()[0]
        coverage = {table: list(conn.execute(f"SELECT MIN({date}),MAX({date}),COUNT(*) FROM {table}").fetchone())
                    for table, date in [("bars", "date"), ("stock_bars", "date"),
                                        ("stock_valuation", "date"), ("stock_universe_history", "trade_date")]}
        indices = [r[0] for r in conn.execute("SELECT DISTINCT index_code FROM stock_universe_history")]
    if indices != ["000300.SH"]:
        raise ValueError("ambiguous historical universe: scorers need an explicit index filter")
    hashes = {}
    for file in code_files(root):
        name = file.relative_to(root).as_posix()
        target = run_dir / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
        hashes[name] = digest(file)
        target.chmod(0o400)
    # Freeze public strategy/gate configs separately. No .secrets or credentials.
    for file in sorted((root / "config").glob("*.json")):
        if file.name.startswith(("strategy-", "multi-factor-")):
            target = run_dir / "source" / "config" / file.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
            target.chmod(0o400)
    params = {k: {n: v for n, v in vars(cls()).items() if not n.startswith("_")}
              for k, (_, _, cls) in SINGLES.items()}
    contract = {"mode": MODE, "schema_version": 1, "run_id": run_id,
        "frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_end": cutoff, "input_sha256": digest(snapshot), "coverage": coverage,
        "code_sha256": hashes, "parameters": {"singles": params, "rotations": PARAMETERS,
            "baskets": {k: {"top_n": n, "sizing": s, "score": f} for k, (_, n, s, f) in BASKETS.items()},
            "rolling_ic": {"history_months": 12, "label_trading_days": 20},
            "gate": {"trend_ma": [20, 60], "slope_days": 5, "breadth_ma": 60,
                     "minimum_breadth": 1/3, "target_vol": .20, "vol_days": 20},
            "momentum": {"lookbacks": [63, 252], "symbols": MOM_SYMBOLS},
            "dividend": {"vol_days": 60, "min_names": 40}},
        "gates": GATES, "costs": {"etf": asdict(ETF_COST), "stock": asdict(STOCK_COST), "multipliers": [1, 2, 4]},
        "execution": {"initial_capital": 100000, "annual_reset": True, "lot_size": 100,
            "stock_volume_participation": .05, "etf_volume_cap": None,
            "signal": "当日收盘信号，下一交易日开盘执行；无价格时跳过，失败卖出保留持仓",
            "warmup": "使用各信号日前全部可用历史；每年账户/信号执行状态重置；月度IC只吸收已满20交易日的标签"},
        "folds": [{"year": y, "development_end": f"{y-2}1231", "validation_year": y-1,
                   "start": f"{y}0101", "end": f"{y}1231", "decision": True} for y in range(2022, 2026)]
                 + [{"year": 2026, "development_end": "20241231", "validation_year": 2025,
                     "start": "20260101", "end": min(cutoff, "20261231"), "decision": False}],
        "environment": {"python": platform.python_version(), "pandas": pd.__version__},
        "history_note": "这些历史已经被查看或用于开发，按时间前推检验不是从未接触过的样本外证明。无参数寻优；验证年只用于预热/滚动IC，不选参数。2026未完整年度不参与筛选。",
        "price_note": "本地历史价格收益，未另计现金分红总回报；复权历史可能修订。固定当前成本模型并非逐年真实费率；ETF未建模溢价、冲击成本和成交量约束。",
        "prospective": {"state": "awaiting_new_data", "start_after": cutoff,
            "freeze_date": dt.datetime.now(dt.timezone.utc).date().strftime("%Y%m%d"),
            "policy": "冻结之后的新行情才算前瞻观察。每次采用同一冻结源码并只读取当时已出现的历史；自动评分不改变正式账户配置。"}}
    write_json(run_dir / "contract.json", contract)
    (run_dir / "contract.json").chmod(0o400)
    (run_dir / "contract.sha256").write_text(digest(run_dir / "contract.json"))
    (run_dir / "contract.sha256").chmod(0o400)
    print(json.dumps({"frozen": run_id, "source_end": cutoff, "gates": GATES}, ensure_ascii=False), flush=True)
    return run_dir


def verify_contract(run_dir, root=ROOT):
    if digest(run_dir / "contract.json") != (run_dir / "contract.sha256").read_text().strip():
        raise ValueError("frozen contract checksum mismatch")
    contract = json.loads((run_dir / "contract.json").read_text())
    if contract["mode"] != MODE or digest(run_dir / "input.db") != contract["input_sha256"]:
        raise ValueError("frozen database/contract mismatch")
    for name, expected in contract["code_sha256"].items():
        if digest(root / name) != expected or digest(run_dir / "source" / name) != expected:
            raise ValueError(f"frozen source mismatch: {name}")
    return contract


def metrics(result, benchmark, initial=100000.):
    frame = result["equity"].copy()
    dates = [str(d).replace("-", "")[:8] for d in frame.date]
    values = pd.Series(frame.total_value.to_numpy(float), index=dates)
    returns = values.pct_change(fill_method=None)
    returns.iloc[0] = values.iloc[0] / initial - 1
    nav = pd.concat([pd.Series([1.]), pd.Series(values.to_numpy() / initial)], ignore_index=True)
    mdd = float((nav / nav.cummax() - 1).min())
    sd = float(returns.std())
    total = float(values.iloc[-1] / initial - 1)
    previous = benchmark.shift(1)
    prices = benchmark.reindex(dates)
    base = previous.get(dates[0])
    bnav = prices / base if base and math.isfinite(base) and prices.notna().all() else None
    btotal = float(bnav.iloc[-1] - 1) if bnav is not None else None
    bdd = float((pd.concat([pd.Series([1.]), pd.Series(bnav.to_numpy())], ignore_index=True)
                 .pipe(lambda v: v / v.cummax() - 1)).min()) if bnav is not None else None
    trades = result["trades"]
    stats = {"start": dates[0], "end": dates[-1], "days": len(values), "total_return": total,
             "annual_return": portfolio_metrics(frame, initial, len(trades))["annual_return"], "max_drawdown": mdd,
             "sharpe": float(returns.mean()/sd*math.sqrt(252)) if sd > 0 else None,
             "num_trades": len(trades), "fees": float(sum(t["fee"] for t in trades)),
             "turnover": float(sum(t["amount"] for t in trades))/initial,
             "mean_invested": float((frame.position_value/frame.total_value).mean()),
             "skipped_orders": len(result.get("skips", [])), "benchmark_return": btotal,
             "benchmark_drawdown": bdd, "excess_return": total-btotal if btotal is not None else None}
    return stats, returns, bnav


def chain_metrics(folds):
    nav, bench_nav, capital, bcapital = [], [], 1., 1.
    for item in folds:
        values = item["values"]
        nav.extend(values * capital / 100000.)
        capital = nav[-1]
        if item["bnav"] is not None:
            bench_nav.extend(item["bnav"] * bcapital)
            bcapital = bench_nav[-1]
    def dd(rows):
        s = pd.Series([1.] + list(rows))
        return float((s / s.cummax() - 1).min())
    return {"total_return": capital-1, "max_drawdown": dd(nav),
            "benchmark_return": bcapital-1 if len(nav) == len(bench_nav) else None,
            "benchmark_drawdown": dd(bench_nav) if len(nav) == len(bench_nav) else None,
            "excess_return": capital-bcapital if len(nav) == len(bench_nav) else None}


def classify(annual, stress, data_issues, gates=GATES):
    full = [f for f in annual if f["decision"]]
    reasons = []
    if len(full) < gates["complete_years"] or any(f["metrics"]["days"] < 220 for f in full):
        reasons.append("完整年度覆盖不足")
    trades = sum(f["metrics"]["num_trades"] for f in full)
    if trades < gates["min_trades"]:
        reasons.append(f"成交样本不足：{trades}笔，至少{gates['min_trades']}笔")
    if reasons:
        return {"state": "data_blocked" if data_issues else "insufficient", "label": "数据待补" if data_issues else "样本不足",
                "reasons": data_issues + reasons, "performance_pass": False}
    positive = sum(f["metrics"]["total_return"] > 0 for f in full)
    if positive < gates["positive_years"]:
        reasons.append(f"只有{positive}/{len(full)}个完整年度盈利")
    base = stress["1"]
    if base["max_drawdown"] < -gates["max_drawdown"]:
        reasons.append("四年拼接回撤超过25%")
    for multiplier in ("1", "2", "4"):
        if stress[multiplier]["total_return"] <= 0:
            reasons.append(f"{multiplier}倍成本下四年拼接收益不为正")
    # An asset-matched excess edge OR a material drawdown benefit is required.
    relative = base["excess_return"] is not None and base["excess_return"] > 0
    bdd = base["benchmark_drawdown"]
    defensive = bdd is not None and bdd < 0 and abs(base["max_drawdown"]) <= abs(bdd) * (1-gates["drawdown_improvement"])
    if not (relative or defensive):
        reasons.append("未体现正超额或至少30%的回撤改善")
    if data_issues:
        return {"state": "data_blocked", "label": "数据待补", "reasons": data_issues + reasons, "performance_pass": not reasons}
    return {"state": "observe" if not reasons else "failed", "label": "通过历史门槛 · 待前瞻" if not reasons else "未通过",
            "reasons": reasons or ["达到预设历史门槛；仍需冻结后的新数据验证"], "performance_pass": not reasons}


def run(run_dir, root=ROOT):
    c = verify_contract(run_dir, root)
    db = run_dir / "input.db"
    # Read-only input throughout; source data beyond any signal date remains
    # inaccessible to generate(history). IC weights settle matured labels only.
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        etfs = pd.read_sql_query("SELECT * FROM bars WHERE date<=? ORDER BY date,symbol", conn, params=(c["source_end"],))
        stocks = pd.read_sql_query("SELECT ts_code,date,open,close,vol FROM stock_bars WHERE date<=? ORDER BY date,ts_code", conn, params=(c["source_end"],))
    etf_open = etfs.pivot(index="date", columns="symbol", values="open")
    etf_close = etfs.pivot(index="date", columns="symbol", values="close")
    stock_open = stocks.pivot(index="date", columns="ts_code", values="open")
    stock_close = stocks.pivot(index="date", columns="ts_code", values="close")
    stock_vol = stocks.pivot(index="date", columns="ts_code", values="vol") * 100  # Tushare lots -> shares
    del stocks
    stock_days = list(stock_close.index)
    rb = monthly_rebalance_dates(stock_days)
    er = monthly_rebalance_dates(list(etf_close.index))
    scorers = {"factor": lambda: compute_factor_ic_scores_asof(db, rb, as_of=c["source_end"]),
               "alpha": lambda: compute_alpha158_ic_scores(db, rb, as_of=c["source_end"]),
               "dividend": lambda: compute_dividend_lowvol_scores(db, rb, as_of=c["source_end"]),
               "momentum": lambda: compute_momentum_scores(db, er, as_of=c["source_end"])}
    scores = {}
    for name, scorer in scorers.items():
        print(f"scores: {name}", flush=True)
        scores[name] = pd.DataFrame(scorer()).T.sort_index()
        scores[name].to_csv(run_dir / f"scores_{name}.csv")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        allocations, records = multi_factor_v3_allocations(conn, stock_days, rb)
    write_json(run_dir / "allocations.json", records)
    rotation_bars = etfs[etfs.symbol.isin(["518880", "511380"])][["symbol", "date", "open", "close"]]
    rotation_signals = {k: targets(rotation_bars, v) for k, (_, v) in ROTATIONS.items()}
    report = {"mode": MODE, "state": "research_only", "run_id": c["run_id"],
              "contract_sha256": digest(run_dir / "contract.json"), "contract": c,
              "strategies": [], "redundancy": [], "generated_at": None,
              "notes": ["四年拼接为逐年重启账户的假想收益，非连续实盘账本；年度之间不承接持仓。",
                        "股票研究执行保留失败卖出持仓、先卖后买并限制单笔5%成交量；与旧版parity回放的成交路径可能不同。",
                        "沪深300成员取不晚于信号的历史月度快照，未验证成分变更公告生效时间。",
                        "趋势回退采用完整MA60预热，与近期正式账本的冷启动机制不同；结果不能直接用于晋升原账户。"]}
    aligned = {}
    cases = list(SINGLES) + list(ROTATIONS) + list(BASKETS)
    for key in cases:
        issues, notes = [], []
        if key in SINGLES:
            label, benchmark, cls = SINGLES[key]
            data = etfs[etfs.symbol == benchmark].copy().reset_index(drop=True)
            cost = ETF_COST
            execute = lambda start, end, friction: run_daily_executor(data, cls(), 100000., friction, benchmark,
                                                                      start=start, end=end, warmup=True)
        elif key in ROTATIONS:
            label = ROTATIONS[key][0]
            benchmark, cost = "518880", ETF_COST
            execute = lambda start, end, friction: simulate(rotation_bars, rotation_signals[key], start, end,
                                                           cost=friction, weighted=key == "trend_vol")
            notes.append("黄金ETF仅为主资产参照，非组合风险匹配基准")
        else:
            label, top_n, sizing, feature = BASKETS[key]
            benchmark = "510300"
            is_etf = feature == "momentum"
            cost = ETF_COST if is_etf else STOCK_COST
            opens, closes = (etf_open[MOM_SYMBOLS], etf_close[MOM_SYMBOLS]) if is_etf else (stock_open, stock_close)
            strategy = NoStarMultiFactorStrategy(top_n=top_n) if key == "multi_factor_v4" else TopNScoreStrategy(top_n=top_n)
            gate = allocations if key in ("multi_factor_v3", "multi_factor_v4") else None
            execute = lambda start, end, friction: simulate_basket(opens, closes, scores[feature], strategy,
                [d for d in closes.index if start <= d <= end], friction, sizing=sizing,
                allocations=gate, volume=None if is_etf else stock_vol)
            if feature == "factor":
                issues.append("ST过滤使用当前stock_info名称，缺少历史ST状态；历史行业字段未参与IC路径")
            if feature == "alpha":
                notes.append(f"名称为Alpha158，实际本地自定义因子数{len(ALPHA_FACTORS)}；不是标准Qlib因子实现")
            if is_etf:
                notes.append("沪深300ETF为权益参照，非风险匹配；Top3只有一席现金代理，全负动量仍可能持有负动量资产")
        annual, stress_folds, return_parts = [], {str(n): [] for n in (1, 2, 4)}, []
        for fold in c["folds"]:
            if fold["end"] < fold["start"]:
                continue
            base = None
            for multiplier in (1, 2, 4):
                result = execute(fold["start"], fold["end"], cost_stress(cost, multiplier))
                stats, daily, bnav = metrics(result, etf_close[benchmark].dropna())
                prefix = f"{key}_{fold['year']}_{multiplier}x"
                result["equity"].to_csv(run_dir / f"{prefix}_equity.csv", index=False)
                write_json(run_dir / f"{prefix}_trades.json", result["trades"])
                write_json(run_dir / f"{prefix}_skips.json", result.get("skips", []))
                if multiplier == 1:
                    base = stats
                    if key in BASKETS:
                        signal_dates = [d for d in monthly_rebalance_dates(list(closes.index)) if fold["start"] <= d <= fold["end"]]
                        valid = [d for d in signal_dates if d in scores[feature].index and scores[feature].loc[d].notna().sum() >= top_n]
                        base["valid_months"], base["expected_months"] = len(valid), len(signal_dates)
                        if len(valid) < len(signal_dates):
                            issues.append(f"{fold['year']}月度评分覆盖不足：{len(valid)}/{len(signal_dates)}")
                    if fold["decision"]:
                        return_parts.append(daily)
                if fold["decision"]:
                    stress_folds[str(multiplier)].append({"values": result["equity"].total_value.to_numpy(), "bnav": bnav})
            annual.append({**fold, "metrics": base})
        stress = {n: chain_metrics(values) for n, values in stress_folds.items()}
        entry = {"key": key, "label": label, "benchmark": benchmark, "annual": annual, "stress": stress,
                 "data_issues": list(dict.fromkeys(issues)), "notes": notes,
                 "decision": classify(annual, stress, list(dict.fromkeys(issues)), c["gates"])}
        report["strategies"].append(entry)
        aligned[key] = pd.concat(return_parts)
        print(f"{key}: {entry['decision']['label']} 1x={stress['1']['total_return']:.4f} 2x={stress['2']['total_return']:.4f} 4x={stress['4']['total_return']:.4f}", flush=True)
    report["strategies"].append({"key": "cb_dual_low", "label": "可转债双低 · 留档", "annual": [], "stress": {},
        "decision": {"state": "data_blocked", "label": "数据待补", "performance_pass": False,
            "reasons": ["当前转债筛选依赖当前存续名单、剩余规模及运行当天日期，缺少逐日历史基本面，无法完成可信时点检验；实际执行资产仍为511380ETF"]},
        "data_issues": ["历史存续名单/规模/到期规则未满足PIT"], "notes": ["不沿用旧十年ETF择时曲线作为当前双低代码证据"]})
    frame = pd.DataFrame(aligned)
    frame.to_csv(run_dir / "daily_returns_2022_2025.csv")
    correlation = frame.corr(min_periods=500)
    correlation.to_csv(run_dir / "return_correlation.csv")
    for i, first in enumerate(frame.columns):
        for second in frame.columns[i+1:]:
            rho = correlation.loc[first, second]
            common = frame[[first, second]].dropna()
            if pd.notna(rho) and rho >= c["gates"]["correlation_alert"]:
                report["redundancy"].append({"first": first, "second": second, "correlation": float(rho), "days": len(common)})
    report["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    report["result_sha256"] = {p.name: digest(p) for p in sorted(run_dir.iterdir())
                               if p.suffix in (".csv", ".json") and p.name not in ("summary.json", "contract.json")}
    write_json(run_dir / "summary.json", report)
    # Verify inputs/source again before publishing a complete result.
    verify_contract(run_dir, root)
    write_json(run_dir.parent / "latest.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("freeze", "run"))
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "quant.db")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "strategy_screening")
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    if args.action == "freeze":
        freeze(args.database, args.output)
    elif args.run_dir:
        result = run(args.run_dir)
        print(json.dumps({"run_id": result["run_id"], "complete": True}, ensure_ascii=False))
    else:
        parser.error("run requires --run-dir")
