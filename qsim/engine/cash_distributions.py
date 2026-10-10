"""Cash distributions for separate mainland-individual paper accounts.

Record-date holdings, ex-date receivables and pay-date cash are distinct.
Tax lots use end-of-day net changes and FIFO, not intraday gross turnover.
Unsupported share distributions fail closed; they are never rounded to zero.
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP


CENT = Decimal('0.01')


def money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('non-finite distribution amount')
    return result


def valid_date(date):
    if not isinstance(date, str) or len(date) != 8 or not date.isdigit():
        raise RuntimeError('invalid distribution date')
    dt.datetime.strptime(date, '%Y%m%d')
    return date


def anniversary(date: str, months: int) -> str:
    valid_date(date)
    year, month, day = int(date[:4]), int(date[4:6]), int(date[6:8])
    total = year * 12 + month - 1 + months
    year, month = total // 12, total % 12 + 1
    return f'{year:04d}{month:02d}{min(day, calendar.monthrange(year, month)[1]):02d}'


def dividend_tax_rate(acquired: str, transferred: str) -> Decimal:
    valid_date(acquired); valid_date(transferred)
    if transferred < acquired:
        raise ValueError('transfer precedes acquisition')
    if transferred <= anniversary(acquired, 1):
        return Decimal('.20')
    if transferred <= anniversary(acquired, 12):
        return Decimal('.10')
    return Decimal('0')


@dataclass(frozen=True)
class CashDistribution:
    key: str
    symbol: str
    kind: str
    record_date: str
    ex_date: str
    pay_date: str
    gross_per_unit: str
    share_ratio: str = '0'

    def validate_entitlement(self):
        for date in (self.record_date, self.ex_date, self.pay_date):
            valid_date(date)
        if self.kind not in ('a_share', 'mainland_etf'):
            raise RuntimeError('unsupported distribution tax profile')
        if not self.record_date <= self.ex_date <= self.pay_date:
            raise RuntimeError('distribution dates unavailable or inconsistent')
        if self.kind == 'a_share' and self.record_date <= '20150908':
            raise RuntimeError('pre-2015 or transition-date dividend tax requires another profile')
        if money(self.share_ratio) != 0:
            raise RuntimeError('share distribution requires par value, capital source and allocation evidence')
        if money(self.gross_per_unit) < 0:
            raise RuntimeError('negative cash distribution')


class CashDistributionBook:
    def __init__(self, actions=(), strict_rounding=False, rounding_policy=None):
        policy=rounding_policy or ('require_evidence' if strict_rounding else 'conservative_floor')
        if policy not in ('require_evidence','conservative_floor'):
            raise ValueError('unknown cash-payment rounding policy')
        self.rounding_policy=policy
        self.strict_rounding = policy == 'require_evidence'
        self.rounding_shortfall=Decimal('0')
        self.actions = sorted(actions, key=lambda a: (a.record_date, a.key))
        if len({a.key for a in self.actions}) != len(self.actions):
            raise ValueError('duplicate distribution identity')
        self.lots: dict[str, list[dict]] = {}
        self.entitlements: dict[str, dict] = {}
        self.events: list[dict] = []
        self.last_date = ''
        self.sequence = 0
        self.rounding_used = False

    def _event(self, action, kind, date, delta, suffix=''):
        key = f'{action.key}:{kind}:{date}:{suffix}'
        if any(e['event_key'] == key for e in self.events):
            raise RuntimeError('distribution event would be repeated')
        self.events.append({'event_key': key, 'action_key': action.key,
                            'symbol': action.symbol, 'kind': kind, 'date': date,
                            'cash_delta': str(delta)})

    def _due(self, date, inclusive):
        change = Decimal('0')
        for entitlement in self.entitlements.values():
            action = entitlement['action']
            reached = lambda day: day <= date if inclusive else day < date
            if reached(action.ex_date) and not entitlement['claimed']:
                entitlement['claimed'] = True
                self._event(action, 'EX_RECEIVABLE', action.ex_date, Decimal('0'))
            if reached(action.pay_date) and not entitlement['paid']:
                if not entitlement['claimed']:
                    raise RuntimeError('payment before ex-date')
                entitlement['paid'] = True
                gross = entitlement['gross']
                change += gross
                self._event(action, 'CASH_PAYMENT', action.pay_date, gross)
            if entitlement['paid']:
                for sale in entitlement['sales']:
                    if not sale['charged']:
                        tax = sale['tax']
                        change -= tax
                        sale['charged'] = True
                        if tax:
                            self._event(action, 'DIVIDEND_TAX', max(sale['date'], action.pay_date), -tax, sale['date'])
        return change

    def open(self, date):
        valid_date(date)
        if self.last_date and date <= self.last_date:
            raise RuntimeError('distribution book dates must advance')
        # Cash with an earlier payment date is available at today's open;
        # same-day receipts are conservatively usable after today's close.
        return float(self._due(date, False))

    def _net_sales(self, date, held):
        removed = []
        for symbol, lots in self.lots.items():
            quantity = sum(lot['quantity'] for lot in lots)
            decrease = max(0, quantity - int(held.get(symbol, 0)))
            for lot in lots:
                amount = min(decrease, lot['quantity'])
                if amount:
                    removed.append((symbol, lot['id'], lot['acquired'], amount))
                    decrease -= amount
        return removed

    def tax_reserve(self, date, held):
        """Reserve today's payable tax before spending sale proceeds on buys."""
        totals = {}
        for symbol, lot_id, acquired, quantity in self._net_sales(date, held):
            for entitlement in self.entitlements.values():
                a = entitlement['action']
                if a.kind != 'a_share' or a.symbol != symbol or a.pay_date > date:
                    continue
                units = min(quantity, entitlement['remaining'].get(lot_id, 0))
                if units:
                    totals[a.key] = totals.get(a.key, Decimal('0')) + units * money(a.gross_per_unit) * dividend_tax_rate(acquired, date)
        return float(sum((value.quantize(CENT, rounding=ROUND_HALF_UP) for value in totals.values()), Decimal('0')))

    def close(self, date, held):
        valid_date(date)
        if date <= self.last_date:
            raise RuntimeError('distribution close would repeat a business date')
        if any(int(q) != q or q < 0 for q in held.values()):
            raise RuntimeError('invalid distribution holdings')
        sales_by_action = {}
        removed = self._net_sales(date, held)
        for symbol, lot_id, acquired, quantity in removed:
            for entitlement in self.entitlements.values():
                action = entitlement['action']
                if action.kind != 'a_share' or action.symbol != symbol:
                    continue
                units = min(quantity, entitlement['remaining'].get(lot_id, 0))
                entitlement['remaining'][lot_id] = entitlement['remaining'].get(lot_id, 0) - units
                amount = units * money(action.gross_per_unit) * dividend_tax_rate(acquired, date)
                sales_by_action[action.key] = sales_by_action.get(action.key, Decimal('0')) + amount
            for lot in self.lots[symbol]:
                if lot['id'] == lot_id:
                    lot['quantity'] -= quantity
                    break
        for key, amount in sales_by_action.items():
            tax = amount.quantize(CENT, rounding=ROUND_HALF_UP)
            self.entitlements[key]['sales'].append({'date': date, 'tax': tax, 'charged': False})
        for symbol, quantity in held.items():
            lots = self.lots.setdefault(symbol, [])
            increase = int(quantity) - sum(lot['quantity'] for lot in lots)
            if increase < 0:
                raise RuntimeError('FIFO holdings mismatch')
            if increase:
                self.sequence += 1
                lots.append({'id': self.sequence, 'acquired': date, 'quantity': increase})
        for action in self.actions:
            if action.record_date != date or not held.get(action.symbol, 0):
                continue
            action.validate_entitlement()
            units = int(held[action.symbol])
            raw = units * money(action.gross_per_unit)
            # Shenzhen issuer cash payments truncate fractions of a cent.
            # Elsewhere this is an explicit conservative simulation assumption.
            gross = raw.quantize(CENT, rounding=ROUND_DOWN)
            if self.strict_rounding and gross != raw:
                raise RuntimeError('sub-cent cash entitlement requires payment-allocation evidence')
            shortfall=raw-gross
            if not Decimal(0) <= shortfall < CENT:
                raise RuntimeError("cash allocation rounding bound violated")
            self.rounding_shortfall += shortfall
            self.rounding_used |= gross != raw
            lots = self.lots[action.symbol]
            self.entitlements[action.key] = {
                'action': action, 'quantity': units, 'gross': gross,
                'remaining': {lot['id']: lot['quantity'] for lot in lots if lot['quantity']},
                'acquired': {lot['id']: lot['acquired'] for lot in lots if lot['quantity']},
                'sales': [], 'claimed': False, 'paid': False}
            self._event(action, 'RECORD_ENTITLEMENT', date, Decimal('0'))
        change = self._due(date, True)
        self.last_date = date
        return float(change)

    def valuation(self, date):
        receivable = liability = Decimal('0')
        for e in self.entitlements.values():
            a = e['action']
            if not e['claimed']:
                continue
            if not e['paid']:
                receivable += e['gross']
            if a.kind == 'a_share':
                accrued = sum((q * money(a.gross_per_unit) * dividend_tax_rate(e['acquired'][lot], date)
                               for lot, q in e['remaining'].items()), Decimal('0'))
                liability += accrued.quantize(CENT, rounding=ROUND_HALF_UP)
                liability += sum((sale['tax'] for sale in e['sales'] if not sale['charged']), Decimal('0'))
        return {'cash_receivable': float(receivable), 'dividend_tax_liability': float(liability),
                'distribution_value': float(receivable - liability)}

    def state(self):
        # A state checkpoint is supplementary; immutable event rows and market
        # actions retain enough evidence for an independent reconstruction.
        return {'last_date': self.last_date, 'lots': self.lots,
                'entitlements': {key: {**e, 'action': asdict(e['action'])}
                                 for key, e in self.entitlements.items()},
                'rounding_used': self.rounding_used,
                'cash_payment_rounding': self.rounding_policy,
                'rounding_shortfall': str(self.rounding_shortfall)}
