#!/usr/bin/env python3
"""Quarterly review summary based on the experiment registry.

Loads per-config equity curves written by auto_scan (artifacts/scan_equity/)
when available, so decisions are annotated with bootstrap return CIs and the
scan-level PBO.  Without equity curves the summary falls back to the plain
threshold review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from qsim.governance.quarterly_review import summarize_experiments

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_LOG = ROOT / "artifacts" / "experiments.jsonl"
EQUITY_DIR = ROOT / "artifacts" / "scan_equity"


def _load_returns() -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    if not EQUITY_DIR.is_dir():
        return out
    for path in sorted(EQUITY_DIR.glob("*.csv")):
        frame = pd.read_csv(path)
        config = path.stem.split("__")[0]
        out[config] = frame["total_value"].pct_change().dropna()
    return out


def main() -> None:
    if not EXPERIMENT_LOG.is_file():
        print("no experiment log found")
        return

    records = []
    with EXPERIMENT_LOG.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    returns_by_config = _load_returns()
    results = summarize_experiments(records, returns_by_config=returns_by_config)
    if not results:
        print("no auto_scan_summary records found")
        return

    pbo_value = next((d.pbo for _, _, d in results if d.pbo is not None), None)
    print("=== Quarterly Review Summary ===")
    if pbo_value is not None:
        print(f"scan PBO (overfitting): {pbo_value:.2f}")
    header = f"{'config':<18} {'avg_return':<12} {'avg_mdd':<12} {'avg_sharpe':<10} decision"
    if returns_by_config:
        header += "   return_CI"
    print(header)
    for config, metrics, decision in results:
        line = (
            f"{config:<18} {metrics['avg_return']:<12.4%} {metrics['avg_max_drawdown']:<12.4%} "
            f"{metrics['avg_sharpe']:<10.4f} {decision.action}"
        )
        if decision.return_ci is not None:
            line += f"   [{decision.return_ci[0]:.1%}, {decision.return_ci[1]:.1%}]"
        print(line)
        for reason in decision.reasons:
            print(f"    - {reason}")


if __name__ == "__main__":
    main()
