"""One reentrant, process-shared lock for qsim data and ledger mutations."""
from __future__ import annotations

import datetime as dt
import fcntl
import functools
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_mutex = threading.RLock()
_local = threading.local()


class MutationBusy(RuntimeError):
    pass


def lock_path() -> Path:
    return Path(os.environ.get("QSIM_MUTATION_LOCK", str(ROOT / "data" / ".qsim-mutation.lock")))


@contextmanager
def mutation_guard(job: str):
    if getattr(_local, "depth", 0):
        _local.depth += 1
        try:
            yield
        finally:
            _local.depth -= 1
        return
    if not _mutex.acquire(blocking=False):
        raise MutationBusy("another data or ledger task is running")
    handle = None
    try:
        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        path.chmod(0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MutationBusy("another data or ledger task is running") from exc
        _local.depth = 1
        handle.seek(0)
        handle.truncate()
        json.dump({"job": job, "pid": os.getpid(), "started_at":
                   dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}, handle)
        handle.flush()
        try:
            yield
        finally:
            _local.depth = 0
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        _local.depth = 0
        if handle is not None:
            handle.close()
        _mutex.release()


def serialized_mutation(func):
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        with mutation_guard(func.__module__ + "." + func.__name__):
            return func(*args, **kwargs)
    return wrapped


def mutation_status() -> dict:
    path = lock_path()
    if not path.exists():
        return {"running": False}
    with path.open("r", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                owner = json.load(handle)
            except ValueError:
                owner = {}
            return {"running": True, **owner}
        return {"running": False}
