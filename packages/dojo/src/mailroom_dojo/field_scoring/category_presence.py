"""Category presence: did the model find the clause of each category the label says is present?

A presence expectation is ``{category: {"expected": bool, "answer": str, "field": str}}``:
whether the document has a clause of that category, the labelled clause text and
the predicted field that should hold it. The score is the share of expected-present
categories whose clause some predicted span covers.

Candidate spans come from the model's reasoning trace when it tags entries with the
category name (``reasoning.entries[].field``) and ``profile.presence.route_by_reasoning``
is on; otherwise from the category's field, both split into clause spans and as
written. A span covers the clause when the clause's content tokens are contained in
it at ``profile.audit.token_coverage``, or, with a :class:`PresenceSimilarity`, when
the backend rates them at least ``profile.presence.embedding_threshold``.
"""

from __future__ import annotations

import logging
import math
import numbers
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..normalization.text import tokenize
from .entity_lists import as_list
from .scalar_scorers import score_containment_field
from .similarity_backend import FallbackReason, SimilarityBackend, SimilarityFallback

if TYPE_CHECKING:
    from ..profile.models import ScoringProfile

__all__ = [
    "DEFAULT_MIN_SPAN_TOKENS",
    "PresenceSimilarity",
    "disaggregate_clause_spans",
    "presence_candidates",
    "score_category_presence",
    "split_clause_spans",
]

logger = logging.getLogger(__name__)

#: A clause fragment with fewer tokens than this is merged into the fragment before it.
DEFAULT_MIN_SPAN_TOKENS = 4

# Split at newlines, at "; " and after sentence-final punctuation that follows a
# letter or ")" and precedes a capital, digit, quote or "(" -- not inside "$5.00",
# "1.2" or "U.S.".
_CLAUSE_BOUNDARY = re.compile(r"(?:\s*\n\s*|\s*;\s+|(?<=[A-Za-z)][.!?])\s+(?=[A-Z0-9\"(]))")


class PresenceSimilarity:
    """A similarity backend and threshold for one presence scoring run, plus the fallbacks it met.

    The backend is asked only for spans whose token containment falls short. Its
    answer counts when it is a real number in ``[0, 1]`` at or above the threshold.
    When it raises, or answers NaN, a non-number or a value outside ``[0, 1]``, the
    span is not matched by similarity and a :class:`SimilarityFallback` is recorded
    in :attr:`fallbacks` (``string_score`` holds the containment score) and logged.
    An answer of None declines silently.
    """

    def __init__(self, backend: SimilarityBackend, threshold: float) -> None:
        """Use ``backend`` with a similarity ``threshold`` in ``[0, 1]``."""
        if not isinstance(backend, SimilarityBackend):
            raise TypeError(
                f"backend must have a similarity(first, second) method, got {type(backend).__name__}"
            )
        if isinstance(threshold, bool) or not isinstance(threshold, numbers.Real):
            raise TypeError(f"threshold must be a number, got {threshold!r}")
        if not (math.isfinite(threshold) and 0.0 <= threshold <= 1.0):
            raise ValueError(f"threshold must be between 0 and 1, got {threshold!r}")
        self._backend = backend
        self._threshold = float(threshold)
        self._fallbacks: list[SimilarityFallback] = []

    @classmethod
    def from_profile(
        cls, profile: ScoringProfile, backend: SimilarityBackend | None
    ) -> PresenceSimilarity | None:
        """Build the similarity a profile allows, or None when ``backend`` is None.

        Raises ``ValueError`` when a backend is passed but the profile disables
        embeddings (``embedding.enabled: false``).
        """
        if backend is None:
            return None
        if not profile.embedding.enabled:
            raise ValueError(
                f"profile {profile.client!r} disables embeddings (embedding.enabled is false) "
                "but a similarity backend was passed"
            )
        return cls(backend, profile.presence.embedding_threshold)

    @property
    def threshold(self) -> float:
        """The similarity a span needs to cover a clause."""
        return self._threshold

    @property
    def fallbacks(self) -> tuple[SimilarityFallback, ...]:
        """Every fallback met so far, in order."""
        return tuple(self._fallbacks)

    def covers(self, span: str, answer: str, containment_score: float) -> bool:
        """True when the backend rates ``span`` and ``answer`` at least :attr:`threshold`."""
        try:
            similarity = self._backend.similarity(span, answer)
        except Exception as error:  # the reported fallback is the contract
            return self._fall_back("backend_raised", f"{type(error).__name__}: {error}", containment_score)
        if similarity is None:
            return False
        if isinstance(similarity, bool) or not isinstance(similarity, numbers.Real):
            return self._fall_back("not_a_number", repr(similarity), containment_score)
        value = float(similarity)
        if math.isnan(value):
            return self._fall_back("not_a_number", repr(similarity), containment_score)
        if not 0.0 <= value <= 1.0:
            return self._fall_back("out_of_range", repr(similarity), containment_score)
        return value >= self._threshold

    def _fall_back(self, reason: FallbackReason, detail: str, containment_score: float) -> bool:
        """Record and log a fallback; the span is not matched by similarity."""
        self._fallbacks.append(SimilarityFallback(reason, detail, containment_score))
        logger.warning("presence similarity fallback: %s (%s); span not matched", reason, detail)
        return False


def split_clause_spans(text: object) -> list[str]:
    """Split one predicted item into clause fragments (newlines, semicolons, sentence ends), stripped, blanks
    dropped."""
    parts = _CLAUSE_BOUNDARY.split(str(text or ""))
    return [part.strip() for part in parts if part.strip()]


def disaggregate_clause_spans(items: object, min_span_tokens: int = DEFAULT_MIN_SPAN_TOKENS) -> list[str]:
    """Split predicted list items into clause-level spans.

    Each item (read with :func:`~.entity_lists.as_list`) is split by
    :func:`split_clause_spans`; a fragment with fewer than ``min_span_tokens``
    tokens is joined to the fragment before it. Spans whose tokens repeat an
    earlier span are dropped, so the result is flat and free of duplicates.
    """
    spans: list[str] = []
    for raw in as_list(items):
        merged: list[str] = []
        for piece in split_clause_spans(str(raw)):
            if merged and len(tokenize(piece)) < min_span_tokens:
                merged[-1] = f"{merged[-1]} {piece}".strip()
            else:
                merged.append(piece)
        spans.extend(span for span in merged if span.strip())
    seen: set[str] = set()
    unique: list[str] = []
    for span in spans:
        key = " ".join(tokenize(span))
        if key not in seen:
            seen.add(key)
            unique.append(span)
    return unique


def presence_candidates(
    profile: ScoringProfile, predicted: Mapping[str, Any], category: str, field: str
) -> list[str]:
    """Return the predicted spans that may hold one category's clause.

    With reasoning routing on, the non-blank evidence (or section reference) of
    every reasoning entry tagged with ``category`` wins when there is one.
    Otherwise: the clause spans of ``predicted[field]``, then each of its items as
    written that is not already a span.
    """
    if profile.presence.route_by_reasoning:
        entries = (predicted.get("reasoning") or {}).get("entries") or []
        routed = [
            str(entry.get("evidence") or entry.get("section_ref") or "")
            for entry in entries
            if str(entry.get("field") or "").strip() == category
        ]
        if routed:
            return [span for span in routed if span.strip()]
    spans = disaggregate_clause_spans(predicted.get(field))
    seen = set(spans)
    for raw in as_list(predicted.get(field)):
        text = str(raw)
        if text.strip() and text not in seen:
            seen.add(text)
            spans.append(text)
    return spans


def _span_covers(
    profile: ScoringProfile, span: str, answer: str, similarity: PresenceSimilarity | None
) -> bool:
    """True when ``span`` covers the labelled clause ``answer`` by containment or by similarity."""
    if not answer or not span:
        return False
    containment = score_containment_field(span, answer)
    if containment >= profile.audit.token_coverage:
        return True
    if similarity is None:
        return False
    return similarity.covers(span, answer, containment)


def score_category_presence(
    profile: ScoringProfile,
    predicted: Mapping[str, Any] | None,
    presence_expectations: Mapping[str, Mapping[str, Any]] | None,
    *,
    similarity: PresenceSimilarity | None = None,
    default_field: str | None = None,
) -> tuple[float, dict[str, dict[str, Any]]]:
    """Score category presence; return ``(score, detail)``.

    ``score`` is the share of expected-present categories whose clause a candidate
    span covers, rounded to four places (1.0 when no category is expected present).
    ``detail`` has one entry per category, in sorted order: ``{"expected": False,
    "matched": None, "field"}`` for an absent category, else ``{"expected": True,
    "matched", "field", "answer", "predicted_items"}``. An expectation without a
    ``field`` uses ``default_field``; when that is None too, ``ValueError`` names the
    category.
    """
    predicted = predicted or {}
    matched = 0
    expected_present = 0
    detail: dict[str, dict[str, Any]] = {}
    for category, expectation in sorted((presence_expectations or {}).items()):
        field = expectation.get("field") or default_field
        if not field:
            raise ValueError(
                f"presence expectation {category!r} names no field and no default_field was given"
            )
        if not expectation.get("expected"):
            detail[category] = {"expected": False, "matched": None, "field": field}
            continue
        expected_present += 1
        answer = str(expectation.get("answer") or "")
        spans = presence_candidates(profile, predicted, category, field)
        hit = any(_span_covers(profile, span, answer, similarity) for span in spans)
        if hit:
            matched += 1
        detail[category] = {
            "expected": True,
            "matched": hit,
            "field": field,
            "answer": answer,
            "predicted_items": len(spans),
        }
    score = round(matched / expected_present, 4) if expected_present else 1.0
    return score, detail
