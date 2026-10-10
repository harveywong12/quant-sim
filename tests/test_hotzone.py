from __future__ import annotations

import pytest

from qsim.strategies import hotzone

V1 = '''
from qsim.strategies.base import Strategy

class Strategy(Strategy):
    def generate(self, df):
        return "BUY"
'''

V2 = '''
from qsim.strategies.base import Strategy

class Strategy(Strategy):
    def generate(self, df):
        return "SELL"
'''


@pytest.fixture()
def hot_dir(tmp_path, monkeypatch):
    hotzone.clear_cache()
    return tmp_path


def test_hot_zone_load_and_reload(hot_dir) -> None:
    (hot_dir / "demo.py").write_text(V1, encoding="utf-8")
    first = hotzone.load("demo", {}, hot_dir=hot_dir)
    assert first.generate(None) == "BUY"

    (hot_dir / "demo.py").write_text(V2, encoding="utf-8")
    second = hotzone.load("demo", {}, hot_dir=hot_dir)
    assert second.generate(None) == "SELL"
    assert len(hotzone.snapshot(hot_dir)) == 1


def test_hot_zone_fallback_to_builtin(hot_dir) -> None:
    strategy = hotzone.load("hold", {}, hot_dir=hot_dir)
    assert strategy.generate(None) == "HOLD"


def test_hot_zone_failure_does_not_cache(hot_dir) -> None:
    (hot_dir / "bad.py").write_text("this is not python", encoding="utf-8")
    with pytest.raises(Exception):
        hotzone.load("bad", {}, hot_dir=hot_dir)
    assert ("bad", hotzone._content_hash(b"this is not python")) not in hotzone._cache
