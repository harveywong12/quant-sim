from __future__ import annotations

import pytest

from qsim.config import Config, ConfigError, load_config


def test_load_config_and_hash(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "app:\n  db_path: data/quant.db\nengines:\n  enabled: true\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.get("app.db_path") == "data/quant.db"
    assert cfg.section("engines") == {"enabled": True}
    assert len(cfg.hash) == 64


def test_hash_is_deterministic(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("a: 1\nb: [x, y]\n", encoding="utf-8")
    first = load_config(path).hash
    second = load_config(path).hash
    assert first == second


def test_missing_config_raises(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.yaml")


def test_config_section_falls_back_to_empty_dict(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("app: {}\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.section("not_present") == {}
