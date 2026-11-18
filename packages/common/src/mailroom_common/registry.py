"""Named plug-in registries (document classes, schemas, nodes, metrics, backends).

A :class:`Registry` is a plain instance: there is no global registry, so two tests or two
engines never share state. Names are slugs (``[a-z0-9][a-z0-9_.-]*``). Every refusal raises
:class:`RegistryError` with the registry's ``kind`` in its context:

* registering a taken name without ``replace=True`` names both objects;
* looking up a missing name lists the available names and the closest match;
* registering after :meth:`Registry.freeze` is refused;
* a plug-in entry point that fails to load is reported with the entry point named and the
  original exception chained as ``__cause__``; it is never skipped, and a failure
  registers none of the group's plug-ins.
"""

from __future__ import annotations

import difflib
import re
import threading
from collections.abc import Callable, Iterator
from importlib.metadata import entry_points
from typing import Any, Generic, TypeVar, cast, overload

from mailroom_common.errors import RegistryError

__all__ = ["Registry"]

T = TypeVar("T")
_SLUG = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_NO_OBJECT: Any = object()  # "no object passed": None is a valid object to register


class Registry(Generic[T]):
    """A name-to-object table for one kind of plug-in."""

    def __init__(self, kind: str) -> None:
        if not kind:
            raise ValueError("a registry needs a kind, e.g. 'metric'")
        self.kind = kind
        self._items: dict[str, T] = {}
        self._frozen = False
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        state = ", frozen" if self._frozen else ""
        return f"Registry({self.kind!r}, {len(self._items)} entries{state})"

    @overload
    def register(self, name: str, *, replace: bool = False) -> Callable[[T], T]: ...

    @overload
    def register(self, name: str, obj: T, *, replace: bool = False) -> T: ...

    def register(self, name: str, obj: Any = _NO_OBJECT, *, replace: bool = False) -> T | Callable[[T], T]:
        """Register ``obj`` under ``name`` and return it; without ``obj``, return a decorator.

        ``None`` is an object like any other: ``register("x", None)`` registers ``None``.
        """
        if obj is _NO_OBJECT:

            def decorator(target: T) -> T:
                self._add(name, target, replace)
                return target

            return decorator
        self._add(name, obj, replace)
        return cast(T, obj)

    def _check_name(self, name: object, **context: Any) -> str:
        if not isinstance(name, str) or not _SLUG.fullmatch(name):
            raise RegistryError(
                f"invalid {self.kind} name: use lowercase letters, digits, '_', '.' or '-', "
                "starting with a letter or digit",
                kind=self.kind,
                name=name,
                **context,
            )
        return name

    def _duplicate(self, name: str, new: object, **context: Any) -> RegistryError:
        return RegistryError(
            f"duplicate {self.kind} name {name!r}",
            kind=self.kind,
            name=name,
            existing=repr(self._items[name]),
            new=repr(new),
            **context,
        )

    def _add(self, name: str, obj: T, replace: bool) -> None:
        self._check_name(name)
        with self._lock:
            if self._frozen:
                raise RegistryError(f"{self.kind} registry is frozen", kind=self.kind, name=name)
            if name in self._items and not replace:
                raise self._duplicate(name, obj)
            self._items[name] = obj

    def get(self, name: str) -> T:
        """The object registered under ``name``; a non-string ``name`` is a RegistryError too."""
        if not isinstance(name, str):
            raise RegistryError(
                f"{self.kind} name must be a string, got {type(name).__name__}", kind=self.kind, name=name
            )
        try:
            return self._items[name]
        except KeyError:
            available = self.names()
            raise RegistryError(
                f"unknown {self.kind} {name!r}",
                kind=self.kind,
                name=name,
                available=available,
                did_you_mean=difflib.get_close_matches(name, available, n=1),
            ) from None

    def names(self) -> list[str]:
        """Registered names, sorted."""
        return sorted(self._items)

    def items(self) -> list[tuple[str, T]]:
        """``(name, object)`` pairs sorted by name."""
        return sorted(self._items.items())

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        """Refuse every later registration (idempotent)."""
        with self._lock:
            self._frozen = True

    def load_entry_points(self, group: str) -> list[str]:
        """Load and register every entry point in ``group``, in name order; return the names.

        All or nothing: every plug-in is imported and checked before any is registered, so
        a failure leaves the registry as it was. A frozen registry is refused before any
        plug-in is imported. A load failure (``SystemExit`` from a plug-in's import
        included) raises :class:`RegistryError` naming the entry point, chained to the
        original exception; an invalid or duplicate name raises one with the ``group`` and
        ``entry_point`` in its context.
        """
        if self._frozen:
            raise RegistryError(f"{self.kind} registry is frozen", kind=self.kind, group=group)
        loaded: list[tuple[str, T, dict[str, str]]] = []
        for point in sorted(entry_points(group=group), key=lambda ep: (ep.name, ep.value)):
            where = {"group": group, "entry_point": f"{point.name} = {point.value}"}
            name = self._check_name(point.name, **where)
            try:
                obj = point.load()
            except (Exception, SystemExit) as exc:
                raise RegistryError(
                    f"{self.kind} plug-in {name!r} failed to load: {type(exc).__name__}: {exc}",
                    kind=self.kind,
                    **where,
                ) from exc
            loaded.append((name, obj, where))
        with self._lock:
            if self._frozen:
                raise RegistryError(f"{self.kind} registry is frozen", kind=self.kind, group=group)
            seen: dict[str, T] = {}
            for name, obj, where in loaded:
                if name in self._items:
                    raise self._duplicate(name, obj, **where)
                if name in seen:
                    raise RegistryError(
                        f"duplicate {self.kind} name {name!r}",
                        kind=self.kind,
                        name=name,
                        existing=repr(seen[name]),
                        new=repr(obj),
                        **where,
                    )
                seen[name] = obj
            self._items.update(seen)
        return [name for name, _, _ in loaded]
