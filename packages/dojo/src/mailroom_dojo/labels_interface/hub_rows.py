"""Reference adapter: shaping mailroom-dataset Hub rows into expected values and presence expectations.

This module moves to ``mailroom_labels`` once that package lands in this workspace;
the Dojo keeps it only so parity tests can shape ground truth exactly as the old
Dojo did. Nothing in the scoring core imports it.

A Hub ``gt_fields`` value is a JSON string (or a Python dict literal, or a mapping)
holding every live class's fields, annotation statistics and a stringified
``gt_presence`` map whose statuses are ``populated``, ``not_applicable``,
``schema_documented_absence`` and ``pending_annotation``.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..profile import ScoringProfile
from ..values import is_empty_value, parse_json_container
from . import PresenceExpectation

__all__ = [
    "ABSENT_PRESENCE_STATUSES",
    "CLAUSE_LABELS_KEY",
    "PENDING_ANNOTATION",
    "PRESENCE_KEY",
    "hub_presence_statuses",
    "parse_field_values",
    "parse_hub_fields",
    "presence_expectations_from_clause_labels",
    "presence_expectations_from_hub_row",
    "scoring_fields_from_hub_row",
]

#: The Hub key holding the stringified field -> presence status map.
PRESENCE_KEY = "gt_presence"

#: The Hub key holding the category -> clause spans map (a presence input, never a field).
CLAUSE_LABELS_KEY = "cuad_clause_labels"

#: Presence statuses that mean "no expected value for this document".
ABSENT_PRESENCE_STATUSES: frozenset[str] = frozenset(
    {"not_applicable", "schema_documented_absence", "pending_annotation"}
)

#: The absent status that means "not labelled" (the field is dropped), not "labelled empty".
PENDING_ANNOTATION = "pending_annotation"

_HAS_ALPHANUMERIC = re.compile(r"[A-Za-z0-9]")


def parse_hub_fields(raw: Any) -> dict[str, Any]:
    """Parse one Hub ``gt_fields`` value into a dict whose stringified JSON containers are parsed.

    Accepts a mapping, a JSON string or a Python dict literal; a blank string gives
    ``{}``. Keys become strings; each value is parsed one level with
    :func:`~mailroom_dojo.values.parse_json_container`. Raises ``TypeError`` for any
    other input and ``ValueError`` for text that is neither JSON nor a dict literal,
    or that decodes to something other than a mapping.
    """
    if isinstance(raw, Mapping):
        parsed: Any = dict(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                raise ValueError("gt_fields is neither JSON nor a Python dict literal") from None
    else:
        raise TypeError(f"gt_fields must be a mapping or string, got {type(raw).__name__}")
    if not isinstance(parsed, Mapping):
        raise ValueError(f"gt_fields must decode to a mapping, got {type(parsed).__name__}")
    return {str(key): parse_json_container(value) for key, value in parsed.items()}


def hub_presence_statuses(fields: Mapping[str, Any] | None) -> dict[str, str]:
    """Return the row's field -> presence status map (stringified or parsed), or ``{}`` when it is missing or
    malformed."""
    raw = (fields or {}).get(PRESENCE_KEY)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def parse_field_values(record: Any) -> Any:
    """Return a new dict with string keys and each value's stringified JSON container parsed; a non-mapping is
    returned as is."""
    if not isinstance(record, Mapping):
        return record
    return {str(key): parse_json_container(value) for key, value in record.items()}


def scoring_fields_from_hub_row(
    fields: Any,
    profile: ScoringProfile,
    *,
    field_types: Mapping[str, str] | None = None,
    extra_keys: Sequence[str] = (),
    drop_unmapped: bool = True,
    class_key: str | None = None,
    subclass: Any = None,
) -> dict[str, Any]:
    """Scope a Hub row to the expected values one class is scored on.

    - drops the profile's ``never_scored_fields`` (annotation keys, the presence map);
    - with ``drop_unmapped`` and a non-empty union of ``field_types`` keys and
      ``extra_keys``, keeps only those keys;
    - a field whose presence status is ``pending_annotation`` is dropped (unlabelled);
    - a field whose status is ``not_applicable`` or ``schema_documented_absence``
      becomes ``""`` (labelled empty: a stated value stays a false positive);
    - for each row variant of ``class_key`` whose marker is derived from the
      subclass, an empty marker field is filled with the subclass token when it is
      one of the marker values (stripped and lowercased).

    Returns a new dict; parse errors from :func:`parse_hub_fields` propagate.
    """
    parsed = parse_hub_fields(fields)
    presence = hub_presence_statuses(parsed)
    allowed = set(field_types or ()) | set(extra_keys or ())
    shaped: dict[str, Any] = {}
    for key, value in parsed.items():
        if key in profile.never_scored_fields:
            continue
        if drop_unmapped and allowed and key not in allowed:
            continue
        status = presence.get(key)
        if status == PENDING_ANNOTATION:
            continue
        shaped[key] = "" if status in ABSENT_PRESENCE_STATUSES else value
    spec = profile.class_spec(class_key) if class_key is not None else None
    for variant in spec.variants if spec is not None else ():
        marker = variant.marker
        if marker.derive_from != "subclass" or not is_empty_value(shaped.get(marker.field)):
            continue
        token = str(subclass or "").strip().lower()
        if token in marker.values:
            shaped[marker.field] = token
    return shaped


def presence_expectations_from_clause_labels(labels: Any, *, field: str) -> dict[str, PresenceExpectation]:
    """Build category presence expectations from a category -> clause spans map.

    Spans are ``[{"start": int, "text": str}, ...]`` (stringified JSON accepted). A
    category with spans is expected, answered by the first span text holding an
    ASCII letter or digit; a category with no spans is not expected. A category
    whose spans are all placeholders (``"[*]"``, ``"____"``) is left out, since no
    model can quote it. ``field`` names the prediction field searched for every
    category. A non-mapping label value gives ``{}``; non-list spans count as none.
    """
    labels = parse_json_container(labels)
    if not isinstance(labels, Mapping):
        return {}
    expectations: dict[str, PresenceExpectation] = {}
    for category, spans in labels.items():
        spans = parse_json_container(spans)
        items = spans if isinstance(spans, list) else []
        answer = ""
        for span in items:
            if isinstance(span, Mapping) and span.get("text"):
                text = str(span["text"])
                if _HAS_ALPHANUMERIC.search(text):
                    answer = text
                    break
        if items and not answer:
            continue
        expectations[str(category)] = {"expected": bool(items), "answer": answer, "field": field}
    return expectations


def presence_expectations_from_hub_row(
    fields: Mapping[str, Any] | None, *, field: str
) -> dict[str, PresenceExpectation] | None:
    """Presence expectations from a parsed Hub row, or None when no category is expected.

    None also when the clause-label key carries an absent presence status (including
    ``pending_annotation``), whatever stale labels are attached, so an unlabelled or
    all-absent row never scores 1.0.
    """
    if hub_presence_statuses(fields).get(CLAUSE_LABELS_KEY) in ABSENT_PRESENCE_STATUSES:
        return None
    expectations = presence_expectations_from_clause_labels(
        (fields or {}).get(CLAUSE_LABELS_KEY), field=field
    )
    if any(entry["expected"] for entry in expectations.values()):
        return expectations
    return None
