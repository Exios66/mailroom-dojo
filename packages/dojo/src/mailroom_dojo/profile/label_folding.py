"""Closed label vocabularies and the rules that fold a written label onto a token.

A label field is scored by exact match after folding: the predicted and expected
values are each mapped onto a token of the field's vocabulary first. Every
vocabulary compares labels by their compact form (lowercase letters and digits
only), and folds with one of five methods:

``exact``
    The compact forms must be equal.
``aliases``
    ``exact``, then an alias table (compact alias to token).
``inventory``
    Prefix aliases first; then ``exact``; then the alias table; then the longest
    alias of eight or more characters found inside the value; then the longest
    token the value starts with; then the longest token of four or more characters
    (three when it starts with a digit) found inside the value. The unknown token is
    never reached by the prefix and substring steps. The result must be a token.
``catalog``
    ``exact``; then a catalog lookup: a catalog key equal to the value's compact
    form, then an alias, then a catalog entry whose compact label equals the value
    or whose first eight compact characters start it.
``allowed_set``
    ``exact``; then the value as written (stripped) or a token with the same compact form.

:meth:`LabelVocabulary.fold` returns ``""`` when nothing matches, so a caller keeps
the raw value and the exact-match scorer marks it wrong. A ``catalog`` or
``allowed_set`` fold never returns the unknown token through its catalog step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from ..normalization.text import compact_label
from .frozen import FrozenMapping

__all__ = ["FOLD_METHODS", "CatalogEntry", "FoldMethod", "LabelVocabulary"]

FoldMethod = Literal["exact", "aliases", "inventory", "catalog", "allowed_set"]

#: The fold methods a vocabulary may name.
FOLD_METHODS: tuple[str, ...] = ("exact", "aliases", "inventory", "catalog", "allowed_set")

_NOT_LOWER_ALPHANUMERIC = re.compile(r"[^a-z0-9]")

#: Inventory folds match an alias inside a value only when the alias is this long.
_INVENTORY_MINIMUM_ALIAS_LENGTH = 8
#: Inventory folds match a token inside a value only when the token is this long ...
_INVENTORY_MINIMUM_TOKEN_LENGTH = 4
#: ... or this long when the token starts with a digit.
_INVENTORY_MINIMUM_NUMERIC_TOKEN_LENGTH = 3
#: Catalog folds compare this many compact characters of an entry's label.
_CATALOG_LABEL_PREFIX_LENGTH = 8


def _text_key(value: object) -> str:
    """Compact ``str(value)`` (unlike :func:`compact_label`, ``0`` and ``False`` keep their text)."""
    return _NOT_LOWER_ALPHANUMERIC.sub("", str(value).lower())


@dataclass(frozen=True)
class CatalogEntry:
    """One catalog row: a token and the human label it is known by."""

    key: str
    label: str


@dataclass(frozen=True)
class LabelVocabulary:
    """A closed set of label tokens and the method that folds written labels onto them.

    ``aliases``, ``prefix_aliases``, ``catalog`` and ``tokens`` are ordered: when two
    entries of equal length both match, the earlier one wins, so two vocabularies
    whose tables differ only in order are not equal. ``unknown_token`` is the
    residual label (``other``): an inventory fold never reaches it through a prefix
    or substring, a catalog or allowed-set fold never returns it from its catalog
    step, and :meth:`normalize` returns it when nothing folds.
    """

    tokens: tuple[str, ...]
    fold_method: FoldMethod = "exact"
    aliases: FrozenMapping[str, str] = field(default_factory=FrozenMapping)
    prefix_aliases: FrozenMapping[str, str] = field(default_factory=FrozenMapping)
    catalog: tuple[CatalogEntry, ...] = ()
    unknown_token: str = "other"
    _alias_order: tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]] = field(
        init=False, repr=False
    )
    _tokens_by_compact: FrozenMapping[str, str] = field(init=False, repr=False, compare=False, hash=False)
    _aliases_by_compact: FrozenMapping[str, str] = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        """Freeze the inputs and precompute the compact lookup tables."""
        object.__setattr__(self, "tokens", tuple(self.tokens))
        object.__setattr__(self, "aliases", FrozenMapping(self.aliases))
        object.__setattr__(self, "prefix_aliases", FrozenMapping(self.prefix_aliases))
        object.__setattr__(self, "catalog", tuple(self.catalog))
        # The alias tables compare order-blind as mappings, but their order breaks ties
        # when folding, so equality and the hash also see it.
        object.__setattr__(
            self, "_alias_order", (tuple(self.aliases.items()), tuple(self.prefix_aliases.items()))
        )
        object.__setattr__(
            self,
            "_tokens_by_compact",
            FrozenMapping({compact_label(token): token for token in self.tokens}),
        )
        object.__setattr__(
            self,
            "_aliases_by_compact",
            FrozenMapping({compact_label(alias): target for alias, target in self.aliases.items()}),
        )

    def fold(self, value: object) -> str:
        """Return the token ``value`` folds onto, or ``""`` when it folds onto none."""
        if self.fold_method == "inventory":
            return self._fold_inventory(value)
        key = compact_label(value)
        if not key:
            return ""
        exact = self._tokens_by_compact.get(key)
        if exact is not None:
            return exact
        if self.fold_method == "exact":
            return ""
        if self.fold_method == "aliases":
            return self._aliases_by_compact.get(key, "")
        token = self.normalize(value)
        return token if token in self.tokens and token != self.unknown_token else ""

    def normalize(self, value: object) -> str:
        """Return the token ``value`` stands for, or the unknown token.

        For ``catalog`` and ``allowed_set`` this is the subclass normalizer: ``None``
        or a value with no letters or digits gives the unknown token, and so does a
        value that matches nothing. For the other methods it is :meth:`fold`, with
        the unknown token in place of ``""``.
        """
        if self.fold_method == "catalog":
            return self._normalize_catalog(value)
        if self.fold_method == "allowed_set":
            return self._normalize_allowed_set(value)
        return self.fold(value) or self.unknown_token

    def _fold_inventory(self, value: object) -> str:
        """Fold through prefix aliases and the inventory search; the result must be a token."""
        key = compact_label(value)
        for prefix, target in self.prefix_aliases.items():
            if key.startswith(compact_label(prefix)):
                return target
        token = self._search_inventory(key)
        return token if token in self.tokens else ""

    def _search_inventory(self, key: str) -> str:
        """Return the inventory match for a compact key, which may be an alias target outside the tokens."""
        if not key:
            return ""
        exact = self._tokens_by_compact.get(key)
        if exact is not None:
            return exact
        alias_target = self._aliases_by_compact.get(key)
        if alias_target is not None:
            return alias_target
        ranked_aliases = sorted(self.aliases.items(), key=lambda item: -len(compact_label(item[0])))
        for alias, target in ranked_aliases:
            alias_key = compact_label(alias)
            if len(alias_key) >= _INVENTORY_MINIMUM_ALIAS_LENGTH and alias_key in key:
                return target
        ranked_tokens = sorted(self.tokens, key=lambda token: -len(compact_label(token)))
        searchable = [
            (token, compact_label(token))
            for token in ranked_tokens
            if compact_label(token) and token != self.unknown_token
        ]
        for token, token_key in searchable:
            if key.startswith(token_key):
                return token
        for token, token_key in searchable:
            if len(token_key) >= _INVENTORY_MINIMUM_TOKEN_LENGTH and token_key in key:
                return token
            if (
                len(token_key) >= _INVENTORY_MINIMUM_NUMERIC_TOKEN_LENGTH
                and token_key[:1].isdigit()
                and token_key in key
            ):
                return token
        if key == self.unknown_token:
            return self.unknown_token
        return ""

    def _normalize_catalog(self, value: object) -> str:
        """Map a value to a catalog key: key, alias, then label (whole or first eight characters).

        A value none of them maps whose compact form is a token's (a key written
        with capitals or punctuation, such as ``Co-Op``) gives that token, as
        :meth:`fold` does; anything else gives the unknown token.
        """
        if value is None:
            return self.unknown_token
        key = _text_key(value)
        if not key:
            return self.unknown_token
        if any(entry.key == key for entry in self.catalog):
            return key
        alias_target = self._aliases_by_compact.get(key)
        if alias_target is not None:
            return alias_target
        for entry in self.catalog:
            label_key = _text_key(entry.label)
            if key == label_key or key.startswith(label_key[:_CATALOG_LABEL_PREFIX_LENGTH]):
                return entry.key
        return self._tokens_by_compact.get(key, self.unknown_token)

    def _normalize_allowed_set(self, value: object) -> str:
        """Map a value to a token written the same way, or with the same compact form."""
        if value is None:
            return self.unknown_token
        raw = str(value).strip()
        key = _text_key(raw)
        if not key:
            return self.unknown_token
        if raw in self.tokens:
            return raw
        for token in self.tokens:
            if key == _text_key(token):
                return token
        return self.unknown_token
