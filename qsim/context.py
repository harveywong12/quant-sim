"""Minimal reversible context for the hybrid core.

This module is intentionally small.  It provides the three primitives the rest of
the refactor relies on:

* ``effect``: perform a side effect and remember its inverse (LIFO rollback).
* ``set/get/subscribe``: a small reactive value store.
* ``use/dispose/reload``: instantiate, remove and hot-replace components.

It does not depend on any quant or web framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

Disposer = Callable[[], None]
Inverse = Callable[[], None]


class ContextError(RuntimeError):
    """Raised when a context operation is invalid."""


@dataclass
class _Effect:
    seq: int
    inverse: Inverse
    disposed: bool = False


class Component(Protocol):
    """A pluggable component.

    ``inject`` lists context keys the component depends on.  It is metadata for
    future dependency-aware wiring; Step 1 does not enforce availability yet.
    """

    id: str
    inject: tuple[str, ...]

    def apply(self, ctx: "Context", config: dict[str, Any]) -> Inverse | None: ...


class Fiber:
    """A live component instance owned by a context."""

    def __init__(
        self,
        ctx: "Context",
        component: Component,
        config: dict[str, Any],
        inverse: Inverse | None,
        seq: int,
    ) -> None:
        self.ctx = ctx
        self.component = component
        self.config = config
        self._inverse = inverse
        self._disposed = False
        self.seq = seq

    @property
    def id(self) -> str:
        return self.component.id

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._inverse is not None:
            self._inverse()
        self.ctx._fibers.pop(self.id, None)


class Context:
    """Unified runtime context."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self._subscribers: dict[str, list[Callable[[str, Any, Any], None]]] = {}
        self._effects: list[_Effect] = []
        self._fibers: dict[str, Fiber] = {}
        self._seq = 0

    # -- reversible effects -------------------------------------------------

    def effect(
        self,
        apply: Callable[[], Inverse | None] | Callable[[], None],
        inverse: Inverse | None = None,
    ) -> Disposer:
        """Run a side effect and return a disposer.

        If ``inverse`` is omitted, the return value of ``apply()`` is used as the
        inverse when callable; otherwise a no-op inverse is registered.
        """
        if inverse is None:
            result = apply()
            resolved = result if callable(result) else (lambda: None)
        else:
            apply()
            resolved = inverse

        self._seq += 1
        record = _Effect(self._seq, resolved)
        self._effects.append(record)

        def dispose() -> None:
            if record.disposed:
                return
            record.disposed = True
            resolved()
            try:
                self._effects.remove(record)
            except ValueError:
                pass

        return dispose

    # -- reactive values ----------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def set(self, key: str, value: Any) -> None:
        old = self._store.get(key, None)
        self._store[key] = value
        self._notify(key, old, value)

    def subscribe(self, key: str, callback: Callable[[str, Any, Any], None]) -> Disposer:
        self._subscribers.setdefault(key, []).append(callback)

        def unsubscribe() -> None:
            listeners = self._subscribers.get(key)
            if listeners is None:
                return
            try:
                listeners.remove(callback)
            except ValueError:
                pass
            if not listeners:
                self._subscribers.pop(key, None)

        return unsubscribe

    def notify(self, keys: list[str]) -> None:
        for key in keys:
            if key in self._store:
                self._notify(key, self._store[key], self._store[key])

    def _notify(self, key: str, old: Any, new: Any) -> None:
        for callback in list(self._subscribers.get(key, ())):
            callback(key, old, new)

    # -- components ---------------------------------------------------------

    def use(self, component: Component, config: dict[str, Any] | None = None) -> Fiber:
        if component.id in self._fibers:
            raise ContextError(f"component already active: {component.id}")
        inverse = component.apply(self, config or {})
        self._seq += 1
        fiber = Fiber(self, component, config or {}, inverse, self._seq)
        self._fibers[component.id] = fiber
        return fiber

    def dispose(self, component_id: str) -> None:
        fiber = self._fibers.get(component_id)
        if fiber is None:
            raise ContextError(f"component not active: {component_id}")
        fiber.dispose()

    def reload(
        self,
        component_id: str,
        new_component: Component,
        new_config: dict[str, Any] | None = None,
    ) -> Fiber:
        """Hot-replace a component: dispose old fiber, then instantiate new one."""
        old = self._fibers.get(component_id)
        if old is not None:
            old.dispose()
        return self.use(new_component, new_config or {})

    def dispose_all(self) -> None:
        items: list[tuple[int, Fiber | _Effect]] = [
            (fiber.seq, fiber) for fiber in self._fibers.values()
        ]
        items.extend((record.seq, record) for record in self._effects)
        for _, item in sorted(items, key=lambda pair: pair[0], reverse=True):
            if isinstance(item, Fiber):
                item.dispose()
            elif not item.disposed:
                item.disposed = True
                item.inverse()

    @property
    def active_ids(self) -> list[str]:
        return sorted(self._fibers)

    @property
    def effect_count(self) -> int:
        return len(self._effects)
