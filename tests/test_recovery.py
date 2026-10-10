import json
import sqlite3

import pytest

from qsim.services import recovery


def fixture_root(tmp_path):
    root = tmp_path / "project"
    for relative in ("data/quant.db", "data/v2_market.db", "artifacts/scheduler_state.db"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE job_runs(id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO job_runs VALUES(1)")
    (root / "data" / "strategies").mkdir()
    (root / "data" / "strategies" / "custom.py").write_text("strategy_version = 42")
    (root / "config").mkdir()
    (root / "config" / "setting.json").write_text('{"version":1}')
    return root


def test_full_backup_restore_and_checksum_rejection(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    monkeypatch.setenv("QSIM_MUTATION_LOCK", str(tmp_path / "lock"))
    bundle = recovery.backup(root)
    restored = tmp_path / "restored"
    recovery.restore(bundle, restored)
    with sqlite3.connect(restored / "data" / "quant.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 1
    assert json.loads((restored / "config" / "setting.json").read_text()) == {"version": 1}
    assert (restored / "data/strategies/custom.py").read_text() == "strategy_version = 42"
    assert bundle.stat().st_mode & 0o777 == 0o700
    (bundle / "config" / "setting.json").write_text('tampered')
    with pytest.raises(RuntimeError, match="checksum"):
        recovery.restore(bundle, tmp_path / "second")
    assert not (tmp_path / "second").exists()


def test_restore_refuses_live_or_nonempty_target(tmp_path):
    with pytest.raises(RuntimeError, match="isolated"):
        recovery.restore(tmp_path, recovery.ROOT)
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "sentinel").write_text("keep")
    with pytest.raises(RuntimeError, match="empty"):
        recovery.restore(tmp_path, target)
    assert (target / "sentinel").read_text() == "keep"
