"""Parse an extraction field type such as ``date`` or ``entity_list:name``.

A field type is a key of the ``field_types`` dictionary, optionally followed by ``:`` and a key
of ``entity_element_types``; only a type whose ``takes_element`` attribute is true (today
``entity_list``) accepts one. Both parts must be written as canonical keys, so a schema file
never carries a spelling the scorer has to guess at.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from mailroom_dictionaries.dictionary import Dictionary, VocabularyError, shipped_dictionaries

__all__ = ["FieldType", "parse_field_type"]


@dataclass(frozen=True)
class FieldType:
    """A parsed field type; ``element`` is ``None`` when none was written."""

    base: str
    element: str | None = None

    def __str__(self) -> str:
        return self.base if self.element is None else f"{self.base}:{self.element}"


def _canonical(dictionary: Dictionary, value: str, text: str) -> str:
    key = dictionary.require(value)
    if key != value:
        raise VocabularyError(
            f"field type {text!r}: write {value!r} as the canonical key {key!r}",
            dictionary=dictionary.name,
            version=dictionary.version,
            value=value,
        )
    return key


def parse_field_type(text: object, *, dictionaries: Mapping[str, Dictionary] | None = None) -> FieldType:
    """Parse ``text`` against ``field_types`` and ``entity_element_types``.

    ``dictionaries`` defaults to the shipped set; pass a client profile's set to validate its
    schemas. Raises :class:`VocabularyError` for a non-string, an unknown or non-canonical base or
    element, an empty element, or an element on a type that takes none.
    """
    if not isinstance(text, str):
        raise VocabularyError(f"a field type must be a string, got {type(text).__name__}", value=text)
    members = shipped_dictionaries() if dictionaries is None else dictionaries
    field_types = members["field_types"]
    base_text, colon, element_text = text.partition(":")
    base = _canonical(field_types, base_text, text)
    if not colon:
        return FieldType(base)
    entry = field_types.get(base)
    assert entry is not None
    if not entry.attributes["takes_element"]:
        raise VocabularyError(
            f"field type {text!r}: {base!r} takes no element type", dictionary=field_types.name, value=text
        )
    if not element_text:
        raise VocabularyError(f"field type {text!r}: the element type after ':' is empty", value=text)
    return FieldType(base, _canonical(members["entity_element_types"], element_text, text))
