"""Text normalization and string similarity used by the field scorers.

These are the English-language rules of the scoring engine (punctuation, corporate
suffixes, containment stopwords). Changing any of them changes scores, so a change
belongs in the score changelog, not in a client profile.
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

import jellyfish

__all__ = [
    "CONTAINMENT_STOPWORDS",
    "CORPORATE_SUFFIXES",
    "compact_label",
    "containment_tokens",
    "jaro_winkler",
    "normalize_text",
    "sequence_ratio",
    "token_f1",
    "token_set_ratio",
    "tokenize",
]

#: Corporate and honorific suffixes dropped from names before matching
#: ("Global Technologies, Ltd" and "Global Technologies Ltd" are one entity).
CORPORATE_SUFFIXES: frozenset[str] = frozenset(
    {
        "INC",
        "INCORPORATED",
        "LLC",
        "LTD",
        "LIMITED",
        "CORP",
        "CORPORATION",
        "CO",
        "COMPANY",
        "PLC",
        "LLP",
        "LP",
        "PLLC",
        "PC",
        "PA",
        "ESQ",
        "ESQUIRE",
        "GMBH",
        "AG",
        "NV",
        "SA",
        "SARL",
        "BV",
        "PTY",
        "GROUP",
        "HOLDINGS",
        "TRUST",
    }
)

#: Function words left out of containment token sets only: clause labels ("the laws
#: of the State of Delaware") carry them, and counting them would make short
#: paraphrases look like misses.
CONTAINMENT_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "and",
        "or",
        "for",
        "to",
        "in",
        "on",
        "by",
        "as",
        "is",
        "be",
        "are",
        "with",
        "its",
        "their",
        "his",
        "her",
        "such",
        "hereof",
        "hereunder",
        "shall",
        "any",
        "all",
        "this",
        "that",
        "these",
        "those",
        "from",
        "between",
        "into",
        "upon",
        "under",
        "over",
    }
)

_PUNCTUATION = re.compile(r"[,.;:'\"()\[\]{}!?@#$%^&*+=|\\/<>~`_-]")
_NOT_LOWER_ALPHANUMERIC = re.compile(r"[^a-z0-9]")


def normalize_text(text: object) -> str:
    """Return the canonical form used by exact and fuzzy matching.

    The text (``str()`` of any non-string) is uppercased, punctuation becomes a
    space, tokens in :data:`CORPORATE_SUFFIXES` are dropped and the rest are joined
    with single spaces.
    """
    if not isinstance(text, str):
        text = str(text)
    tokens = _PUNCTUATION.sub(" ", text.upper()).split()
    return " ".join(token for token in tokens if token not in CORPORATE_SUFFIXES)


def tokenize(text: object) -> list[str]:
    """Return the lowercase tokens of ``text`` with punctuation treated as a separator."""
    if not isinstance(text, str):
        text = str(text)
    return _PUNCTUATION.sub(" ", text.lower()).split()


def containment_tokens(text: object) -> set[str]:
    """Return the token set of ``text`` without :data:`CONTAINMENT_STOPWORDS`."""
    return {token for token in tokenize(text) if token not in CONTAINMENT_STOPWORDS}


def compact_label(value: object) -> str:
    """Return the label comparison key: lowercase letters and digits only.

    A falsy value (``None``, ``""``, ``0``, ``False``) gives ``""``.
    """
    return _NOT_LOWER_ALPHANUMERIC.sub("", str(value or "").lower())


def sequence_ratio(first: str, second: str) -> float:
    """Return difflib's similarity ratio; two empty strings match, one empty string does not."""
    if not first and not second:
        return 1.0
    if not first or not second:
        return 0.0
    return SequenceMatcher(None, first, second).ratio()


def token_set_ratio(first: str, second: str) -> float:
    """Return a token-set similarity in ``[0, 1]`` over the tokens of both strings.

    Equal token sets score 1.0 and an empty side scores 0.0 (both empty: 1.0).
    Otherwise the score is the larger of two sequence ratios: shared tokens against
    the first string's tokens, and the first string's leftover tokens against the
    second's, each joined in sorted order.
    """
    first_tokens, second_tokens = set(tokenize(first)), set(tokenize(second))
    if not first_tokens and not second_tokens:
        return 1.0
    if not first_tokens or not second_tokens:
        return 0.0
    shared = first_tokens & second_tokens
    if shared == first_tokens and shared == second_tokens:
        return 1.0
    base = sequence_ratio(" ".join(sorted(shared)), " ".join(sorted(first_tokens)))
    leftovers = sequence_ratio(
        " ".join(sorted(first_tokens - second_tokens)),
        " ".join(sorted(second_tokens - first_tokens)),
    )
    return max(base, leftovers)


def token_f1(predicted: str, expected: str) -> float:
    """Return the token-level F1 of ``predicted`` against ``expected`` over token multisets.

    An empty expected text scores 1.0 against an empty prediction and 0.0 otherwise;
    an empty prediction against a non-empty expectation scores 0.0.
    """
    predicted_tokens, expected_tokens = tokenize(predicted), tokenize(expected)
    if not expected_tokens:
        return 1.0 if not predicted_tokens else 0.0
    if not predicted_tokens:
        return 0.0
    true_positives = sum((Counter(expected_tokens) & Counter(predicted_tokens)).values())
    if true_positives == 0:
        return 0.0
    precision = true_positives / len(predicted_tokens)
    recall = true_positives / len(expected_tokens)
    return 2 * precision * recall / (precision + recall)


def jaro_winkler(first: str, second: str) -> float:
    """Return the Jaro-Winkler similarity of two strings; an empty side scores 0.0.

    Text that cannot be encoded as UTF-8 (a lone surrogate, which can arrive through
    JSON) is compared with :func:`sequence_ratio` instead. Every other error from
    ``jellyfish`` propagates.
    """
    if not first or not second:
        return 0.0
    try:
        return float(jellyfish.jaro_winkler_similarity(first, second))
    except UnicodeEncodeError:
        return sequence_ratio(first, second)
