"""An immutable, hashable, insertion-ordered mapping for profile data."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any, Generic, TypeVar

__all__ = ["FrozenMapping"]

KeyType = TypeVar("KeyType")
ValueType = TypeVar("ValueType")


class FrozenMapping(Mapping[KeyType, ValueType], Generic[KeyType, ValueType]):
    """A read-only mapping that keeps insertion order and can be hashed.

    Equality follows :class:`~collections.abc.Mapping` (order-blind), and the hash
    is order-blind too, so it agrees with equality. Values must be hashable.
    There is no way to change it after construction.
    """

    __slots__ = ("_items", "_hash")

    def __init__(self, items: Mapping[KeyType, ValueType] | None = None) -> None:
        """Copy ``items`` (a mapping, or nothing for an empty mapping)."""
        object.__setattr__(self, "_items", MappingProxyType(dict(items or {})))
        object.__setattr__(self, "_hash", None)

    def __getitem__(self, key: KeyType) -> ValueType:
        """Return the value for ``key``; raise ``KeyError`` when it is absent."""
        return self._items[key]

    def __iter__(self) -> Iterator[KeyType]:
        """Iterate the keys in insertion order."""
        return iter(self._items)

    def __len__(self) -> int:
        """Return the number of keys."""
        return len(self._items)

    def __hash__(self) -> int:
        """Return an order-blind hash of the items."""
        if self._hash is None:
            object.__setattr__(self, "_hash", hash(frozenset(self._items.items())))
        return self._hash

    def __setattr__(self, name: str, value: Any) -> None:
        """Refuse every attribute assignment: the mapping is immutable."""
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        """Refuse every attribute deletion: the mapping is immutable."""
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __repr__(self) -> str:
        """Show the items like a dict."""
        return f"{type(self).__name__}({self._items!r})"
