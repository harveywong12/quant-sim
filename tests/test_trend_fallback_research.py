import pandas as pd
import pytest

from qsim.engine.costs import CostModel
from qsim.engine.rotation_runner import run_rotation_backtest
from qsim.strategies.rotation import TrendFallback
from qsim.research.trend_fallback import targets, simulate, PRIMARY, FALLBACK


def bars(n=100):
    days = pd.bdate_range('2026-01-01',periods=n).strftime('%Y%m%d')
    return pd.DataFrame([{'symbol':s,'date':d,'open':p,'close':p}
                         for i,d in enumerate(days)
                         for s,p in [(PRIMARY,10+i*.02),(FALLBACK,20-i*.02)]])


def test_warmup_avoids_legacy_fallback_on_short_window():
    data = bars()
    days = sorted(data.date.unique()); start,end = days[70],days[-1]
    warm = simulate(data,targets(data,'baseline_warm'),start,end)
    cold = run_rotation_backtest({s:data[data.symbol==s] for s in [PRIMARY,FALLBACK]},
        TrendFallback(PRIMARY),100000,CostModel(stamp_tax=0),start,end)
    assert warm['trades'][0]['symbol'] == PRIMARY
    assert cold['trades'][0]['symbol'] == FALLBACK
    assert warm['trades'][0]['date'] == days[71]
    assert warm['metrics']['total_return'] > 0 > cold['metrics']['total_return']


@pytest.mark.parametrize('variant',['baseline_warm','fallback_filter','cash_exit','volatility_cap'])
def test_future_prices_cannot_change_prior_signals_or_fills(variant):
    data = bars(); dates = sorted(data.date.unique()); end=dates[85]
    changed = data.copy()
    changed.loc[changed.date > end,['open','close']] *= 10
    a = simulate(data,targets(data,variant),dates[70],end,weighted=variant=='volatility_cap')
    b = simulate(changed,targets(changed,variant),dates[70],end,weighted=variant=='volatility_cap')
    assert a['trades'] == b['trades']
    pd.testing.assert_frame_equal(a['equity'],b['equity'])


def test_both_weak_filtered_rule_holds_cash():
    data = bars()
    data.loc[data.symbol==PRIMARY,'close'] = list(range(110,10,-1))
    signal=targets(data,'fallback_filter')
    assert signal.iloc[-1]['target'] == 'CASH'
    assert signal.iloc[-1]['weight'] == 0


def test_failed_sale_does_not_orphan_the_old_holding():
    data=bars(5); days=sorted(data.date.unique())
    signal=pd.DataFrame({'date':days,'target':[PRIMARY,FALLBACK,FALLBACK,FALLBACK,FALLBACK],
                         'weight':1}).set_index('date')
    # A limit-down opening blocks the scheduled sale on day 3.
    data.loc[(data.symbol==PRIMARY)&(data.date==days[2]),'open'] = 7
    result=simulate(data,signal,days[0],days[-1])
    assert all(t['symbol'] != FALLBACK for t in result['trades'] if t['date']==days[2])
    assert result['equity'].iloc[2]['position_value'] > 0
    assert result['skips']
