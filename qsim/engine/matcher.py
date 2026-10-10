"""Pure matching primitives: one lot = 100 shares, T+1 is enforced by runners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math

from qsim.engine.costs import CostModel

LOT_SIZE = 100


def minimum_order_quantity(symbol: str) -> int:
    return 200 if symbol.startswith(('688', '689')) else 100


def quantity_step(symbol: str) -> int:
    return 1 if symbol.startswith(('688', '689')) else 100


def valid_sell_quantity(symbol, quantity, available):
    if symbol.startswith(('688', '689')):
        return quantity >= 200 or (available < 200 and quantity == available)
    return quantity % 100 == 0 or quantity == available



def _invalid_inputs(cash: float, shares: int, price: float) -> str | None:
    if not all(math.isfinite(float(v)) for v in (cash, shares, price)):
        return "非有限资金、持仓或价格"
    if cash < 0 or shares < 0 or int(shares) != shares or price <= 0:
        return "无效资金、持仓或价格"
    return None



@dataclass(frozen=True, slots=True)
class Trade:
    date: str
    side: str
    price: float
    shares: int
    amount: float
    fee: float
    cash_after: float
    symbol: str
    profit: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "side": self.side,
            "price": self.price,
            "shares": self.shares,
            "amount": self.amount,
            "fee": self.fee,
            "cash_after": self.cash_after,
            "symbol": self.symbol,
            "profit": self.profit,
        }


def buy(
    cash: float,
    shares: int,
    open_price: float,
    date: str,
    cost: CostModel,
    symbol: str,
    prev_close: float | None = None,
    budget: float | None = None,
    max_pos_w: float = 0.0,
    total_value: float | None = None,
    shares_cap: int | None = None,
) -> tuple[float, int, Trade | None, str | None]:
    invalid = _invalid_inputs(cash, shares, open_price)
    if invalid:
        return cash, shares, None, invalid
    reason = cost.limit_blocked("BUY", open_price, prev_close, symbol, date=date)
    if reason:
        return cash, shares, None, reason

    fill = cost.fill_price(open_price, "BUY", symbol, date=date)
    if budget is not None and (not math.isfinite(float(budget)) or budget < 0):
        return cash, shares, None, "无效预算"
    avail = min(budget, cash) if budget is not None else cash
    if fill <= 0:
        return cash, shares, None, "滑点后价格无效"
    step, minimum = quantity_step(symbol), minimum_order_quantity(symbol)
    lots = int(avail / fill / step)
    if lots * step < minimum:
        return cash, shares, None, "资金不足一手"
    n = lots * step

    if shares_cap is not None:
        n = min(n, max(0, int(shares_cap)) // step * step)
        if n < minimum:
            return cash, shares, None, "成交量约束下不足一手"

    if max_pos_w and max_pos_w > 0 and total_value is not None:
        cap = int(max_pos_w * total_value / fill / step) * step - shares
        n = min(n, cap)
        if n < minimum:
            return cash, shares, None, "超过单票仓位上限"

    amount = fill * n
    fee = cost.calc_fee(fill, n, "BUY", symbol, date=date)
    total = amount + fee
    while total > avail and n >= minimum:
        n -= step
        if n < minimum:
            return cash, shares, None, "资金不足一手"
        amount = fill * n
        fee = cost.calc_fee(fill, n, "BUY", symbol, date=date)
        total = amount + fee

    cash -= total
    shares += n
    trade = Trade(
        date=date,
        side="BUY",
        price=fill,
        shares=n,
        amount=round(amount, 2),
        fee=round(fee, 4),
        cash_after=round(cash, 2),
        symbol=symbol,
    )
    return cash, shares, trade, None


def sell(
    cash: float,
    shares: int,
    open_price: float,
    date: str,
    cost: CostModel,
    symbol: str,
    prev_close: float | None = None,
    sell_value: float | None = None,
    max_shares: int | None = None,
    avg_cost: float = 0.0,
) -> tuple[float, int, Trade | None, str | None]:
    invalid = _invalid_inputs(cash, shares, open_price)
    if invalid:
        return cash, shares, None, invalid
    reason = cost.limit_blocked("SELL", open_price, prev_close, symbol, date=date)
    if reason:
        return cash, shares, None, reason

    fill = cost.fill_price(open_price, "SELL", symbol, date=date)
    step, minimum = quantity_step(symbol), minimum_order_quantity(symbol)
    if sell_value is not None:
        n = int(sell_value / fill / step) * step
        n = min(n, shares)
    else:
        n = shares
    if max_shares is not None:
        n = min(n, max_shares)
    if n <= 0 or not valid_sell_quantity(symbol, n, shares):
        return cash, shares, None, "可卖数量不足一手"

    amount = fill * n
    fee = cost.calc_fee(fill, n, "SELL", symbol, date=date)
    cash += amount - fee
    profit = round(amount - fee - avg_cost * n, 2) if avg_cost else None
    trade = Trade(
        date=date,
        side="SELL",
        price=fill,
        shares=n,
        amount=round(amount, 2),
        fee=round(fee, 4),
        cash_after=round(cash, 2),
        symbol=symbol,
        profit=profit,
    )
    return cash, shares - n, trade, None


def buy_exact(
    cash: float,
    shares: int,
    open_price: float,
    date: str,
    cost: CostModel,
    symbol: str,
    target_shares: int,
    prev_close: float | None = None,
) -> tuple[float, int, Trade | None, str | None]:
    invalid = _invalid_inputs(cash, shares, open_price)
    if invalid:
        return cash, shares, None, invalid
    reason = cost.limit_blocked("BUY", open_price, prev_close, symbol, date=date)
    if reason:
        return cash, shares, None, reason
    if not isinstance(target_shares, int) or target_shares < minimum_order_quantity(symbol) or target_shares % quantity_step(symbol):
        return cash, shares, None, "买入数量必须为正整手"
    fill = cost.fill_price(open_price, "BUY", symbol, date=date)
    amount = fill * target_shares
    fee = cost.calc_fee(fill, target_shares, "BUY", symbol, date=date)
    total = amount + fee
    if total > cash:
        return cash, shares, None, "现金不足"
    cash -= total
    shares += target_shares
    trade = Trade(
        date=date,
        side="BUY",
        price=fill,
        shares=target_shares,
        amount=round(amount, 2),
        fee=round(fee, 4),
        cash_after=round(cash, 2),
        symbol=symbol,
    )
    return cash, shares, trade, None


def sell_exact(
    cash: float,
    shares: int,
    open_price: float,
    date: str,
    cost: CostModel,
    symbol: str,
    target_shares: int,
    prev_close: float | None = None,
    avg_cost: float = 0.0,
) -> tuple[float, int, Trade | None, str | None]:
    invalid = _invalid_inputs(cash, shares, open_price)
    if invalid:
        return cash, shares, None, invalid
    reason = cost.limit_blocked("SELL", open_price, prev_close, symbol, date=date)
    if reason:
        return cash, shares, None, reason
    if not isinstance(target_shares, int) or target_shares <= 0 or target_shares > shares:
        return cash, shares, None, "卖出数量超过持仓或无效"
    if not valid_sell_quantity(symbol, target_shares, shares):
        return cash, shares, None, "零股须一次卖清"
    fill = cost.fill_price(open_price, "SELL", symbol, date=date)
    amount = fill * target_shares
    fee = cost.calc_fee(fill, target_shares, "SELL", symbol, date=date)
    cash += amount - fee
    profit = round(amount - fee - avg_cost * target_shares, 2) if avg_cost else None
    trade = Trade(
        date=date,
        side="SELL",
        price=fill,
        shares=target_shares,
        amount=round(amount, 2),
        fee=round(fee, 4),
        cash_after=round(cash, 2),
        symbol=symbol,
        profit=profit,
    )
    return cash, shares - target_shares, trade, None


def adjust_position(
    cash: float,
    shares: int,
    open_price: float,
    date: str,
    cost: CostModel,
    target_weight: float,
    total_value: float,
    symbol: str,
    prev_close: float | None = None,
    max_pos_w: float = 0.0,
    avg_cost: float = 0.0,
    volume_cap: int | None = None,
) -> tuple[float, int, Trade | None, str | None]:
    invalid = _invalid_inputs(cash, shares, open_price)
    if invalid or not math.isfinite(float(target_weight)) or not 0 <= target_weight <= 1 or not math.isfinite(float(total_value)) or total_value < 0:
        return cash, shares, None, invalid or "无效目标权重或净值"
    target_value = total_value * target_weight
    current_value = shares * open_price
    diff = target_value - current_value
    if abs(diff) < open_price * minimum_order_quantity(symbol) and not (diff < 0 and target_weight == 0):
        return cash, shares, None, "目标差不足一手"
    if diff > 0:
        return buy(
            cash,
            shares,
            open_price,
            date,
            cost,
            symbol,
            prev_close=prev_close,
            budget=diff,
            max_pos_w=max_pos_w,
            total_value=total_value,
            shares_cap=volume_cap,
        )
    return sell(
        cash,
        shares,
        open_price,
        date,
        cost,
        symbol,
        prev_close=prev_close,
        sell_value=-diff,
        max_shares=volume_cap,
        avg_cost=avg_cost,
    )
