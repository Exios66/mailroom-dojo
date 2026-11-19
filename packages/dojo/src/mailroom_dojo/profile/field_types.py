"""The grammar of field type names.

A field type is a scalar type (``name``, ``date``, ...) or an entity list,
written ``entity_list`` (elements scored as ``name``) or ``entity_list:<scalar type>``.
The scalar types are the built-in ones below plus the scorer plug-ins a profile
declares; the scorer registry maps each name to its scorer.
"""

from __future__ import annotations

__all__ = [
    "BUILTIN_SCALAR_TYPES",
    "DEFAULT_ELEMENT_TYPE",
    "ENTITY_LIST_PREFIX",
    "element_type",
    "is_entity_list",
]

#: Scalar field types every registry provides.
BUILTIN_SCALAR_TYPES: tuple[str, ...] = (
    "id",
    "date",
    "money",
    "name",
    "label",
    "free_text",
    "containment",
)

#: The list type name; ``entity_list:<scalar type>`` names the element type.
ENTITY_LIST_PREFIX = "entity_list"

#: The element type of a bare ``entity_list``.
DEFAULT_ELEMENT_TYPE = "name"


def is_entity_list(field_type: str) -> bool:
    """True for ``entity_list`` and ``entity_list:<anything>``."""
    return field_type == ENTITY_LIST_PREFIX or field_type.startswith(ENTITY_LIST_PREFIX + ":")


def element_type(field_type: str) -> str:
    """Return the element type of an entity-list type (``name`` for a bare ``entity_list``).

    Raises ``ValueError`` when ``field_type`` is not an entity-list type.
    """
    if not is_entity_list(field_type):
        raise ValueError(f"{field_type!r} is not an entity_list type")
    return field_type.split(":", 1)[1] if ":" in field_type else DEFAULT_ELEMENT_TYPE
