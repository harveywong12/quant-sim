"""Hot-zone strategy loader with content-hash versioning.

A hot strategy file is loaded as a module whose name contains the file hash.
Changing the file produces a new module name, so the old version is naturally
retired without restart.  If loading fails, the failed module is removed and no
cache entry is written, giving callers a rollback point.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("qsim.hotzone")

from qsim.strategies.base import Strategy
from qsim.strategies.registry import create_builtin

HOT_DIR = Path("data/strategies")
_EVENT_DB = Path(__file__).resolve().parents[2] / "data" / "quant.db"

_cache: dict[tuple[str, str], type[Strategy]] = {}
_seen_versions: set[tuple[str, str]] = set()


def _record_version_event(name: str, digest: str) -> None:
    '''First-seen strategy versions leave an INFO event for AI review/audit.'''
    key = (name, digest)
    if key in _seen_versions:
        return
    _seen_versions.add(key)
    try:
        conn = sqlite3.connect(_EVENT_DB, timeout=10)
        conn.execute(
            "INSERT INTO event_log (ts, level, category, message) "
            "VALUES (?, 'INFO', 'strategy', ?)",
            (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             f"hot strategy version first seen: {name}@{digest[:8]}"),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("version event write failed: %s", exc)


def _content_hash(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()[:16]


def _resolve_class(module: Any) -> type[Strategy]:
    own = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, Strategy)
        and value is not Strategy
        and value.__module__ == module.__name__
    ]
    if own:
        return own[0]
    cls = getattr(module, "Strategy", None)
    if isinstance(cls, type):
        return cls
    raise ValueError("hot strategy module exposes no Strategy subclass")


def load(
    name: str,
    params: dict[str, Any] | None = None,
    hot_dir: str | Path | None = None,
) -> Strategy:
    hot_path = Path(hot_dir or HOT_DIR) / f"{name}.py"
    if not hot_path.is_file():
        return create_builtin(name, params)

    source = hot_path.read_bytes()
    digest = _content_hash(source)
    key = (name, digest)
    cls = _cache.get(key)
    if cls is None:
        modname = f"qsim_hot_{name}_{digest[:8]}"
        spec = importlib.util.spec_from_file_location(modname, hot_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(modname, None)
            raise
        cls = _resolve_class(module)
        _cache[key] = cls
        _record_version_event(name, digest)
    return cls(**(params or {}))


def snapshot(hot_dir: str | Path | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    path = Path(hot_dir or HOT_DIR)
    if not path.is_dir():
        return out
    for file in sorted(path.glob("*.py")):
        if file.name.startswith("_"):
            continue
        out[file.name] = _content_hash(file.read_bytes())
    return out


def clear_cache() -> None:
    _cache.clear()
