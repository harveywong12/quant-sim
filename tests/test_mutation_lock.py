import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from qsim.services.mutation_lock import MutationBusy, mutation_guard, mutation_status


def test_nested_guard_and_other_thread_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("QSIM_MUTATION_LOCK", str(tmp_path / "writer.lock"))
    def competing():
        with mutation_guard("other"):
            pytest.fail("competing writer entered")
    with mutation_guard("outer"):
        with mutation_guard("inner"):
            assert mutation_status()["running"]
        with ThreadPoolExecutor(1) as executor:
            with pytest.raises(MutationBusy):
                executor.submit(competing).result()
    assert mutation_status() == {"running": False}


def test_other_process_rejected_and_lock_released_after_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("QSIM_MUTATION_LOCK", str(tmp_path / "writer.lock"))
    with pytest.raises(ValueError):
        with mutation_guard("writer"):
            proc = subprocess.run([sys.executable, "-c",
                "from qsim.services.mutation_lock import mutation_guard; "
                "\nwith mutation_guard('child'): pass"], capture_output=True, text=True)
            assert proc.returncode != 0 and "MutationBusy" in proc.stderr
            raise ValueError("injected")
    with mutation_guard("next"):
        assert mutation_status()["running"]
