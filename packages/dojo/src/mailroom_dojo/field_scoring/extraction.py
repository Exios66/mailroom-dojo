"""Scoring one extraction: every expected field by its type, the factuality audit and the trace.

:func:`score_extraction` is the entry point. It reads every rule from the
:class:`~mailroom_dojo.profile.models.ScoringProfile` it is given: the class's field
types, label vocabularies, row variants, which fields compare by containment or are
scored by recall, the never-scored and retired keys, the judge-review band, the list
match threshold, the audit coverage and the trace capture options.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ..normalization.dates import expected_date_is_null, parse_date_parts
from ..profile.field_types import element_type, is_entity_list
from ..values.emptiness import is_empty_value, parse_json_container
from .audits import audit_list_field, audit_scalar_field, field_audit_support
from .entity_lists import EntityListScore, element_scorer, score_entity_list
from .row_variants import (
    active_row_variant,
    documented_absent_additions,
    is_unstated_marker,
    marker_from_fallback,
    variant_field_type,
)
from .scorer_registry import FieldScorerRegistry, UnknownFieldTypeError, heuristic_field_type
from .similarity_backend import EmbeddingRescue, SimilarityBackend, SimilarityFallback
from .trace_capture import TRACE_FIELDS, capture_trace

if TYPE_CHECKING:
    from ..profile.models import ScoringProfile

__all__ = [
    "CONTAINMENT_TYPES",
    "ExtractionScoreResult",
    "UnmappedFieldError",
    "field_is_ambiguous",
    "is_accepted_unscored_field",
    "is_never_scored_field",
    "score_extraction",
    "score_field",
]

UnknownFieldTypePolicy = Literal["error", "score_as_name"]

#: Field types that ``compare: containment`` turns into containment scoring.
CONTAINMENT_TYPES: frozenset[str] = frozenset({"name", "free_text", "containment"})

#: The type an unregistered field type is scored as under ``unknown_field_types="score_as_name"``.
_NAME_TYPE = "name"


class UnmappedFieldError(LookupError):
    """An expected key has no field type and the profile's ``unmapped_fields`` policy is ``error``."""


@dataclass(frozen=True)
class ExtractionScoreResult:
    """The scores of one extraction.

    ``field_scores`` holds one score per scored field (four places);
    ``overall_score`` is their mean (None when no field was scored);
    ``ambiguous_fields`` are the fields in the judge-review band;
    ``entity_list_scores`` and ``entity_list_audit`` hold the list metrics and the
    per-field factuality audits; ``trace`` is the captured confidence and reasoning.

    Reports that :meth:`to_dict` does not serialize: ``similarity_fallbacks`` (every
    time an embedding backend's answer was not used) and ``field_type_fallbacks``
    (fields whose unregistered type was scored as ``name``, by request).
    """

    class_key: str
    field_scores: dict[str, float]
    overall_score: float | None
    ambiguous_fields: list[str]
    entity_list_scores: dict[str, EntityListScore] = field(default_factory=dict)
    entity_list_audit: dict[str, dict[str, Any]] = field(default_factory=dict)
    trace: dict[str, Any] | None = None
    similarity_fallbacks: tuple[SimilarityFallback, ...] = ()
    field_type_fallbacks: dict[str, str] = field(default_factory=dict)

    @property
    def needs_judge_review(self) -> bool:
        """True when any field score is in the judge-review band."""
        return bool(self.ambiguous_fields)

    @property
    def audit_support(self) -> dict[str, Any]:
        """The audits summarized one vote per audited field (see :func:`~.audits.field_audit_support`)."""
        return field_audit_support(self.entity_list_audit)

    @property
    def overall_verified_precision(self) -> float | None:
        """Mean ``verified_precision`` of the audited fields with a prediction, four places (None when there
        is none)."""
        values = [
            audit["verified_precision"]
            for audit in self.entity_list_audit.values()
            if audit.get("n_predicted")
        ]
        return round(sum(values) / len(values), 4) if values else None

    @property
    def matcher_fallbacks(self) -> dict[str, str]:
        """``{field: reason}`` for every list field the greedy matcher scored because scipy was
        unavailable."""
        return {
            name: score.matcher_fallback
            for name, score in self.entity_list_scores.items()
            if score.matcher_fallback is not None
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize for run logs: class, field scores, overall score, ambiguous fields, list scores, audits,
        trace."""
        return {
            "doc_class": self.class_key,
            "field_scores": self.field_scores,
            "overall_score": self.overall_score,
            "ambiguous_fields": self.ambiguous_fields,
            "entity_list_scores": {name: score.to_dict() for name, score in self.entity_list_scores.items()},
            "entity_list_audit": self.entity_list_audit,
            "trace": self.trace,
        }


def field_is_ambiguous(profile: ScoringProfile, field_type: str, score: float) -> bool:
    """True when a ``field_type`` field scoring ``score`` belongs in the judge-review band.

    The field type's own band in ``thresholds.type_bands`` applies first, then its
    namespace's (``entity_list`` for ``entity_list:name``), then the global
    half-open ``ambiguous_band``.
    """
    return profile.thresholds.is_ambiguous(field_type, score)


def score_field(
    profile: ScoringProfile,
    field_type: str,
    predicted: object,
    expected: object,
    *,
    recall_coverage: bool = False,
    rescue: EmbeddingRescue | None = None,
    registry: FieldScorerRegistry | None = None,
    unknown_type_as: str | None = None,
    fallback_matcher: Literal["greedy"] | None = None,
) -> float | EntityListScore:
    """Score one field: a float for a scalar type, an :class:`EntityListScore` for an entity list.

    An unregistered type raises :class:`UnknownFieldTypeError` unless
    ``unknown_type_as`` names a registered type to score it as.
    """
    registry = registry if registry is not None else FieldScorerRegistry.for_profile(profile)
    if is_entity_list(field_type):
        return score_entity_list(
            profile,
            element_type(field_type),
            predicted,
            expected,
            recall_coverage=recall_coverage,
            rescue=rescue,
            registry=registry,
            fallback_matcher=fallback_matcher,
            unknown_type_as=unknown_type_as,
        )
    return element_scorer(registry, field_type, unknown_type_as)(predicted, expected, rescue)


def _class_key_text(class_key: object) -> str:
    """A class key as written for case-insensitive lookups: an enum's value, stripped and lowercased."""
    return str(getattr(class_key, "value", class_key) or "").strip().lower()


def is_accepted_unscored_field(
    profile: ScoringProfile, class_key: object, key: str, field_types: Mapping[str, str] | None = None
) -> bool:
    """True for a key the class accepts in a prediction but never scores, unless ``field_types`` names it.

    The class is looked up case-insensitively.
    """
    spec = profile.class_spec(_class_key_text(class_key))
    return spec is not None and key in spec.accepted_unscored_fields and key not in (field_types or {})


def is_never_scored_field(profile: ScoringProfile, key: str) -> bool:
    """True for a model trace key (``confidence``, ``reasoning``) or a key the profile never scores."""
    return key in TRACE_FIELDS or key in profile.never_scored_fields


def _mapping_or_empty(value: object, name: str) -> dict[str, Any]:
    """A falsy value as an empty dict, a mapping as a dict; anything else is a ``TypeError``."""
    if not value:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping of field names to values, got {type(value).__name__}")
    return dict(value)


def _checked_field_types(
    field_types: Mapping[str, str], registry: FieldScorerRegistry, unknown_field_types: UnknownFieldTypePolicy
) -> tuple[dict[str, str], dict[str, str]]:
    """Validate a caller's field-type map; return it as a dict with the fields that fall back to ``name``."""
    if not isinstance(field_types, Mapping):
        raise TypeError(
            f"field_types must be a mapping of field names to types, got {type(field_types).__name__}"
        )
    type_map = dict(field_types)
    not_text = {name: field_type for name, field_type in type_map.items() if not isinstance(field_type, str)}
    if not_text:
        raise TypeError(f"field_types values must be field type names; not text: {not_text!r}")
    unknown = {name: field_type for name, field_type in type_map.items() if not registry.knows(field_type)}
    if unknown and unknown_field_types == "error":
        raise UnknownFieldTypeError(
            f"field_types names unregistered types {unknown!r}; registered: {sorted(registry.scalar_types)}"
        )
    return type_map, unknown


def score_extraction(
    profile: ScoringProfile,
    class_key: str,
    predicted: Mapping[str, Any] | None,
    expected: Mapping[str, Any] | None,
    *,
    field_types: Mapping[str, str] | None = None,
    doc_text: str | None = None,
    audit_predicted: Mapping[str, Any] | None = None,
    apply_variants: bool = True,
    similarity: SimilarityBackend | None = None,
    registry: FieldScorerRegistry | None = None,
    unknown_field_types: UnknownFieldTypePolicy = "error",
    fallback_matcher: Literal["greedy"] | None = None,
) -> ExtractionScoreResult:
    """Score one document's predicted fields against its expected fields.

    **Which fields are scored.** Every expected key with a value, except trace keys,
    the profile's ``never_scored_fields``, its ``retired_fields`` and the class's
    ``accepted_unscored_fields`` (both scored only when ``field_types`` names them).
    JSON container texts are parsed on both sides. An empty expectation (None, blank,
    an empty container, "null", "n/a") is not a requirement and is skipped, except a
    row variant's documented-absent field. ``field_types=None`` takes the class's
    field types from the profile (``KeyError`` for a class the profile lacks); an
    explicit map is used as given. A key the map does not type takes the type of a
    field some row variant adds, else follows the profile's ``unmapped_fields``
    policy (typed by its name, skipped, or :class:`UnmappedFieldError`).

    **How.** A ``label`` value folds onto its vocabulary token first (an unfoldable
    value is kept as written). A missing prediction scores 0.0, except a date whose
    expectation states no date (1.0). A field with ``compare: containment`` and a
    name, free-text or containment type is scored by containment. Lists score F1, or
    recall when the field's coverage is ``recall``. Scores are rounded to four places;
    ``overall_score`` is their mean. Ambiguity is judged on the field type before the
    containment switch.

    **Row variants** (``apply_variants=True``, for expected values that are ground
    truth; see :mod:`.row_variants`). Documented-absent fields score 1.0 when the
    prediction is silent and 0.0 when it states something (nothing when there is no
    prediction at all); a key the expectation omits is added when the prediction
    states it. Field types are overridden, and an unstated marker is not scored.

    **Audit.** With ``profile.audit.enabled`` and a non-empty ``doc_text``, every field
    of the field-type map that ``audit_predicted`` (default: ``predicted``) fills is
    audited, scored or not. A label audit that fails is retried on folded labels.

    **Options.** ``similarity`` is an embedding backend for rescue (the profile must
    enable embeddings); failures are reported on the result. An unregistered type in
    ``field_types`` raises :class:`UnknownFieldTypeError` unless
    ``unknown_field_types="score_as_name"``, which scores it as ``name`` and reports
    it. ``fallback_matcher="greedy"`` lets lists use the greedy matcher when scipy is
    unavailable, reported per list.
    """
    if unknown_field_types not in ("error", "score_as_name"):
        raise ValueError(
            f"unknown_field_types must be 'error' or 'score_as_name', got {unknown_field_types!r}"
        )
    registry = registry if registry is not None else FieldScorerRegistry.for_profile(profile)
    if field_types is None:
        type_map = profile.field_types(class_key)
        field_type_fallbacks: dict[str, str] = {}
    else:
        type_map, field_type_fallbacks = _checked_field_types(field_types, registry, unknown_field_types)
    unknown_type_as = _NAME_TYPE if unknown_field_types == "score_as_name" else None
    rescue = EmbeddingRescue.from_profile(profile, similarity)

    had_prediction = bool(predicted)
    predicted = _mapping_or_empty(predicted, "predicted")
    expected = _mapping_or_empty(expected, "expected")
    audit_source = (
        predicted if audit_predicted is None else _mapping_or_empty(audit_predicted, "audit_predicted")
    )
    class_spec = profile.class_spec(class_key)
    active = active_row_variant(class_spec, expected) if apply_variants else None

    field_scores: dict[str, float] = {}
    ambiguous: list[str] = []
    entity_list_scores: dict[str, EntityListScore] = {}
    entity_list_audit: dict[str, dict[str, Any]] = {}

    expected_items = list(expected.items()) + documented_absent_additions(
        active, expected, predicted, type_map
    )
    for key, expected_value in expected_items:
        if is_never_scored_field(profile, key):
            continue
        if key in profile.retired_fields and key not in type_map:
            continue
        if is_accepted_unscored_field(profile, class_key, key, type_map):
            continue
        if is_unstated_marker(active, key, predicted):
            continue
        expected_value = parse_json_container(expected_value)
        if is_empty_value(expected_value):
            if active is not None and key in active.variant.documented_absent_fields and had_prediction:
                absent_prediction = parse_json_container(predicted.get(key))
                if isinstance(absent_prediction, (list, tuple)):
                    absent_prediction = [item for item in absent_prediction if not is_empty_value(item)]
                field_scores[key] = 1.0 if is_empty_value(absent_prediction) else 0.0
            continue

        field_type = type_map.get(key) or variant_field_type(profile, class_spec, key)
        if not field_type:
            if profile.unmapped_fields == "skip":
                continue
            if profile.unmapped_fields == "error":
                raise UnmappedFieldError(
                    f"expected field {key!r} of class {class_key!r} has no field type and the profile "
                    f"{profile.client!r} refuses unmapped fields"
                )
            field_type = heuristic_field_type(key, expected_value)
        if active is not None and key in active.variant.field_type_overrides:
            field_type = active.variant.field_type_overrides[key]
        field_spec = class_spec.field(key) if class_spec is not None else None
        type_override = field_spec.predicted_value_type_override if field_spec is not None else None
        if type_override is not None and type_override.matches(parse_json_container(predicted.get(key))):
            field_type = type_override.field_type
        review_type = field_type

        predicted_value = predicted.get(key)
        if predicted_value is not None:
            predicted_value = parse_json_container(predicted_value)
        if field_type == "label" and class_key:
            expected_value = profile.canonical_label(class_key, key, expected_value) or expected_value
            if predicted_value is not None:
                predicted_value = profile.canonical_label(class_key, key, predicted_value) or predicted_value
        read_from_fallback, fallback_value = marker_from_fallback(class_spec, key, predicted_value, predicted)
        if read_from_fallback:
            predicted_value = fallback_value

        if predicted_value is None:
            states_no_date = (
                field_type == "date"
                and parse_date_parts(str(expected_value)) is None
                and expected_date_is_null(str(expected_value))
            )
            score = 1.0 if states_no_date else 0.0
        else:
            if field_type in CONTAINMENT_TYPES and profile.compares_by_containment(class_key, key):
                field_type = "containment"
            result = score_field(
                profile,
                field_type,
                predicted_value,
                expected_value,
                recall_coverage=profile.scores_coverage_by_recall(class_key, key),
                rescue=rescue,
                registry=registry,
                unknown_type_as=unknown_type_as,
                fallback_matcher=fallback_matcher,
            )
            if isinstance(result, EntityListScore):
                result = result.named(key)
                entity_list_scores[key] = result
                score = result.score
            else:
                score = result
        score = round(score, 4)
        field_scores[key] = score
        if field_is_ambiguous(profile, review_type, score):
            ambiguous.append(key)

    if profile.audit.enabled and doc_text:
        for key, field_type in sorted(type_map.items()):
            predicted_value = parse_json_container(audit_source.get(key))
            if (
                is_empty_value(predicted_value)
                or key in entity_list_audit
                or is_never_scored_field(profile, key)
            ):
                continue
            if is_entity_list(field_type):
                entity_list_audit[key] = audit_list_field(
                    profile,
                    element_type(field_type),
                    predicted_value,
                    expected.get(key) or [],
                    doc_text,
                    rescue=rescue,
                    registry=registry,
                    unknown_type_as=unknown_type_as,
                )
                continue
            expected_value = expected.get(key)
            audit = audit_scalar_field(
                profile,
                field_type,
                predicted_value,
                expected_value,
                doc_text,
                rescue=rescue,
                registry=registry,
                unknown_type_as=unknown_type_as,
            )
            if field_type == "label" and class_key and not audit["true_items"]:
                folded_predicted = profile.canonical_label(class_key, key, predicted_value) or predicted_value
                folded_expected = profile.canonical_label(class_key, key, expected_value) or expected_value
                if (folded_predicted, folded_expected) != (predicted_value, expected_value):
                    folded = audit_scalar_field(
                        profile,
                        field_type,
                        folded_predicted,
                        folded_expected,
                        doc_text,
                        rescue=rescue,
                        registry=registry,
                        unknown_type_as=unknown_type_as,
                    )
                    if folded["true_items"]:
                        audit = folded
            entity_list_audit[key] = audit

    scores = list(field_scores.values())
    overall = round(sum(scores) / len(scores), 4) if scores else None
    return ExtractionScoreResult(
        class_key=class_key,
        field_scores=field_scores,
        overall_score=overall,
        ambiguous_fields=ambiguous,
        entity_list_scores=entity_list_scores,
        entity_list_audit=entity_list_audit,
        trace=capture_trace(predicted, profile.trace_capture, correctness=overall),
        similarity_fallbacks=rescue.fallbacks if rescue is not None else (),
        field_type_fallbacks=field_type_fallbacks,
    )
