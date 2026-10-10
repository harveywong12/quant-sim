"""Portfolio daily net-return metrics; no external cash flows.

Initial capital is the value before the first day's costs. CAGR uses elapsed
calendar time; sample standard deviation (ddof=1), 252 trading days/year and
zero daily risk-free return are explicit conventions. Undefined metrics are
None, never an automatic eligibility pass.
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd


def portfolio_metrics(equity: pd.DataFrame, initial: float, trade_count: int) -> dict:
    if equity.empty:
        return {}
    values = equity['total_value'].astype(float)
    if not math.isfinite(initial) or initial <= 0 or not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError('metrics require finite positive initial capital and NAV')
    dates = pd.to_datetime(equity['date'].astype(str), format='mixed')
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError('metrics require unique ordered daily dates')
    final = float(values.iloc[-1])
    elapsed_years = (dates.iloc[-1] - dates.iloc[0]).total_seconds() / (365.2425 * 86400)
    cagr = (final / initial) ** (1 / elapsed_years) - 1 if elapsed_years > 0 else None
    nav = pd.Series([initial, *values], dtype=float)
    max_drawdown = float((nav / nav.cummax() - 1).min())
    returns = values.pct_change(fill_method=None)
    returns.iloc[0] = values.iloc[0] / initial - 1
    sd = float(returns.std(ddof=1))
    sharpe = float(returns.mean() / sd * math.sqrt(252)) if len(returns) >= 2 and math.isfinite(sd) and sd > 0 else None
    return {'initial': initial, 'final_value': round(final, 2),
            'total_return': final / initial - 1, 'annual_return': cagr, 'cagr': cagr,
            'elapsed_years': elapsed_years, 'max_drawdown': max_drawdown,
            'sharpe': sharpe, 'num_trades': trade_count,
            'metric_convention': 'daily_net_rf0_ddof1_252_calendar_cagr; no_external_flows'}
