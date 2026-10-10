"""Built-in strategy registry."""

from __future__ import annotations

from typing import Any

from qsim.strategies.base import Strategy
from qsim.strategies.builtin.bollinger import BollingerStrategy
from qsim.strategies.builtin.buy_hold import BuyHoldStrategy
from qsim.strategies.builtin.cb_dual_low import CbDualLow
from qsim.strategies.builtin.donchian import DonchianStrategy
from qsim.strategies.builtin.dual_ma import DualMAStrategy
from qsim.strategies.builtin.grid import GridStrategy
from qsim.strategies.builtin.grid_atr import GridATRStrategy
from qsim.strategies.builtin.grid_trend import GridTrendStrategy
from qsim.strategies.builtin.hold import HoldStrategy
from qsim.strategies.builtin.keltner import KeltnerStrategy
from qsim.strategies.builtin.macd import MacdStrategy
from qsim.strategies.builtin.ma_slope import MaSlopeStrategy
from qsim.strategies.builtin.rsi import RsiStrategy
from qsim.strategies.builtin.rsi_boll import RsiBollStrategy
from qsim.strategies.builtin.rsrs import RsrsStrategy
from qsim.strategies.builtin.single_ma import SingleMAStrategy
from qsim.strategies.builtin.tsmom import TsmomVolStrategy
from qsim.strategies.migration import migration_for, migration_manifest

BUILTIN: dict[str, type[Strategy]] = {
    "bollinger": BollingerStrategy,
    "buy_hold": BuyHoldStrategy,
    "cb_dual_low": CbDualLow,
    "donchian": DonchianStrategy,
    "dual_ma": DualMAStrategy,
    "grid": GridStrategy,
    "grid_atr": GridATRStrategy,
    "grid_trend": GridTrendStrategy,
    "hold": HoldStrategy,
    "keltner": KeltnerStrategy,
    "macd": MacdStrategy,
    "ma_slope": MaSlopeStrategy,
    "rsi": RsiStrategy,
    "rsi_boll": RsiBollStrategy,
    "rsrs": RsrsStrategy,
    "single_ma": SingleMAStrategy,
    "tsmom_vol": TsmomVolStrategy,
}


def create_builtin(name: str, params: dict[str, Any] | None = None) -> Strategy:
    try:
        cls = BUILTIN[name]
    except KeyError as exc:
        raise KeyError(f"unknown builtin strategy: {name}") from exc
    return cls(**(params or {}))


def migration_spec(name: str) -> dict[str, object]:
    """Return the immutable migration contract for an active strategy."""
    return migration_for(name).as_dict()


def active_migration_manifest() -> list[dict[str, object]]:
    """Return the operator-facing migration manifest for all eight strategies."""
    return migration_manifest()
