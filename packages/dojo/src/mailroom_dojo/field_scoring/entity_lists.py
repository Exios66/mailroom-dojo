"""Entity-list scoring: pair predicted items with expected items, then precision, recall and F1.

Every predicted item is compared with every expected item by the element type's
scorer. The pairing that maximises the total similarity is found with the Hungarian
algorithm (``scipy.optimize.linear_sum_assignment``); a pair counts as matched when
its similarity reaches the profile's ``thresholds.list_match_threshold``.

The matcher is chosen explicitly. ``matcher="hungarian"`` (the default) needs scipy;
when scipy cannot be imported it raises, unless the caller allows
``fallback_matcher="greedy"``, in which case the greedy matcher runs and the result
says so (:attr:`EntityListScore.matcher` and :attr:`EntityListScore.matcher_fallback`).
Any other error, for example a scorer that raises or returns NaN, propagates: it is a
bug to fix, not a reason to change algorithm.

A list scored by ground-truth coverage (``recall_coverage=True``, for labels that
sample a document rather than list it fully) credits two kinds of expected item
without pairing: party-role words (``profile.list_matching.role_words``) whenever the
prediction names a party, and labels of three to six tokens found word for word inside
a predicted item.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from ..normalization.text import tokenize
from ..values.emptiness import parse_json_container
from .scalar_scorers import date_match_similarity
from .scorer_registry import FieldScorerRegistry, ScalarScorer, UnknownFieldTypeError
from .similarity_backend import EmbeddingRescue

if TYPE_CHECKING:
    from ..profile.models import ScoringProfile

__all__ = [
    "CONTAINED_LABEL_TOKENS",
    "EntityListScore",
    "ListMatcher",
    "as_list",
    "element_scorer",
    "element_similarity",
    "score_entity_list",
]

logger = logging.getLogger(__name__)

ListMatcher = Literal["hungarian", "greedy"]

#: How many tokens an expected label of a recall-scored list may have and still be
#: credited when it occurs word for word inside a predicted item.
CONTAINED_LABEL_TOKENS: tuple[int, int] = (3, 6)

#: The most tokens a phrase may have and still be a party-role phrase ("the Seller").
_ROLE_PHRASE_MAX_TOKENS = 3


@dataclass(frozen=True)
class EntityListScore:
    """Matching counts, precision, recall and F1 of one entity-list field.

    ``recall_coverage`` makes :attr:`score` the recall instead of F1. ``matcher``
    names what paired the items: ``none`` (one side was empty), ``single_pair`` (one
    item on each side after credits), ``hungarian`` or ``greedy``.
    ``matcher_fallback`` is None, or the reason the Hungarian matcher could not run
    when the caller allowed the greedy fallback. Neither is serialized by
    :meth:`to_dict`, whose shape is fixed.
    """

    field_name: str
    precision: float
    recall: float
    f1: float
    matched: int
    unmatched_predicted: int
    unmatched_expected: int
    recall_coverage: bool = False
    matcher: str = "none"
    matcher_fallback: str | None = None

    @property
    def score(self) -> float:
        """The field score: recall for a recall-scored list, else F1."""
        return self.recall if self.recall_coverage else self.f1

    @property
    def n_predicted(self) -> int:
        """How many predicted items were scored."""
        return self.matched + self.unmatched_predicted

    @property
    def n_expected(self) -> int:
        """How many expected items were scored."""
        return self.matched + self.unmatched_expected

    def to_dict(self) -> dict[str, Any]:
        """Serialize: metrics rounded to four places, item counts, and the recall flag as ``partial_gt``."""
        return {
            "field": self.field_name,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "matched": self.matched,
            "n_predicted": self.n_predicted,
            "n_expected": self.n_expected,
            "partial_gt": self.recall_coverage,
        }

    def named(self, field_name: str) -> EntityListScore:
        """Return a copy that carries ``field_name``."""
        return replace(self, field_name=field_name)


def as_list(value: object) -> list[Any]:
    """Read a value as a list: a JSON list text is parsed, None is empty, a scalar is one item."""
    value = parse_json_container(value)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def element_scorer(
    registry: FieldScorerRegistry, element_type: str, unknown_type_as: str | None = None
) -> ScalarScorer:
    """Return the scorer of a list's element type.

    An unregistered element type raises :class:`UnknownFieldTypeError`, unless
    ``unknown_type_as`` names a registered type to score it as.
    """
    if element_type in registry.scalar_types:
        return registry.scalar_scorer(element_type)
    if unknown_type_as is None:
        raise UnknownFieldTypeError(
            f"unregistered element type {element_type!r}; registered: {sorted(registry.scalar_types)}"
        )
    return registry.scalar_scorer(unknown_type_as)


def element_similarity(
    profile: ScoringProfile,
    element_type: str,
    predicted_item: object,
    expected_item: object,
    *,
    scorer: ScalarScorer,
    rescue: EmbeddingRescue | None = None,
) -> float:
    """Similarity of one predicted item to one expected item.

    ``date`` elements use the uncapped date similarity. For ``free_text`` elements,
    when the predicted item holds at least ``profile.audit.token_coverage`` of the
    expected item's tokens, that share is the similarity if it is higher (the item
    quotes the labelled clause at length). ``scorer`` is the element type's scorer.
    """
    if element_type == "date":
        return date_match_similarity(predicted_item, expected_item, rescue)
    base = scorer(predicted_item, expected_item, rescue)
    if element_type == "free_text":
        expected_tokens = set(tokenize(str(expected_item)))
        predicted_tokens = set(tokenize(str(predicted_item)))
        if expected_tokens:
            coverage = len(expected_tokens & predicted_tokens) / len(expected_tokens)
            if coverage >= profile.audit.token_coverage:
                return max(float(base), coverage)
    return base


def _load_hungarian_solver() -> Callable[..., Any]:
    """Import and return scipy's ``linear_sum_assignment`` (raises ImportError without scipy)."""
    from scipy.optimize import linear_sum_assignment

    return linear_sum_assignment


def _hungarian_matches(similarity_rows: list[list[float]], threshold: float, solver: Callable[..., Any]) -> int:
    """Count the pairs of the maximum-total-similarity assignment that reach ``threshold``."""
    import numpy

    similarities = numpy.array(similarity_rows, dtype=float)
    rows, columns = solver(1.0 - similarities)
    return sum(1 for row, column in zip(rows, columns) if similarities[row, column] >= threshold)


def _greedy_matches(
    predicted_items: list[str], expected_items: list[str], threshold: float, similarity: Callable[[str, str], float]
) -> int:
    """Pair each predicted item, in order, with its most similar unpaired expected item at or above ``threshold``.

    On a tie the later expected item wins.
    """
    matched = 0
    assigned: set[int] = set()
    for predicted_item in predicted_items:
        best, best_index = threshold, None
        for index, expected_item in enumerate(expected_items):
            if index in assigned:
                continue
            value = similarity(predicted_item, expected_item)
            if value >= best:
                best, best_index = value, index
        if best_index is not None:
            matched += 1
            assigned.add(best_index)
    return matched


def score_entity_list(
    profile: ScoringProfile,
    element_type: str,
    predicted: object,
    expected: object,
    *,
    recall_coverage: bool = False,
    rescue: EmbeddingRescue | None = None,
    registry: FieldScorerRegistry | None = None,
    matcher: ListMatcher = "hungarian",
    fallback_matcher: Literal["greedy"] | None = None,
    unknown_type_as: str | None = None,
) -> EntityListScore:
    """Score a predicted list against an expected list of ``element_type`` items.

    Both sides are read with :func:`as_list` and every item is compared as text.
    Two empty lists score 1.0; an empty prediction scores 0.0 with recall 0.0; an
    empty expectation scores precision 1.0, recall and F1 0.0. Otherwise items are
    paired by ``matcher`` (see the module docstring), recall-coverage credits are
    added, and the matched count is capped at the smaller list size, so precision
    and recall never exceed 1.0.

    ``registry`` defaults to the profile's (built-ins plus its plug-ins). An
    unregistered element type raises :class:`UnknownFieldTypeError` unless
    ``unknown_type_as`` names a type to score it as.
    """
    if matcher not in ("hungarian", "greedy"):
        raise ValueError(f"matcher must be 'hungarian' or 'greedy', got {matcher!r}")
    if fallback_matcher not in (None, "greedy"):
        raise ValueError(f"fallback_matcher must be None or 'greedy', got {fallback_matcher!r}")
    registry = registry if registry is not None else FieldScorerRegistry.for_profile(profile)
    scorer = element_scorer(registry, element_type, unknown_type_as)
    predicted_items = [str(item) for item in as_list(predicted)]
    expected_items = [str(item) for item in as_list(expected)]
    if not predicted_items and not expected_items:
        return EntityListScore("", 1.0, 1.0, 1.0, 0, 0, 0, recall_coverage)
    if not predicted_items:
        return EntityListScore("", 0.0, 0.0, 0.0, 0, 0, len(expected_items), recall_coverage)
    if not expected_items:
        return EntityListScore("", 1.0, 0.0, 0.0, 0, len(predicted_items), 0, recall_coverage)

    threshold = profile.thresholds.list_match_threshold
    predicted_count, expected_count = len(predicted_items), len(expected_items)
    role_words = profile.list_matching.role_words

    def is_role_phrase(text: str) -> bool:
        """True for a phrase of one to three tokens that are all party-role words."""
        tokens = tokenize(str(text))
        return 1 <= len(tokens) <= _ROLE_PHRASE_MAX_TOKENS and all(token in role_words for token in tokens)

    if recall_coverage:
        predicted_token_lists = [tokenize(str(item)) for item in predicted_items]
        shortest, longest = CONTAINED_LABEL_TOKENS

        def label_contained(label: str) -> bool:
            """True when a label of three to six tokens occurs word for word in a predicted item."""
            label_tokens = tokenize(str(label))
            size = len(label_tokens)
            if not label_tokens or size < shortest or size > longest:
                return False
            return any(
                label_tokens == tokens[start:start + size]
                for tokens in predicted_token_lists
                for start in range(len(tokens) - size + 1)
            )

        paired_expected = [item for item in expected_items if not (is_role_phrase(item) or label_contained(item))]
        role_credits = sum(1 for item in expected_items if is_role_phrase(item))
        contained_credits = expected_count - len(paired_expected) - role_credits
        prediction_names_party = any(not is_role_phrase(item) for item in predicted_items)
    else:
        paired_expected = expected_items
        role_credits = 0
        contained_credits = 0
        prediction_names_party = False

    def similarity(predicted_item: str, expected_item: str) -> float:
        """Element similarity under this call's profile, scorer and rescue."""
        return element_similarity(profile, element_type, predicted_item, expected_item, scorer=scorer, rescue=rescue)

    used_matcher: str
    fallback_reason: str | None = None
    if len(predicted_items) == 1 and len(paired_expected) == 1:
        used_matcher = "single_pair"
        matched = 1 if similarity(predicted_items[0], paired_expected[0]) >= threshold else 0
    elif matcher == "greedy":
        used_matcher = "greedy"
        matched = _greedy_matches(predicted_items, paired_expected, threshold, similarity)
    else:
        try:
            solver = _load_hungarian_solver()
        except ImportError as error:
            if fallback_matcher is None:
                raise
            fallback_reason = f"scipy unavailable ({type(error).__name__}: {error})"
            logger.warning("Hungarian list matcher unavailable; using the greedy matcher: %s", fallback_reason)
            used_matcher = "greedy"
            matched = _greedy_matches(predicted_items, paired_expected, threshold, similarity)
        else:
            used_matcher = "hungarian"
            rows = [[similarity(item, other) for other in paired_expected] for item in predicted_items]
            matched = _hungarian_matches(rows, threshold, solver)

    if role_credits and prediction_names_party:
        matched += role_credits
    if contained_credits:
        matched += contained_credits
    matched = min(matched, predicted_count, expected_count)

    precision = matched / predicted_count
    recall = matched / expected_count
    f1 = 2 * precision * recall / (precision + recall) if matched else 0.0
    return EntityListScore(
        field_name="",
        precision=precision,
        recall=recall,
        f1=f1,
        matched=matched,
        unmatched_predicted=predicted_count - matched,
        unmatched_expected=expected_count - matched,
        recall_coverage=recall_coverage,
        matcher=used_matcher,
        matcher_fallback=fallback_reason,
    )
