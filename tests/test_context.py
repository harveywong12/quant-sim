from __future__ import annotations

from typing import Any, Callable

import pytest

from qsim.context import Context, ContextError, Inverse


class RecordingComponent:
    def __init__(self, component_id: str, events: list[str]) -> None:
        self.id = component_id
        self.inject = ("config",)
        self.events = events

    def apply(self, ctx: Context, config: dict[str, Any]) -> Inverse | None:
        self.events.append(f"apply:{self.id}")

        def inverse() -> None:
            self.events.append(f"dispose:{self.id}")

        return inverse


def test_effect_lifo_inverse_order() -> None:
    ctx = Context()
    events: list[str] = []

    ctx.effect(lambda: events.append("first"), lambda: events.append("undo-first"))
    ctx.effect(lambda: events.append("second"), lambda: events.append("undo-second"))

    assert events == ["first", "second"]
    ctx.dispose_all()
    assert events == ["first", "second", "undo-second", "undo-first"]


def test_effect_uses_returned_inverse() -> None:
    ctx = Context()
    events: list[str] = []

    def apply() -> Callable[[], None]:
        events.append("apply")
        return lambda: events.append("undo")

    dispose = ctx.effect(apply)
    dispose()
    assert events == ["apply", "undo"]


def test_set_get_subscribe_notify() -> None:
    ctx = Context()
    seen: list[tuple[str, Any, Any]] = []
    unsubscribe = ctx.subscribe("strategy_hash", lambda key, old, new: seen.append((key, old, new)))

    ctx.set("strategy_hash", "abc")
    ctx.set("strategy_hash", "def")

    assert ctx.get("strategy_hash") == "def"
    assert seen == [("strategy_hash", None, "abc"), ("strategy_hash", "abc", "def")]

    unsubscribe()
    ctx.set("strategy_hash", "ghi")
    assert len(seen) == 2


def test_use_dispose_component() -> None:
    ctx = Context()
    events: list[str] = []
    component = RecordingComponent("engine-a", events)

    fiber = ctx.use(component, {"name": "engine-a"})
    assert ctx.active_ids == ["engine-a"]
    assert events == ["apply:engine-a"]

    ctx.dispose("engine-a")
    assert events == ["apply:engine-a", "dispose:engine-a"]
    assert ctx.active_ids == []


def test_reload_replaces_component() -> None:
    ctx = Context()
    events: list[str] = []
    first = RecordingComponent("engine-a", events)
    second = RecordingComponent("engine-a", events)

    ctx.use(first, {})
    assert events == ["apply:engine-a"]

    ctx.reload("engine-a", second, {})
    assert events == ["apply:engine-a", "dispose:engine-a", "apply:engine-a"]
    assert len(ctx.active_ids) == 1


def test_duplicate_use_raises() -> None:
    ctx = Context()
    events: list[str] = []
    component = RecordingComponent("engine-a", events)
    ctx.use(component, {})
    with pytest.raises(ContextError):
        ctx.use(component, {})


def test_dispose_all_disposes_fibers_and_effects() -> None:
    ctx = Context()
    events: list[str] = []
    component = RecordingComponent("engine-a", events)
    ctx.use(component, {})
    ctx.effect(lambda: events.append("effect"), lambda: events.append("undo-effect"))

    ctx.dispose_all()
    assert events == ["apply:engine-a", "effect", "undo-effect", "dispose:engine-a"]
