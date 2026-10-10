"""Combined portfolio backtest: single-asset sleeves + cross-sectional sleeves.

This runner unifies single-asset strategies (ETF/grid/trend) and cross-sectional
strategies (multi-factor / Alpha158) into one account.  Allocation policy maps
regime probabilities to per-strategy base weights; cross-sectional sleeves
distribute their base weight equally across the selected stock list.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable, Mapping

import numpy as np
import pandas as pd

from qsim.allocation.cost_aware import should_rebalance
from qsim.allocation.policy import RegimeAllocationPolicy
from qsim.allocation.regime import SimpleRegimeDetector
from qsim.engine.costs import CostModel
from qsim.engine.cross_sectional_runner import (
    CrossSectionalData,
    monthly_rebalance_dates,
    run_cross_sectional_backtest,
)
from qsim.engine.matcher import buy, sell
from qsim.engine.metrics import portfolio_metrics
from qsim.engine.portfolio_runner import Sleeve
from qsim.market.data_source import DataSource
from qsim.risk.limits import RiskLimits, check_risk
from qsim.strategies.base import Strategy
from qsim.strategies.cross_sectional import (
    CrossSectionContext,
    CrossSectionalStrategy,
)
from qsim.strategies.rotation import RotationStrategy


@dataclass(frozen=True, slots=True)
class CrossSleeve:
    name: str
    strategy: CrossSectionalStrategy
    score_provider: Callable[[str], pd.Series]
    top_n: int = 10


@dataclass(frozen=True, slots=True)
class RotationSleeve:
    name: str
    strategy: RotationStrategy
    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SignalTimeline:
    """Config-independent per-day signals shared across scan configs.

    Every field is a pure function of market/sleeve data (strategy state
    evolves identically regardless of allocation weights), so one timeline
    built over the full date range can be replayed by any number of
    ``run_combined_portfolio_backtest`` configs; each config then only pays
    for the execution replay, not signal generation.
    """

    regime_probs: dict[str, dict[str, float]]
    single_signals: dict[str, dict[str, object]]
    rotation_targets: dict[str, dict[str, str]]
    cross_selected: dict[str, dict[str, tuple[str, ...]]]


def _date_str(value) -> str:
    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")
    return str(value).replace("-", "")


def _signal_parts(signal) -> tuple[str, float]:
    if isinstance(signal, tuple):
        return signal[0], float(signal[1] if len(signal) > 1 else 0.0)
    return str(signal), 0.0


def _position_value(shares: int, price: float) -> float:
    return shares * price


def _weight_of(shares: int, price: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return _position_value(shares, price) / total


def _prepare_universe(
    single_sleeves: list[Sleeve],
    rotation_sleeves: list[RotationSleeve],
    data_source: DataSource,
    stock_open: pd.DataFrame,
    market_symbol: str | None,
    start: str | None,
    end: str | None,
):
    single_bars: dict[str, pd.DataFrame] = {}
    for sleeve in single_sleeves:
        bars = data_source.get_bars(sleeve.symbol, start=start, end=end)
        if bars.empty:
            raise ValueError(f"no bars for {sleeve.symbol}")
        single_bars[sleeve.symbol] = bars.reset_index(drop=True)

    rotation_bars: dict[str, dict[str, pd.DataFrame]] = {}
    for sleeve in rotation_sleeves:
        rotation_bars[sleeve.name] = {}
        for sym in sleeve.symbols:
            bars = data_source.get_bars(sym, start=start, end=end)
            if bars.empty:
                raise ValueError(f"no bars for {sym}")
            rotation_bars[sleeve.name][sym] = bars.reset_index(drop=True)

    if market_symbol is None:
        market_symbol = single_sleeves[0].symbol if single_sleeves else "510300"
    market_bars = data_source.get_bars(market_symbol, start=start, end=end)

    stock_dates = sorted(stock_open.index)
    single_dates = sorted(
        {_date_str(b) for bars in single_bars.values() for b in bars["date"]}
    )
    rotation_dates = sorted(
        {
            _date_str(b)
            for sleeve_bars in rotation_bars.values()
            for bars in sleeve_bars.values()
            for b in bars["date"]
        }
    )
    dates = sorted(set(stock_dates) | set(single_dates) | set(rotation_dates))
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    if not dates:
        raise ValueError("no dates in combined backtest")

    single_keys = {
        sleeve.symbol: np.asarray(single_bars[sleeve.symbol]["date"].astype(str).str.replace("-", ""))
        for sleeve in single_sleeves
    }
    rotation_keys = {
        sleeve.name: {
            sym: np.asarray(bars["date"].astype(str).str.replace("-", ""))
            for sym, bars in rotation_bars[sleeve.name].items()
        }
        for sleeve in rotation_sleeves
    }
    market_keys = np.asarray(market_bars["date"].astype(str).str.replace("-", ""))
    return (
        single_bars,
        rotation_bars,
        market_bars,
        market_symbol,
        single_keys,
        rotation_keys,
        market_keys,
        dates,
    )


def precompute_signal_timeline(
    *,
    single_sleeves: list[Sleeve],
    cross_sleeves: list[CrossSleeve],
    rotation_sleeves: list[RotationSleeve] | None = None,
    data_source: DataSource,
    stock_open: pd.DataFrame,
    stock_close: pd.DataFrame,
    regime_detector: SimpleRegimeDetector,
    market_symbol: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> SignalTimeline:
    """Replay the exact per-day strategy call sequence once and record results.

    The sequence (reset, then one generate/detect/select per date in calendar
    order with history truncated at that date) is identical to what every
    config run would execute, so the recorded timeline is bit-identical to
    live generation for any allocation policy.
    """
    rotation_sleeves = rotation_sleeves or []
    (
        single_bars,
        rotation_bars,
        market_bars,
        _,
        single_keys,
        rotation_keys,
        market_keys,
        dates,
    ) = _prepare_universe(
        single_sleeves, rotation_sleeves, data_source, stock_open, market_symbol, start, end
    )
    rebalance_dates = set(monthly_rebalance_dates(dates))

    for sleeve in single_sleeves:
        if hasattr(sleeve.strategy, "reset"):
            sleeve.strategy.reset()
    for sleeve in cross_sleeves:
        if hasattr(sleeve.strategy, "reset"):
            sleeve.strategy.reset()
    for sleeve in rotation_sleeves:
        if hasattr(sleeve.strategy, "reset"):
            sleeve.strategy.reset()

    regime_probs: dict[str, dict[str, float]] = {}
    single_signals: dict[str, dict[str, object]] = {s.name: {} for s in single_sleeves}
    rotation_targets: dict[str, dict[str, str]] = {s.name: {} for s in rotation_sleeves}
    cross_selected: dict[str, dict[str, tuple[str, ...]]] = {s.name: {} for s in cross_sleeves}

    last_closes: dict[str, float] = {}
    for date in dates:
        market_pos = int(np.searchsorted(market_keys, date, side="right"))
        regime_probs[date] = regime_detector.detect(market_bars.iloc[:market_pos])

        for sleeve in single_sleeves:
            keys = single_keys[sleeve.symbol]
            pos = int(np.searchsorted(keys, date, side="right"))
            history = single_bars[sleeve.symbol].iloc[:pos].reset_index(drop=True)
            single_signals[sleeve.name][date] = sleeve.strategy.generate(history)

        for sleeve in rotation_sleeves:
            hist: dict[str, pd.DataFrame] = {}
            for sym, bars in rotation_bars[sleeve.name].items():
                pos = int(np.searchsorted(rotation_keys[sleeve.name][sym], date, side="right"))
                hist[sym] = bars.iloc[:pos].reset_index(drop=True)
            rotation_targets[sleeve.name][date] = sleeve.strategy.generate(hist)

        if date in rebalance_dates:
            for sleeve in cross_sleeves:
                scores = sleeve.score_provider(date)
                if scores is None or scores.empty:
                    cross_selected[sleeve.name][date] = ()
                    continue
                context = CrossSectionContext(
                    date=date,
                    prices=pd.Series(dtype=float),
                    universe=list(scores.index),
                    factor_scores=scores,
                )
                cross_selected[sleeve.name][date] = tuple(
                    sleeve.strategy.select(context)[: sleeve.top_n]
                )

    return SignalTimeline(
        regime_probs=regime_probs,
        single_signals=single_signals,
        rotation_targets=rotation_targets,
        cross_selected=cross_selected,
    )


def run_combined_portfolio_backtest(
    single_sleeves: list[Sleeve],
    cross_sleeves: list[CrossSleeve],
    policy: RegimeAllocationPolicy,
    data_source: DataSource,
    stock_open: pd.DataFrame,
    stock_close: pd.DataFrame,
    regime_detector: SimpleRegimeDetector,
    initial_cash: float,
    cost: CostModel,
    start: str | None = None,
    end: str | None = None,
    risk_limits: RiskLimits | None = None,
    assumed_alpha: float = 0.02,
    market_symbol: str | None = None,
    rotation_sleeves: list[RotationSleeve] | None = None,
    signal_timeline: SignalTimeline | None = None,
) -> dict:
    limits = risk_limits or RiskLimits()
    rotation_sleeves = rotation_sleeves or []

    (
        single_bars,
        rotation_bars,
        market_bars,
        _,
        single_keys,
        rotation_keys,
        market_keys,
        dates,
    ) = _prepare_universe(
        single_sleeves, rotation_sleeves, data_source, stock_open, market_symbol, start, end
    )
    rebalance_dates = set(monthly_rebalance_dates(dates))

    if signal_timeline is None:
        for sleeve in single_sleeves:
            if hasattr(sleeve.strategy, "reset"):
                sleeve.strategy.reset()
        for sleeve in cross_sleeves:
            if hasattr(sleeve.strategy, "reset"):
                sleeve.strategy.reset()
        for sleeve in rotation_sleeves:
            if hasattr(sleeve.strategy, "reset"):
                sleeve.strategy.reset()

    cash = initial_cash
    shares: dict[str, int] = {}
    avg_cost: dict[str, float] = {}
    pending_targets: dict[str, float] = {}
    pending_single_targets: dict[str, float] = {}
    pending_cross_selected: dict[str, list[str]] = {}
    pending_rotation_targets: dict[str, str] = {}
    trades: list[dict] = []
    skips: list[dict] = []
    equity_rows: list[dict] = []

    # Stock-panel scalar lookups per symbol per day used to dominate scan
    # runtime; the panel is converted once to numpy and only symbols that are
    # held or targeted are looked up (all other entries are zeros everywhere
    # downstream, so skipping them cannot change weights or trades).
    close_v = stock_close.ffill()
    so_pos = {d: i for i, d in enumerate(stock_open.index)}
    so_col = {s: j for j, s in enumerate(stock_open.columns)}
    so_vals = stock_open.to_numpy()
    sc_pos = {d: i for i, d in enumerate(close_v.index)}
    sc_col = {s: j for j, s in enumerate(close_v.columns)}
    sc_vals = close_v.to_numpy()
    static_syms = [s.symbol for s in single_sleeves] + [
        sym for s in rotation_sleeves for sym in s.symbols
    ]
    static_set = set(static_syms)

    last_closes: dict[str, float] = {}
    for date in dates:
        open_prices: dict[str, float] = {}
        close_prices: dict[str, float] = {}

        for sleeve in single_sleeves:
            sym = sleeve.symbol
            keys = single_keys[sym]
            pos = int(np.searchsorted(keys, date))
            if pos >= len(keys) or keys[pos] != date:
                continue
            row = single_bars[sym].iloc[pos]
            open_prices[sym] = float(row["open"])
            close_prices[sym] = float(row["close"])

        for sleeve in rotation_sleeves:
            for sym, bars in rotation_bars[sleeve.name].items():
                keys = rotation_keys[sleeve.name][sym]
                pos = int(np.searchsorted(keys, date))
                if pos >= len(keys) or keys[pos] != date:
                    continue
                row = bars.iloc[pos]
                open_prices[sym] = float(row["open"])
                close_prices[sym] = float(row["close"])

        lookup_syms = static_syms + sorted(
            (set(shares) | set(pending_targets)) - static_set
        )
        so_i = so_pos.get(date)
        if so_i is not None:
            row = so_vals[so_i]
            for sym in lookup_syms:
                j = so_col.get(sym)
                if j is None:
                    continue
                val = row[j]
                if pd.notna(val):
                    open_prices[sym] = float(val)
        sc_i = sc_pos.get(date)
        if sc_i is not None:
            row = sc_vals[sc_i]
            for sym in lookup_syms:
                j = sc_col.get(sym)
                if j is None:
                    continue
                val = row[j]
                if pd.notna(val):
                    close_prices[sym] = float(val)

        # Execute pending targets at today's open.
        if pending_targets:
            total_value_open = cash + sum(
                _position_value(shares[sym], open_prices.get(sym, last_closes.get(sym, 0.0)))
                for sym in list(shares)
            )
            current_weights = {
                sym: _weight_of(shares.get(sym, 0), open_prices[sym], total_value_open)
                for sym in open_prices
            }
            target_weights = {sym: pending_targets.get(sym, 0.0) for sym in open_prices}
            current_dd = _drawdown(equity_rows)
            target_weights = _apply_risk_overlay(target_weights, current_dd, limits)
            t = sum(abs(target_weights.get(k, 0.0) - current_weights.get(k, 0.0)) for k in set(current_weights) | set(target_weights)) / 2
            expected_gain = sum(target_weights.values()) * total_value_open * assumed_alpha
            do_rebalance, _, est_cost = should_rebalance(
                current_weights,
                target_weights,
                expected_gain=expected_gain,
                portfolio_value=total_value_open,
                cost=cost,
            )
            risk = check_risk(
                target_weights,
                current_drawdown=current_dd,
                turnover=t,
                limits=limits,
            )
            if any(count > 0 and sym not in open_prices for sym, count in shares.items()):
                skips.append({"date": date, "reason": "持仓开盘价缺失，停止新增风险"})
                pending_targets = dict(current_weights)
            elif not do_rebalance:
                skips.append({"date": date, "reason": f"cost gate cost={est_cost}"})
                pending_targets = dict(current_weights)
            elif not risk.ok:
                skips.append({"date": date, "reason": "; ".join(risk.violations)})
                pending_targets = dict(current_weights)
            else:
                for sym in open_prices:
                    target_value = target_weights.get(sym, 0.0) * total_value_open
                    current_value = _position_value(shares.get(sym, 0), open_prices[sym])
                    diff = target_value - current_value
                    if diff > 0:
                        cash, shares[sym], trade, reason = buy(
                            cash,
                            shares.get(sym, 0),
                            open_prices[sym],
                            date,
                            cost,
                            sym,
                            budget=min(diff, cash),
                            prev_close=last_closes.get(sym),
                        )
                        if trade:
                            prev_shares = shares[sym] - trade.shares
                            avg_cost[sym] = (
                                (avg_cost.get(sym, 0.0) * prev_shares + trade.amount + trade.fee) / shares[sym]
                                if shares[sym] > 0
                                else 0.0
                            )
                            trades.append(trade.to_dict())
                        elif reason:
                            skips.append({"date": date, "symbol": sym, "side": "BUY", "reason": reason})
                    elif diff < 0:
                        cash, shares[sym], trade, reason = sell(
                            cash,
                            shares.get(sym, 0),
                            open_prices[sym],
                            date,
                            cost,
                            sym,
                            sell_value=-diff,
                            avg_cost=avg_cost.get(sym, 0.0),
                            prev_close=last_closes.get(sym),
                        )
                        if trade:
                            trades.append(trade.to_dict())
                        elif reason:
                            skips.append({"date": date, "symbol": sym, "side": "SELL", "reason": reason})
                        if shares.get(sym, 0) == 0:
                            shares.pop(sym, None)
                            avg_cost.pop(sym, None)

        # Suspended holdings retain their last observed mark; missing a row
        # cannot erase a position from NAV. No future price is backfilled.
        for sym, count in shares.items():
            if count and sym not in close_prices:
                if sym not in last_closes:
                    raise ValueError("held symbol has no historical close mark")
                close_prices[sym] = last_closes[sym]
        last_closes.update(close_prices)
        # Mark equity at close.
        total_value_close = cash + sum(
            _position_value(shares[sym], close_prices[sym])
            for sym in list(shares) if sym in close_prices
        )
        equity_rows.append(
            {
                "date": date,
                "cash": cash,
                "position_value": total_value_close - cash,
                "total_value": total_value_close,
            }
        )

        # Generate next period targets.
        if signal_timeline is None:
            market_pos = int(np.searchsorted(market_keys, date, side="right"))
            market_history = market_bars.iloc[:market_pos]
            regime_prob = regime_detector.detect(market_history)
        else:
            regime_prob = signal_timeline.regime_probs[date]
        base_weights = policy.target_weights(regime_prob)

        new_targets: dict[str, float] = {}

        def add_target(sym: str, weight: float) -> None:
            if weight > 0:
                new_targets[sym] = new_targets.get(sym, 0.0) + weight

        for sleeve in single_sleeves:
            if signal_timeline is None:
                keys = single_keys[sleeve.symbol]
                pos = int(np.searchsorted(keys, date, side="right"))
                history = single_bars[sleeve.symbol].iloc[:pos].reset_index(drop=True)
                signal = sleeve.strategy.generate(history)
            else:
                signal = signal_timeline.single_signals[sleeve.name][date]
            sig, _ = _signal_parts(signal)
            if sig == "SELL":
                single_target = 0.0
            elif sig == "BUY":
                single_target = base_weights.get(sleeve.name, 0.0)
            else:
                single_target = pending_single_targets.get(sleeve.name, 0.0)
            pending_single_targets[sleeve.name] = single_target
            add_target(sleeve.symbol, single_target)

        if date in rebalance_dates:
            for sleeve in cross_sleeves:
                if signal_timeline is None:
                    scores = sleeve.score_provider(date)
                    if scores is None or scores.empty:
                        pending_cross_selected[sleeve.name] = []
                        continue
                    context = CrossSectionContext(
                        date=date,
                        prices=close_prices if isinstance(close_prices, pd.Series) else pd.Series(dtype=float),
                        universe=list(scores.index),
                        factor_scores=scores,
                    )
                    selected = sleeve.strategy.select(context)[: sleeve.top_n]
                else:
                    selected = list(signal_timeline.cross_selected[sleeve.name][date])
                pending_cross_selected[sleeve.name] = selected
                weight = base_weights.get(sleeve.name, 0.0)
                per = weight / len(selected) if selected else 0.0
                for sym in selected:
                    add_target(sym, per)

        # Keep cross-sectional targets unchanged on non-rebalance days.
        for sleeve in cross_sleeves:
            if date not in rebalance_dates:
                selected = pending_cross_selected.get(sleeve.name, [])
                weight = base_weights.get(sleeve.name, 0.0)
                per = weight / len(selected) if selected else 0.0
                for sym in selected:
                    add_target(sym, per)

        # Rotation sleeves evaluate every day and hold the best target symbol.
        for sleeve in rotation_sleeves:
            if signal_timeline is None:
                hist: dict[str, pd.DataFrame] = {}
                for sym, bars in rotation_bars[sleeve.name].items():
                    pos = int(np.searchsorted(rotation_keys[sleeve.name][sym], date, side="right"))
                    hist[sym] = bars.iloc[:pos].reset_index(drop=True)
                target = sleeve.strategy.generate(hist)
            else:
                target = signal_timeline.rotation_targets[sleeve.name][date]
            weight = base_weights.get(sleeve.name, 0.0)
            if target == "HOLD":
                prev = pending_rotation_targets.get(sleeve.name)
                if prev and prev in rotation_bars[sleeve.name]:
                    add_target(prev, weight)
            else:
                pending_rotation_targets[sleeve.name] = target
                if target != "CASH" and target in rotation_bars[sleeve.name]:
                    add_target(target, weight)

        pending_targets = new_targets

    equity = pd.DataFrame(equity_rows)
    metrics = portfolio_metrics(equity, initial_cash, len(trades))
    return {"equity": equity, "trades": trades, "metrics": metrics, "skips": skips}


def _drawdown(equity_rows: list[dict]) -> float:
    if not equity_rows:
        return 0.0
    values = [row["total_value"] for row in equity_rows]
    peak = max(values)
    return values[-1] / peak - 1.0 if peak > 0 else 0.0


def _apply_risk_overlay(
    target_weights: dict[str, float],
    current_drawdown: float,
    limits: RiskLimits,
) -> dict[str, float]:
    """Reduce exposure with a simple tiered drawdown rule.

    This is a fixed, transparent rule (not tuned on many variants):
      drawdown <= -10% -> 0.60x
      drawdown <= -15% -> 0.35x
      drawdown <= -20% -> 0.15x
      drawdown <= -25% -> 0.05x
    Single-strategy weights are also capped.
    """
    scale = 1.0
    if current_drawdown <= -0.25:
        scale = 0.05
    elif current_drawdown <= -0.20:
        scale = 0.15
    elif current_drawdown <= -0.15:
        scale = 0.35
    elif current_drawdown <= -0.10:
        scale = 0.60

    out = {sym: weight * scale for sym, weight in target_weights.items()}
    for sym, weight in out.items():
        out[sym] = min(weight, limits.max_single_strategy_weight)
    return {sym: round(weight, 6) for sym, weight in out.items() if weight > 0}
