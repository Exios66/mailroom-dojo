"""The factuality audit: is everything a model reported true?

A predicted value is *true* when it matches the ground truth (its similarity reaches
the profile's ``thresholds.list_match_threshold``) or when it is grounded in the
source document: at least ``profile.audit.token_coverage`` of its content tokens
occur in the document text. A predicted date is grounded when the document states a
date that agrees with every component the prediction states, in any format.

An audit is a plain dict with ``n_predicted``, ``matched_gt``, ``verified_in_doc``,
``true_items``, ``verified_precision``, ``hallucinated``, ``hallucination_rate`` and
``doc_verification``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from ..normalization.dates import MONTH_NAMES, DateParts, parse_date_parts
from ..normalization.text import containment_tokens
from .entity_lists import as_list, element_scorer, element_similarity
from .scalar_scorers import date_match_similarity
from .scorer_registry import FieldScorerRegistry, ScalarScorer
from .similarity_backend import EmbeddingRescue

if TYPE_CHECKING:
    import datetime

    from ..profile.models import ScoringProfile

__all__ = [
    "audit_list_field",
    "audit_scalar_field",
    "date_grounded_in_document",
    "field_audit_support",
    "verification_tokens",
    "verify_list_items",
]

# Date-like spans in a document: numeric (04-01-06, 3/24/2006), month first
# (April 1, 2006) or day first ("18th day of August, 2014", including OCR
# artifacts such as "18t h day of").
_DATE_CANDIDATE = re.compile(
    r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{1,2}[a-z]*(?:\s+[a-z]+)*\s+day\s+of\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*,?\s*\d{4}\b",
    flags=re.IGNORECASE,
)


def _empty_audit(doc_verification: bool) -> dict[str, Any]:
    """The audit of a field the model left empty."""
    return {
        "n_predicted": 0,
        "matched_gt": 0,
        "verified_in_doc": 0,
        "true_items": 0,
        "verified_precision": 0.0,
        "hallucinated": 0,
        "hallucination_rate": 0.0,
        "doc_verification": doc_verification,
    }


def _covered(item_tokens: set[str], document_tokens: set[str], token_coverage: float) -> bool:
    """True when both token sets are non-empty and ``token_coverage`` of the item's tokens are in the
    document."""
    return (
        bool(item_tokens)
        and bool(document_tokens)
        and len(item_tokens & document_tokens) / len(item_tokens) >= token_coverage
    )


def verify_list_items(items: Iterable[object] | None, doc_text: object, token_coverage: float) -> list[bool]:
    """Return, for each item, whether at least ``token_coverage`` of its content tokens occur in ``doc_text``.

    An item with no content tokens, or a document with none, is never grounded.
    """
    document_tokens = containment_tokens(str(doc_text or ""))
    return [_covered(containment_tokens(str(item)), document_tokens, token_coverage) for item in items or []]


def audit_list_field(
    profile: ScoringProfile,
    element_type: str,
    predicted_items: object,
    expected_items: object,
    doc_text: str | None,
    *,
    token_coverage: float | None = None,
    rescue: EmbeddingRescue | None = None,
    registry: FieldScorerRegistry | None = None,
    unknown_type_as: str | None = None,
) -> dict[str, Any]:
    """Audit every predicted item of one list field.

    An item is true when it matches any expected item or is grounded in
    ``doc_text`` (at ``token_coverage``, default ``profile.audit.token_coverage``).
    ``verified_precision`` and ``hallucination_rate`` are rounded to four places.
    An empty prediction gives the empty audit with ``doc_verification`` False.
    """
    predicted_texts = [str(item) for item in as_list(predicted_items)]
    expected_texts = [str(item) for item in as_list(expected_items)]
    registry = registry if registry is not None else FieldScorerRegistry.for_profile(profile)
    scorer = element_scorer(registry, element_type, unknown_type_as)
    predicted_count = len(predicted_texts)
    if predicted_count == 0:
        return _empty_audit(False)

    match_threshold = profile.thresholds.list_match_threshold
    document_tokens = containment_tokens(str(doc_text or ""))
    coverage = profile.audit.token_coverage if token_coverage is None else token_coverage

    matched_gt = verified = true_items = 0
    for item in predicted_texts:
        hit = any(
            element_similarity(profile, element_type, item, expected, scorer=scorer, rescue=rescue)
            >= match_threshold
            for expected in expected_texts
        )
        grounded = _covered(containment_tokens(item), document_tokens, coverage)
        matched_gt += hit
        verified += grounded
        true_items += hit or grounded

    return {
        "n_predicted": predicted_count,
        "matched_gt": matched_gt,
        "verified_in_doc": verified,
        "true_items": true_items,
        "verified_precision": round(true_items / predicted_count, 4),
        "hallucinated": predicted_count - true_items,
        "hallucination_rate": round((predicted_count - true_items) / predicted_count, 4),
        "doc_verification": bool(doc_text),
    }


def verification_tokens(value: object, field_type: str) -> set[str]:
    """The content tokens a predicted value is looked up by in the document.

    A parseable date is first written as prose ("August 18, 2014", "August 2014",
    "2014"), so an ISO prediction meets the document's prose date.
    """
    text = str(value or "")
    if field_type == "date":
        parts = parse_date_parts(text)
        if parts is not None:
            month = MONTH_NAMES[parts.month - 1].capitalize() if parts.month else ""
            day = f" {parts.day}" if parts.day else ""
            if parts.year and parts.day:  # noqa: SIM108 (a nested conditional expression reads worse)
                year = f", {parts.year}"
            else:
                year = f" {parts.year}" if parts.year else ""
            text = f"{month}{day}{year}".strip()
    return containment_tokens(text)


def date_grounded_in_document(predicted_date: datetime.date | DateParts | None, doc_text: str | None) -> bool:
    """True when ``doc_text`` states a date that agrees with every component ``predicted_date`` states.

    ``predicted_date`` is a full date or the :class:`DateParts` of a partial one
    ("August 18"); a component the prediction does not state is never compared.
    """
    if predicted_date is None or not doc_text:
        return False
    if not isinstance(predicted_date, DateParts):
        predicted_date = DateParts(predicted_date.year, predicted_date.month, predicted_date.day)
    for match in _DATE_CANDIDATE.finditer(doc_text):
        candidate = parse_date_parts(match.group(0))
        if candidate is not None and all(
            wanted is None or wanted == found for wanted, found in zip(predicted_date, candidate, strict=True)
        ):
            return True
    return False


def _scalar_audit_scorer(
    registry: FieldScorerRegistry, field_type: str, unknown_type_as: str | None
) -> ScalarScorer:
    """The scorer a scalar audit matches with: the uncapped date similarity for dates, else the type's
    scorer."""
    if field_type == "date":
        return date_match_similarity
    return element_scorer(registry, field_type, unknown_type_as)


def audit_scalar_field(
    profile: ScoringProfile,
    field_type: str,
    predicted: object,
    expected: object,
    doc_text: str | None,
    *,
    rescue: EmbeddingRescue | None = None,
    registry: FieldScorerRegistry | None = None,
    unknown_type_as: str | None = None,
) -> dict[str, Any]:
    """Audit one scalar predicted value.

    The value is true when it matches ``expected`` (which must not be None or
    ``""``) or is grounded in ``doc_text``. A blank prediction is not audited (the
    empty audit, with ``doc_verification`` telling whether a document was given).
    """
    registry = registry if registry is not None else FieldScorerRegistry.for_profile(profile)
    scorer = _scalar_audit_scorer(registry, field_type, unknown_type_as)
    predicted_text = str(predicted or "")
    if not predicted_text.strip():
        return _empty_audit(bool(doc_text))
    matched = expected not in (None, "") and scorer(predicted_text, str(expected), rescue) >= (
        profile.thresholds.list_match_threshold
    )
    grounded = False
    if doc_text:
        if field_type == "date":
            grounded = date_grounded_in_document(parse_date_parts(predicted_text), doc_text)
        else:
            grounded = _covered(
                verification_tokens(predicted_text, field_type),
                containment_tokens(doc_text),
                profile.audit.token_coverage,
            )
    true = bool(matched) or bool(grounded)
    return {
        "n_predicted": 1,
        "matched_gt": int(bool(matched)),
        "verified_in_doc": int(bool(grounded)),
        "true_items": int(true),
        "verified_precision": 1.0 if true else 0.0,
        "hallucinated": 0 if true else 1,
        "hallucination_rate": 0.0 if true else 1.0,
        "doc_verification": bool(doc_text),
    }


def field_audit_support(audits: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize audits one vote per audited field, never one vote per predicted item.

    A field counts when its ``n_predicted`` is truthy. Returns ``n_audited`` and the
    mean ``verified_precision`` and ``hallucination_rate`` over those fields rounded
    to four places (None when no field counts). A counted audit missing either rate
    raises ``KeyError``.
    """
    eligible = [audit for audit in audits.values() if audit.get("n_predicted")]
    count = len(eligible)
    if not count:
        return {"n_audited": 0, "verified_precision": None, "hallucination_rate": None}
    return {
        "n_audited": count,
        "verified_precision": round(sum(audit["verified_precision"] for audit in eligible) / count, 4),
        "hallucination_rate": round(sum(audit["hallucination_rate"] for audit in eligible) / count, 4),
    }
