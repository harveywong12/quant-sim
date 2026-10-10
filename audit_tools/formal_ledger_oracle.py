"""Independent NAS ledger oracle. No qsim engine/strategy imports.

Provider cash fields, recorded trade cashflows, FIFO EOD tax lots, raw marks,
receivables and tax liabilities are reconstructed independently. Diagnostics
stay private; callers export only engineering PASS/BLOCKED.
"""
from collections import defaultdict
from decimal import Decimal,ROUND_HALF_UP,ROUND_DOWN
import calendar,datetime as dt,json,math

CENT=Decimal('.01')
def D(v):
 x=Decimal(str(v))
 if not x.is_finite():raise ValueError('non-finite source')
 return x

def rate(bought,sold):
 date=dt.datetime.strptime(bought,'%Y%m%d').date()
 def endpoint(months):
  n=date.year*12+date.month-1+months;y,m=divmod(n,12);m+=1
  return dt.date(y,m,min(date.day,calendar.monthrange(y,m)[1])).strftime('%Y%m%d')
 if sold<=endpoint(1):return D('.2')
 if sold<=endpoint(12):return D('.1')
 return D(0)


def actions_from_provider(c,start,end,as_of):
 actions={}
 for r in c.execute('SELECT symbol,source,payload_json FROM qsim_distribution_observations'):
  symbol,source,p=r[0],r[1],json.loads(r[2])
  if p.get('ann_date') and str(p['ann_date'])>as_of:continue
  if source=='dividend' and p.get('div_proc')!='实施':continue
  date=str(p.get('record_date') or '')
  if not start<=date<=end:continue
  gross=p.get('cash_div_tax') if source=='dividend' else p.get('div_cash')
  if gross is None:raise RuntimeError('gross basis unavailable')
  ratio=max(D(p.get('stk_div') or 0),D(p.get('stk_bo_rate') or 0)+D(p.get('stk_co_rate') or 0)) if source=='dividend' else D(0)
  if not D(gross) and not ratio:continue
  identity=(symbol,date,str(p.get('ex_date') or ''))
  a=dict(symbol=symbol,record=date,ex=str(p.get('ex_date') or ''),pay=str(p.get('pay_date') or ''),gross=D(gross),ratio=ratio,taxed=source=='dividend')
  if identity in actions and actions[identity]!=a:raise RuntimeError('source ambiguity')
  actions[identity]=a
 return actions


def validate(c,names,start,end,as_of,rounding_policy="require_evidence"):
 if rounding_policy not in ("require_evidence","conservative_floor"):raise ValueError("unrecognized rounding policy")
 issues=[]
 def fail(code,name,day=''):issues.append({'check':code,'strategy':name,'date':day})
 acts=actions_from_provider(c,start,end,as_of)
 for name in names:
  acct=c.execute('SELECT * FROM qsim_accounts WHERE strategy=?',(name,)).fetchone()
  snaps=c.execute('SELECT * FROM qsim_snapshots WHERE strategy=? ORDER BY date',(name,)).fetchall()
  if not acct or not snaps:fail('MISSING_ACCOUNT_OR_SNAPSHOTS',name);continue
  cash=D(acct['initial_capital']);held=defaultdict(int);lots=defaultdict(list);rights={};serial=0
  expected_events={};fills=defaultdict(list)
  for t in c.execute('SELECT * FROM qsim_trades WHERE strategy=? ORDER BY date,id',(name,)):fills[t['date']].append(t)
  def event(a,kind,date,change,suffix=''):
   key=f"{a['symbol']}:{a['record']}:{a['ex']}:{kind}:{date}:{suffix}"
   if key in expected_events:raise RuntimeError('oracle duplicate event')
   expected_events[key]=(date,str(change),kind)
  def process_dates(day,inclusive):
   nonlocal cash
   for e in rights.values():
    a=e['action'];due=lambda d:d<=day if inclusive else d<day
    if due(a['ex']) and not e['exed']:
     e['exed']=True;event(a,'EX_RECEIVABLE',a['ex'],D(0))
    if due(a['pay']) and not e['paid']:
     e['paid']=True;cash+=e['gross'];event(a,'CASH_PAYMENT',a['pay'],e['gross'])
    if e['paid']:
     for sale in e['sales']:
      if not sale['paid']:
       cash-=sale['tax'];sale['paid']=True
       if sale['tax']:event(a,'DIVIDEND_TAX',max(sale['day'],a['pay']),-sale['tax'],sale['day'])
  for snap in snaps:
   day=snap['date'];process_dates(day,False);sellable=dict(held)
   before=dict(held)
   for t in fills.pop(day,[]):
    sym,side,q,price=t['symbol'],t['side'],int(t['shares']),D(t['price'])
    if q<=0 or q!=t['shares'] or price<=0 or D(t['fee'])<0:fail('INVALID_FILL',name,day);continue
    star=sym.startswith(('688','689'));stock='.' in sym
    if side=='BUY':
     if (star and q<200) or (not star and q%100):fail('BUY_QUANTITY_RULE',name,day)
     cash-=D(t['amount'])+D(t['fee']);held[sym]+=q
    elif side=='SELL':
     available=sellable.get(sym,0)
     if q>available:fail('T1_QUANTITY',name,day)
     if star and q<200 and (available>=200 or q!=available):fail('STAR_SELL_QUANTITY',name,day)
     if not star and q%100 and q!=available:fail('ODD_RESIDUAL_SELL',name,day)
     sellable[sym]=available-q;cash+=D(t['amount'])-D(t['fee']);held[sym]-=q
    else:fail('INVALID_SIDE',name,day)
    if abs(D(t['amount'])-price*q)>D('.03'):fail('FILL_AMOUNT',name,day)
    table,key=('stock_execution_bars','ts_code') if stock else ('bars','symbol')
    raw=c.execute(f'SELECT open,close FROM {table} WHERE {key}=? AND date=?',(sym,day)).fetchone()
    if not raw:fail('RAW_EXECUTION_ROW_MISSING',name,day);continue
    tick=2 if stock else 3
    expected_price=round(float(raw[0])*(1.0005 if side=='BUY' else .9995),tick)
    bounds=c.execute('SELECT lower,upper FROM qsim_execution_limits WHERE symbol=? AND date=?',(sym,day)).fetchone()
    if bounds:
     expected_price=min(max(expected_price,bounds[0]),bounds[1])
    if abs(float(price)-expected_price)>1e-8:fail('RAW_OPEN_SLIPPAGE_BASIS',name,day)
    if bounds and (float(raw[0])<bounds[0]-1e-9 or float(raw[0])>bounds[1]+1e-9 or not bounds[0]<=float(price)<=bounds[1]):fail('OFFICIAL_BAND_RAW_AND_FILL',name,day)
    if not bounds or (side=='BUY' and float(raw[0])>=bounds[1]-1e-9) or (side=='SELL' and float(raw[0])<=bounds[0]+1e-9):fail('OFFICIAL_LIMIT_EXECUTION',name,day)
    amount=price*q
    fee=max(D('5') if stock else D('.1'),amount*(D('.0003') if stock else D('.0001')))
    if stock:
     fee+=amount*(D('.00002') if day<'20220429' else D('.00001'))
     if side=='SELL':fee+=amount*(D('.001') if day<'20230828' else D('.0005'))
    if abs(D(t['fee'])-fee)>D('.000051'):fail('COMMISSION_STAMP_TRANSFER_FEE',name,day)
    if cash<D('-.03'):fail('NEGATIVE_INTRADAY_CASH',name,day)
   # FIFO EOD net-change tax lots are independent of gross intraday turnover.
   tax_totals=defaultdict(lambda:D(0))
   for sym,old in before.items():
    remaining=max(0,old-held.get(sym,0))
    for lot in lots[sym]:
     n=min(remaining,lot['q']);remaining-=n;lot['q']-=n
     if not n:continue
     for identity,e in rights.items():
      a=e['action']
      if a['symbol']!=sym or not a['taxed']:continue
      taxable=min(n,e['remaining'].get(lot['id'],0));e['remaining'][lot['id']]=e['remaining'].get(lot['id'],0)-taxable
      tax_totals[identity]+=taxable*a['gross']*rate(lot['bought'],day)
   for identity,tax in tax_totals.items():rights[identity]['sales'].append({'day':day,'tax':tax.quantize(CENT,rounding=ROUND_HALF_UP),'paid':False})
   for sym,q in held.items():
    add=q-sum(l['q'] for l in lots[sym])
    if add<0:fail('FIFO_LOTS',name,day)
    if add>0:
     serial+=1;lots[sym].append({'id':serial,'bought':day,'q':add})
   for identity,a in acts.items():
    if a['record']!=day or held.get(a['symbol'],0)<=0:continue
    if a['ratio']:fail('UNSUPPORTED_SHARE_ACTION',name,day);continue
    if not a['record']<=a['ex']<=a['pay']:fail('ACTION_DATES',name,day);continue
    units=held[a['symbol']];gross=units*a['gross']
    shortfall=gross-gross.quantize(CENT,rounding=ROUND_DOWN)
    if not D(0)<=shortfall<CENT:fail('PAYMENT_ROUNDING_BOUND',name,day)
    if shortfall and rounding_policy=='require_evidence':fail('SUBCENT_PAYMENT_UNVERIFIED',name,day)
    rights[identity]={'action':a,'gross':gross.quantize(CENT,rounding=ROUND_DOWN),'exed':False,'paid':False,
                      'remaining':{l['id']:l['q'] for l in lots[a['symbol']] if l['q']},
                      'bought':{l['id']:l['bought'] for l in lots[a['symbol']] if l['q']},'sales':[]}
    event(a,'RECORD_ENTITLEMENT',day,D(0))
   process_dates(day,True)
   receivable=liability=D(0)
   for e in rights.values():
    a=e['action']
    if not e['exed']:continue
    if not e['paid']:receivable+=e['gross']
    if a['taxed']:
     subtotal=sum((q*a['gross']*rate(e['bought'][lot],day) for lot,q in e['remaining'].items()),D(0))
     liability+=subtotal.quantize(CENT,rounding=ROUND_HALF_UP)
     liability+=sum((x['tax'] for x in e['sales'] if not x['paid']),D(0))
   position=D(0)
   for sym,q in held.items():
    if q<0:fail('NEGATIVE_SHARES',name,day)
    if not q:continue
    table,key=('stock_execution_bars','ts_code') if '.' in sym else ('bars','symbol')
    mark=c.execute(f'SELECT close FROM {table} WHERE {key}=? AND date<=? ORDER BY date DESC LIMIT 1',(sym,day)).fetchone()
    if not mark:fail('RAW_MARK_MISSING',name,day);continue
    position+=q*D(mark[0])
   nav=c.execute('SELECT cash_receivable,dividend_tax_liability FROM qsim_distribution_nav WHERE strategy=? AND date=?',(name,day)).fetchone()
   if not nav or abs(receivable-D(nav[0]))>D('.001') or abs(liability-D(nav[1]))>D('.001'):fail('DISTRIBUTION_NAV_COMPONENTS',name,day)
   if abs(cash-D(snap['cash']))>D('.06'):fail('CASH_WITH_ACTIONS',name,day)
   if abs(position-D(snap['position_value']))>D('.06'):fail('RAW_POSITION_MARK',name,day)
   if abs(cash+position+receivable-liability-D(snap['total_value']))>D('.10'):fail('NAV_WITH_ACTIONS',name,day)
   if cash<D('-.03'):fail('NEGATIVE_CLOSING_CASH',name,day)
  if fills:fail('UNSETTLED_TRADES',name)
  events={r[0]:(r[1],r[2],r[3]) for r in c.execute('SELECT event_key,date,cash_delta,kind FROM qsim_distribution_events WHERE strategy=?',(name,))}
  if events!=expected_events:fail('ACTION_EVENT_IDEMPOTENCY_AND_COMPLETENESS',name)
  actual={r[0]:r[1] for r in c.execute('SELECT ts_code,shares FROM qsim_positions WHERE account_id=? AND shares>0',(acct['id'],))}
  if actual!={s:q for s,q in held.items() if q>0}:fail('FINAL_SHARES',name)
  if sum(held.values())!=acct['shares'] or abs(cash-D(acct['cash']))>D('.06'):fail('FINAL_ACCOUNT',name)
 return {'ok':bool(names) and not issues,'issues':issues,'ten_year_target':'NOT_VERIFIED','broker_statement_reconciliation':'NOT_APPLICABLE_PAPER_ONLY','cash_payment_rounding':rounding_policy,'payment_rule_precision':'SIMULATION_ASSUMPTION' if rounding_policy=='conservative_floor' else 'INTEGRAL_CENTS_ONLY'}
