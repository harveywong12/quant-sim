"""Validate calendar replacements and alert before the verified range expires."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from urllib.parse import urlparse

from qsim.market import legacy_adapter as market
from qsim.services import notify
from qsim.services.mutation_lock import serialized_mutation
from qsim.services.operations import calendar_status, CN

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "artifacts" / "calendar_maintenance_state.json"


def check() -> dict:
    result = calendar_status()
    today = dt.datetime.now(CN).date().isoformat()
    try:
        previous = json.loads(STATE.read_text())
    except (OSError, ValueError):
        previous = {}
    if result["status"] in {"expiring", "expired", "invalid"} and previous.get("alert_date") != today:
        notify.notify("[qsim] 交易日历需要更新", "有效至 " + str(result.get("valid_through") or "未知")
                      + "；请使用已核实的交易所休市安排更新日历。")
        result["alert_date"] = today
    else:
        result["alert_date"] = previous.get("alert_date")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False))
    temporary.replace(STATE)
    return result


@serialized_mutation
def install(candidate: Path) -> dict:
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    start = dt.date.fromisoformat(payload["valid_from"])
    end = dt.date.fromisoformat(payload["valid_through"])
    today = dt.datetime.now(CN).date()
    closed = {dt.date.fromisoformat(value) for value in payload["closed_dates"]}
    source = payload.get("source") or {}
    domain = urlparse(str(source.get("url") or "")).hostname or ""
    if not domain.endswith(".sse.com.cn") and domain != "sse.com.cn":
        raise ValueError("calendar provenance must reference the Shanghai Stock Exchange")
    dt.date.fromisoformat(str(source["retrieved_at"])[:10])
    if not source.get("name") or not start <= today <= end or not closed:
        raise ValueError("calendar does not cover today or lacks holiday/provenance data")
    if any(day < start or day > end for day in closed):
        raise ValueError("holiday is outside calendar range")
    old = market._load_calendar_payload()
    if old:
        if end < old["valid_through"]:
            raise ValueError("calendar replacement would shorten the verified range")
        overlap_start, overlap_end = max(start, old["valid_from"]), min(end, old["valid_through"])
        prior = {dt.date.fromisoformat(day) for day in old["closed_dates"]
                 if overlap_start <= dt.date.fromisoformat(day) <= overlap_end}
        current = {day for day in closed if overlap_start <= day <= overlap_end}
        if prior != current:
            raise ValueError("calendar replacement changes already verified holiday dates")
    target = market.CALENDAR_FILE
    history = target.parent / "calendar-history"
    history.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stamp = dt.datetime.now(CN).strftime("%Y%m%d-%H%M%S")
        (history / f"{stamp}.json").write_bytes(target.read_bytes())
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(target)
    return {"ok": True, "valid_through": end.isoformat()}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "install"))
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args()
    if args.action == "install" and not args.candidate:
        parser.error("install requires --candidate")
    print(json.dumps(check() if args.action == "check" else install(args.candidate), ensure_ascii=False))
