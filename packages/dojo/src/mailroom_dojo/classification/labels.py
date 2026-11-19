"""Reading a classifier's output as a document class key.

A :class:`ClassLabelSet` is built from a scoring profile: the live classes in the
profile's ``classification.match_order``, each with the patterns that recognise it
in free text (its key, its sorter label and its synonyms). Retired classes are not
in the set, so text naming one is never read as that class.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..profile import FrozenMapping, ProfileError, ScoringProfile

__all__ = [
    "ERROR_PREFIX",
    "LABEL_JSON_KEY",
    "ClassLabelSet",
    "class_label_pattern",
    "exact_match",
    "failure",
    "normalize_label",
]

#: A classifier output that starts with this text is a failed row, not a label.
ERROR_PREFIX = "ERROR: "

#: The key of a JSON object output that holds the class label.
LABEL_JSON_KEY = "doc_type"

#: Characters stripped from both ends of an output that matched no class.
_UNMATCHED_STRIP = '"`*_ '

_WORD_SEPARATOR = re.compile(r"[\W_]+")


def class_label_pattern(label: str) -> str | None:
    """Return the regular expression that finds ``label`` as words in lowercase text.

    The label's words (split on anything that is not a letter or digit, underscores
    included) must appear in order, joined by nothing, one space or one underscore,
    and bounded by word boundaries: ``merger_agreement`` gives
    ``\\bmerger[_ ]?agreement\\b``. A label with no letter or digit gives None.
    """
    words = [word for word in _WORD_SEPARATOR.split(label.lower()) if word]
    if not words:
        return None
    return r"\b" + "[_ ]?".join(re.escape(word) for word in words) + r"\b"


@dataclass(frozen=True)
class ClassLabelSet:
    """The classes a classifier output can be read as, in match order.

    ``exact`` maps a whole lowercased output (a class key or a sorter label) to its
    class key. ``patterns`` holds, per class in match order, the compiled patterns
    tried against the output; the first class with a matching pattern wins.
    """

    class_keys: tuple[str, ...]
    exact: FrozenMapping[str, str]
    patterns: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]

    @classmethod
    def from_profile(cls, profile: ScoringProfile) -> ClassLabelSet:
        """Build the label set of a profile's live classes, in ``classification.match_order``.

        Each class is recognised by its key's words, then its sorter label's words
        (when the label differs from the key), then each synonym (a regular expression
        matched case-blind). Retired classes are left out. Raises :class:`ProfileError`
        when two live classes would claim the same exact output (a sorter label that
        equals another class's key or, ignoring case, another sorter label) or when the
        profile has no live class.
        """
        exact: dict[str, str] = {}
        owners: dict[str, str] = {}
        patterns: list[tuple[str, tuple[re.Pattern[str], ...]]] = []
        live = [profile.require_class(key) for key in profile.classification_match_order]
        live = [spec for spec in live if spec.is_live]
        if not live:
            raise ProfileError("classes", "no live class to classify into")
        for spec in live:
            exact[spec.key] = spec.key
            owners[spec.key] = f"classes[{profile.class_keys.index(spec.key)}].key"
        for spec in live:
            label = spec.sorter_label.strip().lower()
            path = f"classes[{profile.class_keys.index(spec.key)}].sorter_label"
            if label in exact and exact[label] != spec.key:
                raise ProfileError(
                    path, f"sorter label {spec.sorter_label!r} is already claimed by {owners[label]}"
                )
            exact[label] = spec.key
            owners.setdefault(label, path)
        for spec in live:
            compiled: list[re.Pattern[str]] = [re.compile(str(class_label_pattern(spec.key)))]
            label_pattern = class_label_pattern(spec.sorter_label)
            if label_pattern is not None and label_pattern != compiled[0].pattern:
                compiled.append(re.compile(label_pattern))
            compiled += [re.compile(synonym, re.IGNORECASE) for synonym in spec.synonyms]
            patterns.append((spec.key, tuple(compiled)))
        return cls(
            class_keys=tuple(spec.key for spec in live), exact=FrozenMapping(exact), patterns=tuple(patterns)
        )

    def normalize(self, value: Any) -> str:
        """Read a classifier output as a class key (best effort); see :func:`normalize_label`."""
        if value is None:
            return ""
        text = str(value).strip().lower()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
                text = str(parsed.get(LABEL_JSON_KEY) or text).lower()
            except json.JSONDecodeError:
                pass
        key = self.exact.get(text)
        if key is not None:
            return key
        for class_key, class_patterns in self.patterns:
            for pattern in class_patterns:
                if pattern.search(text):
                    return class_key
        return text.strip(_UNMATCHED_STRIP)


def normalize_label(value: Any, label_set: ClassLabelSet) -> str:
    """Read a classifier output as a class key (best effort).

    ``None`` gives ``""``. Otherwise the output is stripped and lowercased; a JSON
    object output is replaced by its ``doc_type`` value (lowercased) when it has a
    truthy one. An output that is exactly a class key or sorter label gives that
    class; else the first class in match order with a pattern found in the text wins
    (so "correspondence about an insurance claim" is ``correspondence`` when
    correspondence comes first). An output matching no class is returned stripped of
    quotes, backticks, asterisks, underscores and spaces at both ends.
    """
    return label_set.normalize(value)


def exact_match(output: Any, expected: Any, label_set: ClassLabelSet) -> float:
    """1.0 when the output and the expected label normalize to the same class, else 0.0."""
    return 1.0 if label_set.normalize(output) == label_set.normalize(expected) else 0.0


def failure(output: Any, expected: Any) -> float:
    """1.0 when the output is a failed row (text starting with :data:`ERROR_PREFIX`), else 0.0."""
    return 1.0 if str(output).startswith(ERROR_PREFIX) else 0.0
