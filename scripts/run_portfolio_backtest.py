#!/usr/bin/env python3
"""Run a portfolio backtest combining strategy pool, allocation, cost and risk."""

from __future__ import annotations

from pathlib import Path

from qsim.allocation.policy import default_policy
from qsim.allocation.regime import SimpleRegimeDetector
from qsim.engine.costs import CostModel
from qsim.engine.portfolio_runner import Sleeve, run_portfolio_backtest
from qsim.market.data_source import SqliteDataSource
from qsim.strategies.builtin.dual_ma import DualMAStrategy
from qsim.strategies.builtin.grid_atr import GridATRStrategy
from qsim.strategies.builtin.rsi_boll import RsiBollStrategy
from qsim.strategies.builtin.tsmom import TsmomVolStrategy

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "quant.db"

START = "20230101"
END = "20251231"

ETF_COST = CostModel(
    commission_rate=0.0001,
    min_commission=0.1,
    stamp_tax=0.0,
    slippage=0.0005,
)


def main() -> None:
    source = SqliteDataSource(DB_PATH, table="bars", symbol_column="symbol")
    sleeves = [
        Sleeve("tsmom_vol", "513100", TsmomVolStrategy()),
        Sleeve("dual_ma", "510300", DualMAStrategy()),
        Sleeve("grid_atr", "512100", GridATRStrategy()),
        Sleeve("rsi_boll", "518880", RsiBollStrategy()),
    ]

    result = run_portfolio_backtest(
        sleeves=sleeves,
        policy=default_policy(),
        data_source=source,
        regime_detector=SimpleRegimeDetector(),
        initial_cash=100000.0,
        cost=ETF_COST,
        start=START,
        end=END,
        market_symbol="510300",
        assumed_alpha=0.02,
    )

    metrics = result["metrics"]
    equity = result["equity"]
    print("=== Portfolio Backtest ===")
    print(f"period: {START}-{END}")
    print(f"bars: {len(equity)}")
    print(f"trades: {len(result['trades'])}")
    print(f"skips: {len(result['skips'])}")
    print(f"final_value: {metrics['final_value']:.2f}")
    print(f"total_return: {metrics['total_return']:.4%}")
    print(f"annual_return: {metrics['annual_return']:.4%}")
    print(f"max_drawdown: {metrics['max_drawdown']:.4%}")
    print(f"sharpe: {metrics['sharpe']:.4f}")
    print()
    print("equity tail:")
    print(equity.tail(5).to_string(index=False))


if __name__ == "__main__":
    main()
