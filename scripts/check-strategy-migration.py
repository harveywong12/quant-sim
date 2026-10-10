#!/usr/bin/env python3
"""Validate the checked-in strategy migration manifest against qsim metadata."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from qsim.strategies.pool import validate_active_pool
from qsim.strategies.registry import active_migration_manifest


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    config = root / "config" / "strategy-migration.json"
    payload = json.loads(config.read_text(encoding="utf-8"))
    expected = {str(item["name"]): item for item in payload["strategies"]}
    actual = {str(item["name"]): item for item in active_migration_manifest()}
    errors = validate_active_pool()
    if set(expected) != set(actual):
        errors.append("config and code strategy sets differ")
    for name in sorted(set(expected) & set(actual)):
        for key in ("family", "execution", "state"):
            if expected[name].get(key) != actual[name].get(key):
                errors.append(
                    f"{name}.{key}: config={expected[name].get(key)!r} "
                    f"code={actual[name].get(key)!r}"
                )
    result = {"ok": not errors, "strategies": sorted(actual), "errors": errors}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
