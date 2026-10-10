"""ExperimentStore: JSONL + sqlite 双写与溯源列。"""

from __future__ import annotations

import json
import sqlite3

from qsim.research.experiment_store import ExperimentStore


def _make_store(tmp_path):
    return ExperimentStore(tmp_path / "experiments.jsonl")


def test_record_dual_writes_jsonl_and_sqlite(tmp_path):
    store = _make_store(tmp_path)
    rec = store.record({"kind": "auto_scan", "config": "c1", "sharpe": 1.5})
    assert (tmp_path / "experiments.jsonl").is_file()
    assert (tmp_path / "experiments.db").is_file()
    rows = store.query()
    assert len(rows) == 1
    assert rows[0]["kind"] == "auto_scan"
    assert rows[0]["config"] == "c1"
    # 溯源列存在（值可能为 None，取决于环境是否有 git/quant.db）
    assert "git_sha" in rows[0] and "code_hash" in rows[0]
    assert "data_fingerprint" in rows[0]
    assert rows[0]["ts"] == rec["ts"]


def test_query_filters_by_kind_and_orders_desc(tmp_path):
    store = _make_store(tmp_path)
    store.record({"kind": "nested_final", "n": 1})
    store.record({"kind": "auto_scan", "n": 2})
    store.record({"kind": "nested_final", "n": 3})
    nf = store.query(kind="nested_final")
    assert [r["n"] for r in nf] == [3, 1]
    assert all(store.query(kind="auto_scan")[0]["n"] == 2 for _ in range(1))


def test_jsonl_load_unchanged(tmp_path):
    store = _make_store(tmp_path)
    store.record({"kind": "k", "x": 1})
    loaded = store.load()
    assert loaded == [{"ts": loaded[0]["ts"], "kind": "k", "x": 1}]


def test_backfill_historical_jsonl_on_first_open(tmp_path):
    path = tmp_path / "experiments.jsonl"
    old = {"ts": "2026-01-01T00:00:00", "kind": "legacy", "v": 9}
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    store = ExperimentStore(path)
    rows = store.query()
    assert len(rows) == 1
    assert rows[0]["kind"] == "legacy"
    assert rows[0]["v"] == 9
    # 不回填第二次
    store2 = ExperimentStore(path)
    assert len(store2.query()) == 1


def test_payload_in_sqlite_is_valid_json(tmp_path):
    store = _make_store(tmp_path)
    store.record({"kind": "scan", "nested": {"a": [1, 2]}})
    conn = sqlite3.connect(tmp_path / "experiments.db")
    payload = conn.execute("SELECT payload FROM experiments").fetchone()[0]
    conn.close()
    assert json.loads(payload)["nested"]["a"] == [1, 2]
