"""测试隔离：调度器状态库指向临时目录，避免污染 artifacts/。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_scheduler_state(tmp_path, monkeypatch):
    monkeypatch.setenv("QSIM_SCHEDULER_STATE_PATH", str(tmp_path / "scheduler_state.db"))


@pytest.fixture(autouse=True)
def _isolate_mutation_lock_and_market_mode(tmp_path, monkeypatch):
    monkeypatch.setenv('QSIM_MUTATION_LOCK', str(tmp_path / 'mutation.lock'))
    monkeypatch.setenv('QSIM_NATIVE_MARKET', '0')
