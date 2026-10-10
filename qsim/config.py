"""Configuration loading and versioned hash.

The new core keeps the legacy YAML format as its input contract, but wraps it in
a small immutable snapshot so components can depend on ``config_hash`` instead of
re-reading files or guessing whether configuration changed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


class ConfigError(RuntimeError):
    """Raised when configuration cannot be loaded."""


class Config:
    """Read-only configuration snapshot."""

    def __init__(self, raw: dict[str, Any], path: Path | None = None) -> None:
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a mapping")
        self.raw = raw
        self.path = path
        self.hash = self._hash(raw)

    @staticmethod
    def _hash(raw: dict[str, Any]) -> str:
        canonical = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name, {})
        return value if isinstance(value, dict) else {}


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    return Config(raw, config_path)
