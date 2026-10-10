"""Explicit historical replay: a new artifact, never an official ledger rewrite."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path

from qsim.services import paper_daily as paper
from qsim.services.ledger_publish import historical_differences, provenance
from qsim.services.mutation_lock import serialized_mutation
from qsim.storage.paper import SCHEMA


@serialized_mutation
def run(start: str = paper.BACKFILL_START, end: str | None = None, only: tuple[str, ...] | None = None) -> dict:
    root = Path(paper.QUANT_DB).parent.parent
    base = root / 'artifacts' / 'historical_replays'
    base.mkdir(parents=True, exist_ok=True)
    run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    temporary = base / ('.' + run_id + '.tmp')
    target = base / (run_id + '.db')
    source = sqlite3.connect(f'file:{Path(paper.QUANT_DB).resolve()}?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    stage = sqlite3.connect(temporary)
    stage.row_factory = sqlite3.Row
    token = paper._ARTIFACT_WRITES.set(False)
    try:
        stage.executescript(SCHEMA)
        columns = [row[1] for row in source.execute('PRAGMA table_info(qsim_accounts)')]
        stage.executemany(f"INSERT INTO qsim_accounts ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                          source.execute(f"SELECT {','.join(columns)} FROM qsim_accounts").fetchall())
        stage.commit()
        metadata = provenance(root, source)
        stats = paper._rebuild_paper(stage, start, end, only, market_conn=source)
        names = paper._eligible(only)
        actual_end = min(end or dt.date.today().strftime('%Y%m%d'), dt.date.today().strftime('%Y%m%d'))
        if names:
            paper._validate_staged(source, stage, names, actual_end)
        differences = historical_differences(source, stage, names)
        payload = {'ok': True, 'mode': 'isolated_historical_replay', 'run_id': run_id,
                   'created_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'start': start,
                   'end': actual_end, 'stats': stats, 'metadata': metadata,
                   'differences': differences, 'difference_sample_limit': 20, 'ledger': target.name}
        stage.execute('CREATE TABLE replay_metadata(payload_json TEXT NOT NULL)')
        stage.execute('INSERT INTO replay_metadata VALUES(?)', (json.dumps(payload, ensure_ascii=False, sort_keys=True),))
        stage.commit()
        stage.close()
        temporary.replace(target)
        report = base / (run_id + '.json')
        report.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    except BaseException:
        stage.close()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        paper._ARTIFACT_WRITES.reset(token)
        source.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', default=paper.BACKFILL_START)
    parser.add_argument('--end')
    parser.add_argument('--strategy', action='append')
    args = parser.parse_args()
    print(json.dumps(run(args.start, args.end, tuple(args.strategy) if args.strategy else None), ensure_ascii=False))
