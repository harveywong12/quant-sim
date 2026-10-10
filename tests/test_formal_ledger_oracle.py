"""Hand-written monetary ledger independent of the implementation under test."""
import json,sqlite3
import pytest
from audit_tools.formal_ledger_oracle import validate
from qsim.storage.paper import SCHEMA,seed_accounts
from qsim.services.distributions import SCHEMA as INPUT_SCHEMA


def ledger():
 c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;c.executescript(SCHEMA+INPUT_SCHEMA)
 c.executescript('CREATE TABLE stock_execution_bars(ts_code,date,open,close); CREATE TABLE bars(symbol,date,open,close);')
 seed_accounts(c,{'s':{'symbol':None,'initial':2000}})
 symbol='000001.SZ'
 provider={'ts_code':symbol,'div_proc':'实施','record_date':'20260109','ex_date':'20260112','pay_date':'20260114','cash_div_tax':1,'cash_div':.8}
 c.execute('INSERT INTO qsim_distribution_observations VALUES(?,?,?,?,?)',(symbol,'dividend','h','x',json.dumps(provider)))
 for day,open_,close,cash,receivable,tax,nav in [
  ('20260108',10,10,994.99,0,0,1994.99),
  ('20260109',10,10,994.99,0,0,1994.99),
  ('20260112',9,9,994.99,100,20,1974.99),
  ('20260114',9,9,1094.99,0,20,1974.99),
  ('20260115',9,9,1969.53,0,0,1969.53)]:
  c.execute('INSERT INTO stock_execution_bars VALUES(?,?,?,?)',(symbol,day,open_,close))
  c.execute('INSERT INTO qsim_execution_limits VALUES(?,?,?,?,?)',(symbol,day,8,12,'x'))
  position=100*close if day!='20260115' else 0
  c.execute('INSERT INTO qsim_snapshots(date,strategy,total_value,cash,position_value) VALUES(?,?,?,?,?)',(day,'s',nav,cash,position))
  c.execute('INSERT INTO qsim_distribution_nav VALUES(?,?,?,?)',('s',day,receivable,tax))
 c.execute('INSERT INTO qsim_trades(date,strategy,symbol,side,price,shares,amount,fee) VALUES(?,?,?,?,?,?,?,?)',('20260108','s',symbol,'BUY',10,100,1000,5.01))
 c.execute('INSERT INTO qsim_trades(date,strategy,symbol,side,price,shares,amount,fee) VALUES(?,?,?,?,?,?,?,?)',('20260115','s',symbol,'SELL',9,100,900,5.459))
 key=symbol+':20260109:20260112'
 for kind,day,amount,suffix in [('RECORD_ENTITLEMENT','20260109','0',''),('EX_RECEIVABLE','20260112','0',''),('CASH_PAYMENT','20260114','100.00',''),('DIVIDEND_TAX','20260115','-20.00','20260115')]:
  c.execute('INSERT INTO qsim_distribution_events VALUES(?,?,?,?,?,?,?)',('s',key+':'+kind+':'+day+':'+suffix,day,key,symbol,kind,amount))
 c.execute('UPDATE qsim_accounts SET cash=1969.53,shares=0')
 return c


def test_independent_oracle_accepts_hand_calculated_cash_tax_and_nav():
 c=ledger();report=validate(c,['s'],'20260101','20260115','20260115')
 assert report['ok'],report['issues']
 assert report['ten_year_target']=='NOT_VERIFIED'


@pytest.mark.parametrize('mutation',[
 "UPDATE qsim_trades SET fee=0 WHERE side='SELL'",
 "UPDATE qsim_trades SET shares=200,amount=2000 WHERE side='BUY'",
 "DELETE FROM qsim_distribution_events WHERE kind='CASH_PAYMENT'",
 "UPDATE qsim_distribution_nav SET dividend_tax_liability=0",
 "UPDATE qsim_snapshots SET total_value=total_value+100",
 "UPDATE stock_execution_bars SET open=11 WHERE date='20260108'",
 "UPDATE qsim_execution_limits SET upper=10 WHERE date='20260108'",
])
def test_independent_oracle_rejects_business_corruption(mutation):
 c=ledger();c.execute(mutation)
 assert not validate(c,['s'],'20260101','20260115','20260115')['ok']
