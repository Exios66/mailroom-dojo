"""Embedding rescue: a second, semantic opinion for name and free-text scores.

When a string score is below the profile's ``embedding.rescue_below``, a caller's
:class:`SimilarityBackend` may lift it to the semantic similarity of the two texts.
The Dojo never builds a backend itself: with no backend, scoring is string-only.
There is no shared backend, no cache across callers, no environment variable and no
network access here.

A backend that fails is never trusted silently. When it raises, returns something
that is not a real number, returns NaN, or returns a value outside ``[0, 1]``, the
string score is kept and an :class:`SimilarityFallback` is appended to the
:class:`EmbeddingRescue` that asked (and logged as a warning on this module's
logger). A backend answering ``None`` declines to judge the pair: the string score is
kept and nothing is reported.
"""

from __future__ import annotations

import logging
import math
import numbers
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..profile.models import ScoringProfile

__all__ = [
    "EmbeddingRescue",
    "FallbackReason",
    "SimilarityBackend",
    "SimilarityFallback",
    "with_embedding_rescue",
]

logger = logging.getLogger(__name__)

FallbackReason = Literal["backend_raised", "not_a_number", "out_of_range"]


@runtime_checkable
class SimilarityBackend(Protocol):
    """Anything that rates how alike two texts are in meaning."""

    def similarity(self, first: str, second: str) -> float | None:
        """Return a similarity in ``[0, 1]``, or None to decline judging the pair."""


@dataclass(frozen=True)
class SimilarityFallback:
    """One time a backend's answer was not used and the string score was kept.

    ``reason`` is ``backend_raised`` (``detail`` is the exception type and message),
    ``not_a_number`` (a non-number, a bool, or NaN; ``detail`` is its repr) or
    ``out_of_range`` (a number outside ``[0, 1]``, including infinity).
    """

    reason: FallbackReason
    detail: str
    string_score: float


class EmbeddingRescue:
    """A backend and a rescue threshold for one scoring run, plus the fallbacks it met.

    Create one per scoring run (or use :meth:`from_profile`); it records every
    fallback in :attr:`fallbacks` so the run can report them. Instances share nothing.
    """

    def __init__(self, backend: SimilarityBackend, rescue_below: float) -> None:
        """Use ``backend`` for string scores below ``rescue_below`` (a number in ``[0, 1]``)."""
        if not isinstance(backend, SimilarityBackend):
            raise TypeError(f"backend must have a similarity(first, second) method, got {type(backend).__name__}")
        if isinstance(rescue_below, bool) or not isinstance(rescue_below, numbers.Real):
            raise TypeError(f"rescue_below must be a number, got {rescue_below!r}")
        if not (math.isfinite(rescue_below) and 0.0 <= rescue_below <= 1.0):
            raise ValueError(f"rescue_below must be between 0 and 1, got {rescue_below!r}")
        self._backend = backend
        self._rescue_below = float(rescue_below)
        self._fallbacks: list[SimilarityFallback] = []

    @classmethod
    def from_profile(cls, profile: ScoringProfile, backend: SimilarityBackend | None) -> EmbeddingRescue | None:
        """Build the rescue a profile allows, or None when ``backend`` is None.

        Raises ``ValueError`` when a backend is passed but the profile disables
        embedding rescue (``embedding.enabled: false``): the two disagree, and
        ignoring the backend would hide that.
        """
        if backend is None:
            return None
        if not profile.embedding.enabled:
            raise ValueError(
                f"profile {profile.client!r} disables embedding rescue (embedding.enabled is false) "
                "but a similarity backend was passed"
            )
        return cls(backend, profile.embedding.rescue_below)

    @property
    def backend(self) -> SimilarityBackend:
        """The similarity backend."""
        return self._backend

    @property
    def rescue_below(self) -> float:
        """String scores below this are offered to the backend."""
        return self._rescue_below

    @property
    def fallbacks(self) -> tuple[SimilarityFallback, ...]:
        """Every fallback met so far, in order."""
        return tuple(self._fallbacks)

    def rescue(self, string_score: float, predicted: object, expected: object) -> float:
        """Return ``string_score``, or the backend's similarity of the two texts when that is higher.

        The backend is asked only when ``string_score`` is below :attr:`rescue_below`
        and neither text is blank. It receives ``str(predicted)`` and ``str(expected)``.
        """
        if string_score >= self._rescue_below:
            return string_score
        predicted_text, expected_text = str(predicted), str(expected)
        if not predicted_text.strip() or not expected_text.strip():
            return string_score
        try:
            similarity = self._backend.similarity(predicted_text, expected_text)
        except Exception as error:  # the reported fallback below is the contract
            return self._fall_back("backend_raised", f"{type(error).__name__}: {error}", string_score)
        if similarity is None:
            return string_score
        if isinstance(similarity, bool) or not isinstance(similarity, numbers.Real):
            return self._fall_back("not_a_number", repr(similarity), string_score)
        try:
            value = float(similarity)
        except OverflowError:  # an integer too large for a float is out of range, not a crash
            return self._fall_back("out_of_range", repr(similarity)[:80], string_score)
        if math.isnan(value):
            return self._fall_back("not_a_number", repr(similarity), string_score)
        if not 0.0 <= value <= 1.0:
            return self._fall_back("out_of_range", repr(similarity), string_score)
        return max(string_score, value)

    def _fall_back(self, reason: FallbackReason, detail: str, string_score: float) -> float:
        """Record and log a fallback, and return the string score."""
        self._fallbacks.append(SimilarityFallback(reason, detail, string_score))
        logger.warning("similarity backend fallback: %s (%s); kept string score %s", reason, detail, string_score)
        return string_score


def with_embedding_rescue(
    string_score: float, predicted: object, expected: object, rescue: EmbeddingRescue | None
) -> float:
    """Return ``string_score`` lifted by embedding rescue, or unchanged when ``rescue`` is None."""
    if rescue is None:
        return string_score
    return rescue.rescue(string_score, predicted, expected)
