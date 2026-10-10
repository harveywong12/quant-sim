"""A-share trading cost and limit rules shared by backtest and simulation."""

from __future__ import annotations

from dataclasses import dataclass
import math
from decimal import Decimal, ROUND_HALF_UP
import datetime as dt


def _is_etf(symbol: str) -> bool:
    return symbol.startswith(("15", "16", "18", "5"))


def _is_star_or_chinext(symbol: str) -> bool:
    return symbol.startswith(("30", "68"))


@dataclass(frozen=True, slots=True)
class CostModel:
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.0005
    slippage: float = 0.0005
    transfer_fee_rate: float = 0.00001
    flow_fee: float = 0.0
    round_fee: bool = True
    historical_rates: bool = False
    daily_limits: dict | None = None
    require_daily_limits: bool = False

    def __post_init__(self) -> None:
        for field in ('commission_rate','min_commission','stamp_tax','slippage','transfer_fee_rate','flow_fee'):
            value = getattr(self, field)
            if not math.isfinite(value) or value < 0:
                raise ValueError('cost rates must be finite and nonnegative')
        if self.slippage >= 1:
            raise ValueError('slippage must be below 1')

    @classmethod
    def from_config(cls, config: dict | None = None) -> "CostModel":
        raw = (config or {}).get("backtest", {}).get("trade_cost", {})
        defaults = cls()
        return cls(
            commission_rate=float(raw.get("commission_rate", defaults.commission_rate)),
            min_commission=float(raw.get("min_commission", defaults.min_commission)),
            stamp_tax=float(raw.get("stamp_tax", defaults.stamp_tax)),
            slippage=float(raw.get("slippage", defaults.slippage)),
            transfer_fee_rate=float(raw.get("transfer_fee_rate", defaults.transfer_fee_rate)),
            flow_fee=float(raw.get("flow_fee", defaults.flow_fee)),
            round_fee=bool(raw.get("round_fee", defaults.round_fee)),
            historical_rates=bool(raw.get("historical_rates", defaults.historical_rates)),
        )

    def price_decimals(self, symbol: str) -> int:
        return 3 if _is_etf(symbol) else 2

    def fill_price(self, price: float, side: str, symbol: str, date: str | None = None) -> float:
        p = price * (1 + self.slippage) if side == "BUY" else price * (1 - self.slippage)
        filled=round(p, self.price_decimals(symbol))
        bounds=(self.daily_limits or {}).get((date,symbol))
        if bounds is not None:
            lower,upper=bounds
            filled=min(max(filled,lower),upper)
        return filled

    def transfer_rate(self, symbol: str) -> float:
        return 0.0 if _is_etf(symbol) else self.transfer_fee_rate

    def calc_fee(
        self,
        price: float,
        shares: int,
        side: str,
        symbol: str,
        date: str | None = None,
    ) -> float:
        if side not in ("BUY", "SELL") or not math.isfinite(price) or price <= 0 or shares < 0:
            raise ValueError("invalid cost inputs")
        if shares == 0:
            return 0.0
        amount = price * shares
        commission = max(self.min_commission, amount * self.commission_rate)
        stamp_rate, transfer_rate = self.stamp_tax, self.transfer_rate(symbol)
        if self.historical_rates and date is not None and not _is_etf(symbol):
            day = str(date).replace('-', '')
            dt.datetime.strptime(day, '%Y%m%d')
            if day < '20150801':
                raise ValueError('historical fee schedule is verified only from 2015-08-01')
            stamp_rate = .001 if day < '20230828' else .0005
            transfer_rate = .00002 if day < '20220429' else .00001
        tax = amount * stamp_rate if side == "SELL" and not _is_etf(symbol) else 0.0
        transfer = amount * transfer_rate
        flow = self.flow_fee if shares > 0 else 0.0
        fee = commission + tax + transfer + flow
        return round(fee, 4) if self.round_fee else fee

    def limit_pct(self, symbol: str, date: str | None = None) -> float:
        if symbol.startswith('30') and date and date.replace('-', '') < '20200824':
            return .10
        return 0.20 if _is_star_or_chinext(symbol) else 0.10

    def limit_blocked(
        self,
        side: str,
        price: float,
        prev_close: float | None,
        symbol: str,
        date: str | None = None,
    ) -> str | None:
        official = (self.daily_limits or {}).get((date, symbol))
        if official is not None:
            lower, upper = official
            if not (math.isfinite(lower) and math.isfinite(upper) and 0 < lower < upper):
                raise RuntimeError('invalid official daily price limits')
            if price < lower - 1e-9 or price > upper + 1e-9:
                raise RuntimeError('raw opening price outside official band')
            if side == 'BUY' and price >= upper - 1e-9:
                return '开盘触及官方涨停价无法买入'
            if side == 'SELL' and price <= lower + 1e-9:
                return '开盘触及官方跌停价无法卖出'
            return None
        if self.require_daily_limits:
            raise RuntimeError('official daily price limits missing')
        if not prev_close or prev_close <= 0 or not price or price <= 0:
            return None
        pct = self.limit_pct(symbol, date)
        tick = Decimal('0.001') if _is_etf(symbol) else Decimal('0.01')
        upper = float((Decimal(str(prev_close)) * (1+Decimal(str(pct)))).quantize(tick, rounding=ROUND_HALF_UP))
        lower = float((Decimal(str(prev_close)) * (1-Decimal(str(pct)))).quantize(tick, rounding=ROUND_HALF_UP))
        if side == "BUY" and price >= upper - 1e-9:
            return f"开盘触及涨停(+{pct:.0%})无法买入"
        if side == "SELL" and price <= lower + 1e-9:
            return f"开盘触及跌停(-{pct:.0%})无法卖出"
        return None
