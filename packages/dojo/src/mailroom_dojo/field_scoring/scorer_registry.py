"""Which scorer scores which field type.

A :class:`FieldScorerRegistry` maps scalar field type names to scorers. It holds
the built-in types and the plug-ins a profile declares, and resolves any field type
(scalar or entity list) to the scalar scorer that compares one pair of values. An
unregistered type is an error, not a quiet name match; a caller that must keep the
old fallback asks for it explicitly and gets told when it was used.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..profile.field_types import ENTITY_LIST_PREFIX, element_type, is_entity_list
from ..profile.frozen import FrozenMapping
from .plugins import BUNDLED_PLUGINS
from .scalar_scorers import (
    score_containment_field,
    score_date_field,
    score_free_text_field,
    score_id_field,
    score_label_field,
    score_money_field,
    score_name_field,
)
from .similarity_backend import EmbeddingRescue

if TYPE_CHECKING:
    from ..profile.models import ScoringProfile

__all__ = [
    "BUILTIN_SCORERS",
    "FieldScorerRegistry",
    "ResolvedFieldType",
    "ScalarScorer",
    "ScorerResultError",
    "UnknownFieldTypeError",
    "heuristic_field_type",
    "is_entity_list",
]


class ScalarScorer(Protocol):
    """Scores one predicted value against one expected value, in ``[0, 1]``."""

    def __call__(self, predicted: object, expected: object, rescue: EmbeddingRescue | None = None) -> float:
        """Return the score."""


#: The built-in scalar scorers, one per name in
#: :data:`mailroom_dojo.profile.field_types.BUILTIN_SCALAR_TYPES` (a test checks they agree).
BUILTIN_SCORERS: Mapping[str, ScalarScorer] = FrozenMapping({
    "id": score_id_field,
    "date": score_date_field,
    "money": score_money_field,
    "name": score_name_field,
    "label": score_label_field,
    "free_text": score_free_text_field,
    "containment": score_containment_field,
})


class UnknownFieldTypeError(LookupError):
    """A field type names a scalar type the registry does not have."""


class ScorerResultError(ValueError):
    """A plug-in scorer returned something other than a finite number in [0, 1]."""


def _checked(name: str, scorer: ScalarScorer) -> ScalarScorer:
    """Wrap a plug-in scorer so that any result outside a finite [0, 1] raises :class:`ScorerResultError`.

    The built-in scorers are never wrapped (they are proved by parity). A plug-in is
    new code supplied by a client profile, and a result of 2.0, NaN or None would
    otherwise flow into field scores and the overall score without a trace.
    """

    @functools.wraps(scorer)
    def checked(*arguments: Any, **keywords: Any) -> float:
        result = scorer(*arguments, **keywords)
        if isinstance(result, bool) or not isinstance(result, (int, float)) or not 0.0 <= result <= 1.0:
            raise ScorerResultError(f"plug-in scorer {name!r} returned {result!r}; it must return a number in [0, 1]")
        return float(result)

    return checked


@dataclass(frozen=True)
class ResolvedFieldType:
    """A field type resolved against a registry.

    ``scalar_type`` is the type whose scorer compares one pair: the field type
    itself, or an entity list's element type. ``fell_back`` is True when the type
    was unregistered and the caller's fallback type was used instead.
    """

    field_type: str
    is_list: bool
    scalar_type: str
    scorer: ScalarScorer
    fell_back: bool = False


class FieldScorerRegistry:
    """An immutable map from scalar field type names to scorers."""

    def __init__(self, scorers: Mapping[str, ScalarScorer]) -> None:
        """Hold ``scorers``; every name must be a plain type name and every scorer callable."""
        for name, scorer in scorers.items():
            if not isinstance(name, str) or not name or ":" in name or name == ENTITY_LIST_PREFIX:
                raise ValueError(f"invalid scalar field type name {name!r}")
            if not callable(scorer):
                raise TypeError(f"scorer for {name!r} is not callable")
        self._scorers: Mapping[str, ScalarScorer] = FrozenMapping(
            {name: scorer if BUILTIN_SCORERS.get(name) is scorer else _checked(name, scorer) for name, scorer in scorers.items()}
        )

    @classmethod
    def builtin(cls) -> FieldScorerRegistry:
        """Return a registry with the built-in scalar types only."""
        return cls(BUILTIN_SCORERS)

    @classmethod
    def for_profile(
        cls, profile: ScoringProfile, plugins: Mapping[str, ScalarScorer] | None = None
    ) -> FieldScorerRegistry:
        """Return the built-in types plus every plug-in the profile declares.

        A declared plug-in is taken from ``plugins`` when given there, else from the
        plug-ins bundled with the Dojo. Raises :class:`UnknownFieldTypeError` naming a
        declared plug-in that neither provides, and ``ValueError`` for a ``plugins``
        entry the profile does not declare (it could never be used).
        """
        supplied = dict(plugins or {})
        undeclared = sorted(set(supplied) - set(profile.scorer_plugins))
        if undeclared:
            raise ValueError(f"plug-ins {undeclared} are not declared in the profile's scorer_plugins")
        scorers = dict(BUILTIN_SCORERS)
        for name in profile.scorer_plugins:
            scorer = supplied.get(name) or BUNDLED_PLUGINS.get(name)
            if scorer is None:
                raise UnknownFieldTypeError(
                    f"profile {profile.client!r} declares plug-in {name!r}, but no scorer was supplied "
                    f"and none is bundled; bundled: {sorted(BUNDLED_PLUGINS)}"
                )
            scorers[name] = scorer
        return cls(scorers)

    def with_plugin(self, name: str, scorer: ScalarScorer) -> FieldScorerRegistry:
        """Return a new registry that also has ``name``; refuses to replace an existing type."""
        if name in self._scorers:
            raise ValueError(f"field type {name!r} is already registered")
        return FieldScorerRegistry({**self._scorers, name: scorer})

    @property
    def scalar_types(self) -> frozenset[str]:
        """The registered scalar type names."""
        return frozenset(self._scorers)

    def knows(self, field_type: str) -> bool:
        """True when ``field_type`` (scalar or entity list) resolves without a fallback."""
        scalar = element_type(field_type) if is_entity_list(field_type) else field_type
        return scalar in self._scorers

    def scalar_scorer(self, scalar_type: str) -> ScalarScorer:
        """Return the scorer of a scalar type; raise :class:`UnknownFieldTypeError` when it is not registered."""
        try:
            return self._scorers[scalar_type]
        except KeyError:
            raise UnknownFieldTypeError(
                f"unregistered field type {scalar_type!r}; registered: {sorted(self._scorers)}"
            ) from None

    def resolve(self, field_type: str, *, unknown_as: str | None = None) -> ResolvedFieldType:
        """Resolve a field type to its pair scorer.

        An unregistered scalar or element type raises :class:`UnknownFieldTypeError`,
        unless ``unknown_as`` names a registered type to use instead; the result then
        has ``fell_back=True`` so the caller can report it.
        """
        is_list = is_entity_list(field_type)
        scalar = element_type(field_type) if is_list else field_type
        if scalar in self._scorers:
            return ResolvedFieldType(field_type, is_list, scalar, self._scorers[scalar])
        if unknown_as is None:
            self.scalar_scorer(scalar)
        return ResolvedFieldType(field_type, is_list, unknown_as, self.scalar_scorer(unknown_as), fell_back=True)


_MONEY_NAME_WORDS = ("value", "amount", "fee", "compensation", "price", "cost", "salary", "consideration", "total")
_ID_NAME_WORDS = ("number", "id", "docket", "reference", "filing")


def heuristic_field_type(field_name: str, value: object) -> str:
    """Guess a field type from a field's name and expected value (the ``heuristic`` unmapped-field policy).

    A list value is ``entity_list``. Otherwise, by lowercase name: containing
    "date" is ``date``; a money word (value, amount, fee, compensation, price, cost,
    salary, consideration, total) is ``money``; an id word (number, id, docket,
    reference, filing) is ``id``; anything else is ``name``.
    """
    if isinstance(value, list):
        return ENTITY_LIST_PREFIX
    name = field_name.lower()
    if "date" in name:
        return "date"
    if any(word in name for word in _MONEY_NAME_WORDS):
        return "money"
    if any(word in name for word in _ID_NAME_WORDS):
        return "id"
    return "name"

