"""Portfolio backtest: strategy pool + allocation + cost gate + risk limits.

This runner treats every sleeve as a separate signal source on its own symbol.
The allocation layer converts regime probabilities into base target weights.
Strategy signals then decide whether a sleeve should be invested (BUY), flat
(SELL) or kept unchanged (HOLD).

The final target weights are executed only if the cost gate and risk limits
allow the rebalance.
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
from qsim.engine.matcher import buy, sell
from qsim.engine.metrics import portfolio_metrics
from qsim.market.data_source import DataSource
from qsim.risk.limits import RiskLimits, check_risk
from qsim.strategies.base import Strategy


@dataclass(frozen=True, slots=True)
class Sleeve:
    name: str
    symbol: str
    strategy: Strategy


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


def run_portfolio_backtest(
    sleeves: list[Sleeve],
    policy: RegimeAllocationPolicy,
    data_source: DataSource,
    regime_detector: SimpleRegimeDetector,
    initial_cash: float,
    cost: CostModel,
    start: str | None = None,
    end: str | None = None,
    risk_limits: RiskLimits | None = None,
    assumed_alpha: float = 0.02,
    market_symbol: str | None = None,
) -> dict:
    limits = risk_limits or RiskLimits()
    bars_by_symbol: dict[str, pd.DataFrame] = {}
    for sleeve in sleeves:
        bars = data_source.get_bars(sleeve.symbol, start=start, end=end)
        if bars.empty:
            raise ValueError(f"no bars for {sleeve.symbol}")
        bars_by_symbol[sleeve.symbol] = bars.reset_index(drop=True)

    if market_symbol is None:
        market_symbol = sleeves[0].symbol
    market_bars = data_source.get_bars(market_symbol, start=start, end=end)
    if market_bars.empty:
        raise ValueError(f"no market bars for {market_symbol}")

    dates = sorted({_date_str(b) for bars in bars_by_symbol.values() for b in bars["date"]}
                   | {_date_str(b) for b in market_bars["date"]})
    if not dates:
        raise ValueError("no dates in portfolio backtest")

    # Hoisted once: per-day string normalization of the whole frame was the
    # dominant loop cost on multi-year scans.
    bar_keys = {
        sym: np.asarray(bars["date"].astype(str).str.replace("-", ""))
        for sym, bars in bars_by_symbol.items()
    }
    market_keys = np.asarray(market_bars["date"].astype(str).str.replace("-", ""))

    for sleeve in sleeves:
        if hasattr(sleeve.strategy, "reset"):
            sleeve.strategy.reset()

    cash = initial_cash
    shares: dict[str, int] = {sleeve.symbol: 0 for sleeve in sleeves}
    avg_cost: dict[str, float] = {sleeve.symbol: 0.0 for sleeve in sleeves}
    pending_targets: dict[str, float] = {sleeve.symbol: 0.0 for sleeve in sleeves}
    trades: list[dict] = []
    skips: list[dict] = []
    equity_rows: list[dict] = []

    last_closes: dict[str, float] = {}
    for date in dates:
        open_prices: dict[str, float] = {}
        close_prices: dict[str, float] = {}
        for sleeve in sleeves:
            sym = sleeve.symbol
            keys = bar_keys[sym]
            pos = int(np.searchsorted(keys, date))
            if pos >= len(keys) or keys[pos] != date:
                continue
            bar = bars_by_symbol[sym].iloc[pos]
            open_prices[sym] = float(bar["open"])
            close_prices[sym] = float(bar["close"])

        # 1) Execute pending targets at today's open.
        if open_prices:
            total_value_open = cash + sum(
                _position_value(shares[sym], open_prices.get(sym, last_closes.get(sym, 0.0)))
                for sym in shares
            )
            current_weights = {
                sym: _weight_of(shares[sym], open_prices[sym], total_value_open)
                for sym in open_prices
            }
            target_weights = {sym: pending_targets.get(sym, 0.0) for sym in open_prices}

            do_rebalance, t, est_cost = should_rebalance(
                current_weights,
                target_weights,
                expected_gain=turnover_gain(target_weights, total_value_open, assumed_alpha),
                portfolio_value=total_value_open,
                cost=cost,
            )
            risk = check_risk(
                target_weights,
                current_drawdown=_current_drawdown(equity_rows),
                turnover=t,
                limits=limits,
            )
            if any(count > 0 and sym not in open_prices for sym, count in shares.items()):
                skips.append({"date": date, "reason": "持仓开盘价缺失，停止新增风险"})
                pending_targets = dict(current_weights)
            elif not do_rebalance:
                skips.append({"date": date, "reason": f"cost gate: turnover={t}, cost={est_cost}"})
                pending_targets = dict(current_weights)
            elif not risk.ok:
                skips.append({"date": date, "reason": "; ".join(risk.violations)})
                pending_targets = dict(current_weights)
            else:
                for sym in open_prices:
                    target_value = target_weights.get(sym, 0.0) * total_value_open
                    current_value = _position_value(shares[sym], open_prices[sym])
                    diff = target_value - current_value
                    if diff > 0:
                        cash, shares[sym], trade, reason = buy(
                            cash,
                            shares[sym],
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
                                (avg_cost[sym] * prev_shares + trade.amount + trade.fee) / shares[sym]
                                if shares[sym] > 0
                                else 0.0
                            )
                            trades.append(trade.to_dict())
                        elif reason:
                            skips.append({"date": date, "symbol": sym, "side": "BUY", "reason": reason})
                    elif diff < 0:
                        cash, shares[sym], trade, reason = sell(
                            cash,
                            shares[sym],
                            open_prices[sym],
                            date,
                            cost,
                            sym,
                            sell_value=-diff,
                            avg_cost=avg_cost[sym],
                            prev_close=last_closes.get(sym),
                        )
                        if trade:
                            trades.append(trade.to_dict())
                        elif reason:
                            skips.append({"date": date, "symbol": sym, "side": "SELL", "reason": reason})

        # Suspended holdings retain their last observed mark; missing a row
        # cannot erase a position from NAV. No future price is backfilled.
        for sym, count in shares.items():
            if count and sym not in close_prices:
                if sym not in last_closes:
                    raise ValueError("held symbol has no historical close mark")
                close_prices[sym] = last_closes[sym]
        last_closes.update(close_prices)
        # 2) Mark equity at today's close.
        total_value_close = cash + sum(
            _position_value(shares[sym], close_prices[sym])
            for sym in close_prices
        )
        equity_rows.append(
            {
                "date": date,
                "cash": cash,
                "position_value": total_value_close - cash,
                "total_value": total_value_close,
            }
        )

        # 3) Generate tomorrow's targets.
        market_pos = int(np.searchsorted(market_keys, date, side="right"))
        market_history = market_bars.iloc[:market_pos]
        regime_prob = regime_detector.detect(market_history)
        base_weights = policy.target_weights(regime_prob)

        new_targets: dict[str, float] = {}
        for sleeve in sleeves:
            sym = sleeve.symbol
            keys = bar_keys[sym]
            pos = int(np.searchsorted(keys, date, side="right"))
            history = bars_by_symbol[sym].iloc[:pos].reset_index(drop=True)
            signal = sleeve.strategy.generate(history)
            sig, _ = _signal_parts(signal)
            if sig == "SELL":
                new_targets[sym] = 0.0
            elif sig == "BUY":
                new_targets[sym] = base_weights.get(sleeve.name, 0.0)
            else:
                new_targets[sym] = pending_targets.get(sym, 0.0)
        pending_targets = new_targets

    equity = pd.DataFrame(equity_rows)
    metrics = portfolio_metrics(equity, initial_cash, len(trades))
    return {
        "equity": equity,
        "trades": trades,
        "metrics": metrics,
        "skips": skips,
    }


def turnover_gain(target_weights: Mapping[str, float], portfolio_value: float, assumed_alpha: float) -> float:
    """Heuristic expected gain used by the demo cost gate.

    Assumes rebalancing captures ``assumed_alpha`` of the traded value.
    """
    traded_value = sum(target_weights.values()) * portfolio_value
    return traded_value * assumed_alpha


def _current_drawdown(equity_rows: list[dict]) -> float:
    if not equity_rows:
        return 0.0
    values = [row["total_value"] for row in equity_rows]
    peak = max(values)
    return values[-1] / peak - 1.0 if peak > 0 else 0.0
