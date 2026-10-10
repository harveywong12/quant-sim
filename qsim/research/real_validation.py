"""Local, offline diagnostic runner for existing ETF strategies.

Reads only the explicitly supplied isolated SQLite copy. Writes private files;
never prints NAV/trades or falls back to synthetic data. This export lacks
company-action accounting and actual fee verification, so real target status
stays NOT_VERIFIED regardless of attractive metrics.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
import pandas as pd
from qsim.engine.runner import run_daily_executor
from qsim.engine.execution import ExecutionPolicy
from qsim.market.data_source import SqliteDataSource
from qsim.research.screening import SINGLES,ETF_COST


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def validate(db: Path, contract: Path, output: Path, strategy: str, start: str, end: str) -> dict:
    if output.exists():
        raise ValueError('output must be a new directory; never overwrite a baseline')
    metadata = json.loads(contract.read_text())
    if metadata.get('synthetic') is not False or metadata.get('source') != 'Tushare':
        raise ValueError('real mode requires explicit real Tushare provenance; no synthetic fallback')
    if metadata.get('volume_unit') != 'lots' or metadata.get('amount_unit') != 'thousand_cny':
        raise ValueError('input units must be independently confirmed before replay')
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('input must be an immutable standalone SQLite backup, not an active database')
    database_hash = _file_hash(db)
    code_root = Path(__file__).resolve().parents[1]
    code_hashes = {str(p.relative_to(code_root)): _file_hash(p) for p in sorted(code_root.rglob('*.py'))}
    title, symbol, factory = SINGLES[strategy]
    source = SqliteDataSource(db, volume_multiplier=100, amount_multiplier=1000)
    frame = source.get_bars(symbol, end=end)
    selected = frame[(pd.to_datetime(frame.date) >= pd.Timestamp(start)) & (pd.to_datetime(frame.date) <= pd.Timestamp(end))]
    if selected.empty:
        raise ValueError('isolated real database has no in-window bars')
    dates = pd.to_datetime(frame.date)
    if dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError('input dates must be unique and sorted')
    expected = metadata.get('expected_sessions')
    actual = [d.strftime('%Y%m%d') for d in selected.date]
    if not isinstance(expected,list) or expected != actual:
        raise ValueError('calendar gaps/extra sessions or missing expected_sessions; no acceptance')
    if not (selected[['open','high','low','close']].astype(float) > 0).all().all():
        raise ValueError('invalid real prices')
    policy = ExecutionPolicy(volume_participation=.05, record_suspensions=True)
    result = run_daily_executor(frame, factory(), 100000., ETF_COST, symbol,
        start=start, end=end, warmup=True, policy=policy)
    if _file_hash(db) != database_hash:
        raise ValueError('input database changed during replay; no baseline may be published')
    reasons = ['corporate actions/dividends are not integrated into this execution path',
               'actual commission and ETF execution-cost contract not independently verified',
               'historical price-limit/reference-price and tradability coverage not fully verified',
               'historical development intervals are not unseen out-of-sample evidence']
    if (pd.Timestamp(end)-pd.Timestamp(start)).days < 3650:
        reasons.append('requested window is shorter than ten years')
    report = {'kind':'real_local_diagnostic','strategy':strategy,'title':title,'symbol':symbol,
              'start':start,'end':end,'initial':100000.,'bars':len(selected),
              'metrics':result['metrics'],'goal_status':'NOT_VERIFIED','reasons':reasons,
              'input_contract_sha256':hashlib.sha256(contract.read_bytes()).hexdigest(),
              'out_of_sample_status':'NOT_VERIFIED',
              'database_sha256':database_hash,'code_sha256':code_hashes,
              'python':sys.version,'cost':asdict(ETF_COST),'execution_policy':asdict(policy),
              'warmup':True,'strategy_parameters':'existing factory defaults; fingerprinted source'}
    output.mkdir(parents=True,mode=0o700)
    result['equity'].to_csv(output/'nav.csv',index=False)
    (output/'trades.json').write_text(json.dumps(result['trades'],allow_nan=False))
    (output/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    for p in output.iterdir():p.chmod(0o600)
    return report


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,required=True)
    p.add_argument('--contract',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--strategy',choices=sorted(SINGLES),required=True)
    p.add_argument('--start',required=True);p.add_argument('--end',required=True)
    args=p.parse_args()
    try:
        validate(args.db,args.contract,args.output,args.strategy,args.start,args.end)
        print('Local diagnostic saved; goal NOT_VERIFIED. Keep output local.')
        return 2
    except (ValueError, OSError, KeyError, sqlite3.Error):
        # Do not expose actual input rows or provider messages on stdout.
        print('BLOCKED: inputs/provenance incomplete or invalid; inspect locally.')
        return 3


if __name__=='__main__':
    raise SystemExit(main())
