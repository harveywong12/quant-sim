"""Dated account-return presentation; no execution or ledger writes."""
import datetime as dt
import json
import math


def date_key(value):
    text = str(value or '')[:10].replace('-', '')
    try:
        return dt.datetime.strptime(text, '%Y%m%d').strftime('%Y%m%d')
    except ValueError:
        return None


def finite_nav(value, *, positive=False):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 and (not positive or value > 0) else None
    except (TypeError, ValueError, OverflowError):
        return None


def previous_session(day, root):
    try:
        calendar = json.loads((root/'config/trading-calendar.json').read_text())
        first = dt.date.fromisoformat(calendar['valid_from'])
        last = dt.date.fromisoformat(calendar['valid_through'])
        closed = set(calendar['closed_dates'])
        if not first <= day <= last:
            return None, 'calendar_unavailable'
        if day.weekday() >= 5 or day.isoformat() in closed:
            return None, 'market_closed'
        candidate = day - dt.timedelta(days=1)
        while candidate >= first:
            if candidate.weekday() < 5 and candidate.isoformat() not in closed:
                return candidate.strftime('%Y%m%d'), None
            candidate -= dt.timedelta(days=1)
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return None, 'calendar_unavailable'


def account_return_state(latest, previous, marked_total, positions, quotes, now, root):
    today = now.strftime('%Y%m%d')
    last_date, prior_date = date_key(latest.get('date')), date_key(previous.get('date'))
    last_nav = finite_nav(latest.get('total_value'))
    prior_nav = finite_nav(previous.get('total_value'), positive=True)
    period = None
    if last_date and prior_date and prior_date < last_date <= today and last_nav is not None and prior_nav is not None:
        period = last_nav / prior_nav - 1
    result = {'day_return': None, 'day_return_date': None,
              'day_return_status': 'awaiting_today_settlement',
              'settled_period_return': period, 'settled_return_date': last_date,
              'settled_return_from_date': prior_date}
    expected_previous, issue = previous_session(now.date(), root)
    if issue:
        result['day_return_status'] = issue
    elif not last_date or last_nav is None or last_date > today:
        result['day_return_status'] = 'snapshot_unavailable'
    elif last_date == today:
        if prior_date == expected_previous and period is not None:
            result.update(day_return=period, day_return_date=today, day_return_status='settled')
        else:
            result['day_return_status'] = 'previous_settlement_missing'
    elif quotes.get('fresh') and date_key(quotes.get('updated_at')) == today:
        if last_date != expected_previous or last_nav <= 0:
            result['day_return_status'] = 'previous_settlement_missing'
        elif not positions or not all(date_key(p.get('price_date')) == today and
                p.get('price_source') == 'realtime' and finite_nav(p.get('market_value')) is not None for p in positions):
            result['day_return_status'] = 'quotes_incomplete'
        elif any(finite_nav(latest.get(k)) is None for k in ('cash','position_value')) or abs(
                last_nav - float(latest['cash']) - float(latest['position_value'])) > .01:
            # The simple mark excludes dividend receivables/deferred tax. Do
            # not present that incomplete valuation as a portfolio net return.
            result['day_return_status'] = 'valuation_components_missing'
        elif finite_nav(marked_total) is not None:
            result.update(day_return=float(marked_total)/last_nav-1,
                          day_return_date=today, day_return_status='intraday_estimate')
    return result
