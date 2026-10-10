#!/usr/bin/env python3
"""Run at least five historical backtests using the local read-only market data.

Usage:
    python scripts/run_backtest_verification.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from qsim.engine.costs import CostModel
from qsim.engine.runner import run_backtest
from qsim.market.data_source import SqliteDataSource

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "quant.db"

ETF_COST = CostModel(
    commission_rate=0.0001,
    min_commission=0.1,
    stamp_tax=0.0,
    slippage=0.0005,
)


class BuyHold:
    name = "buy_hold"

    def __init__(self) -> None:
        self._first = True

    def reset(self) -> None:
        self._first = True

    def generate(self, df: pd.DataFrame):
        if self._first:
            self._first = False
            return "BUY"
        return "HOLD"


class Hold:
    name = "hold"

    def generate(self, df: pd.DataFrame):
        return "HOLD"


class Momentum:
    name = "momentum"

    def generate(self, df: pd.DataFrame):
        closes = df["close"]
        if len(closes) < 2:
            return "HOLD"
        return "BUY" if closes.iloc[-1] > closes.iloc[-2] else "SELL"


class MaCross:
    name = "ma_cross"

    def __init__(self, n: int = 20) -> None:
        self.n = n

    def generate(self, df: pd.DataFrame):
        closes = df["close"]
        if len(closes) < self.n:
            return "HOLD"
        ma = closes.rolling(self.n).mean().iloc[-1]
        return "BUY" if closes.iloc[-1] > ma else "SELL"


def fmt(value: float) -> str:
    if value is None:
        return "N/A"
    return f"{value:.4f}"


def main() -> None:
    source = SqliteDataSource(DB_PATH, table="bars", symbol_column="symbol")

    cases = [
        ("512100", "20240101", "20241231", BuyHold(), "BuyHold"),
        ("510300", "20240101", "20251231", Momentum(), "Momentum"),
        ("510500", "20230101", "20241231", MaCross(20), "MaCross20"),
        ("159915", "20240601", "20260825", Momentum(), "Momentum"),
        ("588000", "20250101", "20260825", BuyHold(), "BuyHold"),
        ("518880", "20240101", "20260825", MaCross(60), "MaCross60"),
        ("513100", "20250101", "20260825", Hold(), "Hold"),
    ]

    print(f"{'#':<2} {'symbol':<8} {'period':<23} {'strategy':<10} "
          f"{'bars':<6} {'trades':<7} {'final':<12} {'return':<10} "
          f"{'mdd':<10} {'sharpe':<8} status")
    print("-" * 110)

    passed = 0
    for idx, (symbol, start, end, strategy, label) in enumerate(cases, start=1):
        try:
            bars = source.get_bars(symbol, start=start, end=end)
            result = run_backtest(
                bars,
                strategy,
                initial_cash=100000.0,
                cost=ETF_COST,
                symbol=symbol,
                start=start,
                end=end,
            )
            metrics = result["metrics"]
            print(
                f"{idx:<2} {symbol:<8} {start}-{end:<14} {label:<10} "
                f"{len(bars):<6} {len(result['trades']):<7} "
                f"{metrics.get('final_value', 0):<12.2f} "
                f"{fmt(metrics.get('total_return', 0))}   "
                f"{fmt(metrics.get('max_drawdown', 0))}   "
                f"{fmt(metrics.get('sharpe', 0))}  OK"
            )
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"{idx:<2} {symbol:<8} {start}-{end:<14} {label:<10} "
                f"FAILED: {type(exc).__name__}: {exc}"
            )

    print("-" * 110)
    print(f"passed {passed}/{len(cases)}")
    if passed != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
