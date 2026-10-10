"""drift_monitor：回测下界计算、live 评估、漂移状态转换与通知去重。"""

from __future__ import annotations

import csv
import json
import sqlite3

import qsim.services.drift_monitor as dm


def _write_csv(path, values):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "cash", "position_value", "total_value"])
        for i, v in enumerate(values):
            w.writerow([f"2026{i // 28 + 1:02d}{i % 28 + 1:02d}", 0, 0, v])


def _write_db(path, strategy, values):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE qsim_snapshots (id INTEGER, date TEXT, strategy TEXT, total_value REAL)")
    for i, v in enumerate(values):
        conn.execute("INSERT INTO qsim_snapshots VALUES (?, ?, ?, ?)",
                     (i, f"2026{i // 28 + 1:02d}{i % 28 + 1:02d}", strategy, v))
    conn.commit()
    conn.close()


def _healthy(n):
    return [100000 * (1.0008 ** i) for i in range(n)]


def _setup(tmp_path, monkeypatch, live_values):
    curves = tmp_path / "curves"
    curves.mkdir()
    _write_csv(curves / "test_strat_equity.csv", _healthy(500))
    db = tmp_path / "quant.db"
    _write_db(db, "test_strat", live_values)
    monkeypatch.setattr(dm, "CURVES_DIR", curves)
    monkeypatch.setattr(dm, "_monitored_strategies", lambda: ["test_strat"])
    return db, tmp_path / "drift_state.json"


def test_floor_from_backtest_distribution(tmp_path):
    curves = tmp_path / "curves"
    curves.mkdir()
    _write_csv(curves / "x_equity.csv", _healthy(500))
    floor = dm.backtest_sharpe_floor("x", curves_dir=curves)
    assert floor is not None and floor > 0


def test_healthy_live_no_alert(tmp_path, monkeypatch):
    db, state_path = _setup(tmp_path, monkeypatch, _healthy(80))
    sent = []
    result = dm.evaluate(db_path=db, state_path=state_path,
                         notifier=lambda t, b: sent.append(t))
    assert result["events"] == [] and sent == []
    assert result["strategies"]["test_strat"]["drifting"] is False


def test_collapsing_live_triggers_once_then_quiet(tmp_path, monkeypatch):
    collapsing = _healthy(20) + [90000 * (0.995 ** i) for i in range(41)]
    db, state_path = _setup(tmp_path, monkeypatch, collapsing)
    sent = []
    r1 = dm.evaluate(db_path=db, state_path=state_path,
                     notifier=lambda t, b: sent.append(t))
    assert [e["event"] for e in r1["events"]] == ["drift_start"]
    assert len(sent) == 1
    r2 = dm.evaluate(db_path=db, state_path=state_path,
                     notifier=lambda t, b: sent.append(t))
    assert r2["events"] == [] and len(sent) == 1


def test_recovery_notifies(tmp_path, monkeypatch):
    db, state_path = _setup(tmp_path, monkeypatch, _healthy(80))
    state_path.write_text(json.dumps({"strategies": {"test_strat": {"drifting": True, "since": "2026-09-01"}}}))
    sent = []
    result = dm.evaluate(db_path=db, state_path=state_path,
                         notifier=lambda t, b: sent.append(t))
    assert [e["event"] for e in result["events"]] == ["drift_recover"]
    assert any("恢复" in t for t in sent)


def test_insufficient_data_no_evaluation(tmp_path, monkeypatch):
    db, state_path = _setup(tmp_path, monkeypatch, _healthy(30))
    sent = []
    result = dm.evaluate(db_path=db, state_path=state_path,
                         notifier=lambda t, b: sent.append(t))
    assert result["events"] == [] and sent == []
    assert "insufficient_data" in result["strategies"]["test_strat"]["status"]
