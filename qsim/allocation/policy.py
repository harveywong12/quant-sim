"""Regime-based allocation policy.

All strategies only produce signals.  This layer decides target weights from
regime probabilities and a per-regime strategy weight table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class RegimeAllocationPolicy:
    regime_weights: Mapping[str, Mapping[str, float]]

    def target_weights(self, regime_prob: Mapping[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for regime, prob in regime_prob.items():
            if prob <= 0:
                continue
            weights = self.regime_weights.get(regime, {})
            for strategy, weight in weights.items():
                out[strategy] = out.get(strategy, 0.0) + prob * weight
        return {name: round(weight, 6) for name, weight in out.items() if weight > 0}


DEFAULT_REGIME_WEIGHTS: dict[str, dict[str, float]] = {
    "up": {
        "tsmom_vol": 0.25,
        "rsi_boll": 0.25,
        "alpha158": 0.25,
        "trend_fallback": 0.25,
    },
    "range": {
        "tsmom_vol": 0.25,
        "rsi_boll": 0.25,
        "alpha158": 0.25,
        "trend_fallback": 0.25,
    },
    "down": {
        "tsmom_vol": 0.25,
        "rsi_boll": 0.25,
        "alpha158": 0.25,
        "trend_fallback": 0.25,
    },
}


def default_policy() -> RegimeAllocationPolicy:
    return RegimeAllocationPolicy(DEFAULT_REGIME_WEIGHTS)
