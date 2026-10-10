#!/usr/bin/env python3
"""Promote one strategy after consecutive, data-consistent shadow passes."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from qsim.strategies.migration import MigrationState, migration_for


def _reports(report_dir: Path) -> list[dict]:
    reports = []
    for path in sorted(report_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not payload.get("run_id"):
            continue
        payload["_path"] = str(path)
        reports.append(payload)
    return reports


def _strategy_passed(report: dict, name: str) -> bool:
    if report.get("all_ok") is not True:
        return False
    if report.get("fingerprints", {}).get("source_data_match") is not True:
        return False
    row = next((item for item in report.get("results", []) if item.get("name") == name), None)
    return bool(row and all(row.get(key) is True for key in (
        "canonical_ok", "metric_ok", "equity_ok", "trade_ok",
    )))


def _promotion_evidence(report_dir: Path, name: str, consecutive: int) -> list[dict]:
    latest_by_market_date: dict[str, dict] = {}
    for report in _reports(report_dir):
        # Older canonical reports used their report date; all new reports
        # persist the actual common source-market watermark.  Either way,
        # repeated runs over the same market snapshot count once.
        market_date = str(report.get("source_market_as_of") or report.get("date") or "")
        if market_date:
            latest_by_market_date[market_date] = report
    reports = [latest_by_market_date[key] for key in sorted(latest_by_market_date)]
    if len(reports) < consecutive:
        return []
    latest = reports[-consecutive:]
    return latest if all(_strategy_passed(report, name) for report in latest) else []


def _update_config(config_path: Path, name: str, evidence: list[dict], consecutive: int) -> None:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for item in payload["strategies"]:
        if item.get("name") != name:
            continue
        item["state"] = MigrationState.PAPER_ENABLED.value
        item["promotion"] = {
            "promoted_at": now,
            "consecutive_required": consecutive,
            "reports": [report["run_id"] for report in evidence],
        }
        break
    else:
        raise ValueError(f"strategy not found in config: {name}")
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, config_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("strategy")
    parser.add_argument("--consecutive", type=int, default=3)
    parser.add_argument("--apply", action="store_true", help="persist PAPER_ENABLED state")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if args.consecutive < 1:
        parser.error("--consecutive must be positive")

    spec = migration_for(args.strategy)
    root = Path(__file__).resolve().parents[1]
    report_dir = args.report_dir or root / "artifacts" / "shadow"
    config_path = args.config or root / "config" / "strategy-migration.json"
    if spec.state is MigrationState.PAPER_ENABLED:
        print(json.dumps({"ok": True, "action": "noop", "reason": "already_paper_enabled", "strategy": args.strategy}, ensure_ascii=False))
        return 0
    if spec.state in (MigrationState.RETIRED, MigrationState.HISTORICAL_ONLY):
        # Retirement is an explicit operator decision.  Never let a later
        # batch of old shadow reports resurrect a retired strategy.
        print(json.dumps({
            "ok": False, "action": "blocked", "reason": "strategy_retired",
            "strategy": args.strategy, "current_state": spec.state.value,
        }, ensure_ascii=False, sort_keys=True))
        return 0

    evidence = _promotion_evidence(report_dir, args.strategy, args.consecutive)
    result = {
        "ok": bool(evidence),
        "action": "promote" if args.apply and evidence else "eligible" if evidence else "blocked",
        "strategy": args.strategy,
        "current_state": spec.state.value,
        "consecutive_required": args.consecutive,
        "evidence": [report["run_id"] for report in evidence],
    }
    if args.apply and evidence:
        _update_config(config_path, args.strategy, evidence, args.consecutive)
        result["new_state"] = MigrationState.PAPER_ENABLED.value
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if evidence else 1


if __name__ == "__main__":
    sys.exit(main())
