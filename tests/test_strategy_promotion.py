from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_promotion_module():
    path = Path(__file__).parents[1] / "scripts" / "promote-strategy.py"
    spec = importlib.util.spec_from_file_location("promote_strategy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_promotion_requires_consecutive_complete_reports(tmp_path: Path) -> None:
    module = _load_promotion_module()
    for index in range(3):
        payload = {
            "run_id": f"run-{index}",
            "source_market_as_of": f"2026082{index}",
            "all_ok": True,
            "fingerprints": {"source_data_match": True},
            "results": [{
                "name": "multi_factor",
                "canonical_ok": True,
                "metric_ok": True,
                "equity_ok": True,
                "trade_ok": True,
            }],
        }
        (tmp_path / f"2026082{index}.json").write_text(json.dumps(payload), encoding="utf-8")

    evidence = module._promotion_evidence(tmp_path, "multi_factor", 3)

    assert [item["run_id"] for item in evidence] == ["run-0", "run-1", "run-2"]
