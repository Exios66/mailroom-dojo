"""Row variants: rows of a class that are scored by extra rules.

A :class:`~mailroom_dojo.profile.models.RowVariant` is recognised from a row's
*expected* fields: its marker field holds one of the marker values and its guard
field (if any) holds an allowed value or, when allowed, nothing. On such a row the
extraction scorer applies the variant's rules:

- ``documented_absent_fields`` are scored as expected empty: silence is right, a
  stated value is wrong;
- ``field_type_overrides`` replace field types;
- the marker field is left unscored when the prediction states no marker value,
  neither in the marker field nor in a ``fallback_fields`` field
  (``skip_when_unstated``).

Two rules hold for every row of a class with a variant, recognised or not, because
they concern how a prediction is written: a blank predicted marker field is read
from the first fallback field that holds a marker value (:func:`marker_from_fallback`),
and a field's ``predicted_value_type_override`` applies whatever the row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..values.emptiness import is_empty_value, parse_json_container

if TYPE_CHECKING:
    from ..profile.models import ClassSpec, RowVariant, ScoringProfile

__all__ = [
    "ActiveVariant",
    "active_row_variant",
    "documented_absent_additions",
    "is_unstated_marker",
    "marker_from_fallback",
    "variant_field_type",
]


@dataclass(frozen=True)
class ActiveVariant:
    """A row variant recognised on one row, with the marker value its expected fields hold."""

    variant: RowVariant
    marker_value: str


def _token(value: object) -> str:
    """A field value as a stripped, lowercased token; ``""`` for an empty value.

    Empty is :func:`is_empty_value`: None, a blank string, a null token (``"null"``,
    ``"nan"``, ...), a float NaN and an empty container, so a missing field never reads
    as the marker ``"nan"``. A number or a boolean keeps its text (``0`` is ``"0"``), so a
    profile marker ``"0"`` matches a JSON number 0. A blank token marks no variant.
    """
    if is_empty_value(value):
        return ""
    return str(value).strip().lower()


def active_row_variant(class_spec: ClassSpec | None, expected: object) -> ActiveVariant | None:
    """Return the first variant of ``class_spec`` that the ``expected`` fields mark, or None.

    The marker field's value (a JSON text is parsed) must be one of the marker
    values; the guard field's value, when stated, must be one of the allowed values,
    and when not stated the guard must allow empty. ``expected`` must be a dict.
    """
    if class_spec is None or not isinstance(expected, dict):
        return None
    for variant in class_spec.variants:
        marker = variant.marker
        token = _token(parse_json_container(expected.get(marker.field)))
        if not token or token not in marker.values:
            continue
        guard = variant.guard
        if guard is not None:
            guard_value = parse_json_container(expected.get(guard.field))
            if is_empty_value(guard_value):
                if not guard.allow_empty:
                    continue
            elif str(guard_value).strip().lower() not in guard.allowed:
                continue
        return ActiveVariant(variant, token)
    return None


def _fallback_marker_token(variant: RowVariant, predicted: Mapping[str, Any]) -> str | None:
    """The first marker value a predicted fallback field holds, or None."""
    for field_name in variant.marker.fallback_fields:
        token = _token(predicted.get(field_name))
        if token and token in variant.marker.values:
            return token
    return None


def is_unstated_marker(active: ActiveVariant | None, key: str, predicted: object) -> bool:
    """True when ``key`` is the active variant's marker field and the prediction states no marker value.

    Only a variant with ``skip_when_unstated`` leaves its marker unscored. The
    prediction states the marker when the marker field is not empty or a fallback
    field holds a marker value.
    """
    if active is None or not isinstance(predicted, dict):
        return False
    marker = active.variant.marker
    if key != marker.field or not marker.skip_when_unstated:
        return False
    if not is_empty_value(parse_json_container(predicted.get(key))):
        return False
    return _fallback_marker_token(active.variant, predicted) is None


def marker_from_fallback(
    class_spec: ClassSpec | None, key: str, predicted_value: object, predicted: Mapping[str, Any]
) -> tuple[bool, object]:
    """Read a blank predicted marker field from its fallback fields.

    Returns ``(True, token or None)`` when ``key`` is the marker field of a variant
    of the class that has fallback fields and ``predicted_value`` is empty: the
    token is the first marker value a fallback field holds, searched variant by
    variant in profile order (variants sharing a marker field may accept different
    values), or None when no such variant finds one. Otherwise returns
    ``(False, predicted_value)`` unchanged.
    """
    if class_spec is None or not is_empty_value(predicted_value):
        return False, predicted_value
    applicable = False
    for variant in class_spec.variants:
        if key == variant.marker.field and variant.marker.fallback_fields:
            applicable = True
            token = _fallback_marker_token(variant, predicted)
            if token is not None:
                return True, token
    if applicable:
        return True, None
    return False, predicted_value


def documented_absent_additions(
    active: ActiveVariant | None,
    expected: Mapping[str, Any],
    predicted: Mapping[str, Any],
    field_types: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Expected entries ``(field, "")`` to add for documented-absent fields the expectation omits.

    Only on an active variant, only for fields the prediction states (silence on an
    omitted field earns no credit), and, when ``field_types`` is not empty, only for
    fields it names.
    """
    if active is None:
        return []
    return [
        (field_name, "")
        for field_name in active.variant.documented_absent_fields
        if field_name not in expected
        and (not field_types or field_name in field_types)
        and not is_empty_value(parse_json_container(predicted.get(field_name)))
    ]


def variant_field_type(profile: ScoringProfile, class_spec: ClassSpec | None, key: str) -> str | None:
    """The type of a field that a row variant adds, for a key a field-type map does not name.

    The class's own variants are searched first, then every class's, in profile
    order, so a variant-only field keeps its type wherever it appears.
    """
    candidates = ([class_spec] if class_spec is not None else []) + list(profile.classes)
    for spec in candidates:
        added = spec.variant_added_field(key)
        if added is not None:
            return added.field_type
    return None
