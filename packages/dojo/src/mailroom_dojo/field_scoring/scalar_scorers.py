"""One scorer per scalar field type. Each returns a score in ``[0, 1]`` for one predicted value against one
expected value.

Every scorer has the signature ``(predicted, expected, rescue=None) -> float``.
``rescue`` is an optional :class:`~.similarity_backend.EmbeddingRescue`; only the
name and free-text rules (and the money and date rules that fall back to them) use it.
"""

from __future__ import annotations

import datetime
import re

from ..normalization.dates import (
    DateParts,
    date_is_blank_or_bare_label,
    date_phrase_contained,
    expected_date_is_null,
    integral_float_as_text,
    parse_date_parts,
    stated_years,
)
from ..normalization.money import parse_money
from ..normalization.text import (
    containment_tokens,
    jaro_winkler,
    normalize_text,
    token_f1,
    token_set_ratio,
    tokenize,
)
from .similarity_backend import EmbeddingRescue, with_embedding_rescue

__all__ = [
    "DATE_PARTIAL_CREDIT",
    "DATE_NEAR_WINDOW_DAYS",
    "DATE_WEAK_CREDIT",
    "MONEY_TOLERANCE",
    "UNVERIFIED_YEAR_CAP",
    "date_match_similarity",
    "date_year_unverified",
    "score_containment_field",
    "score_date_field",
    "score_free_text_field",
    "score_id_field",
    "score_label_field",
    "score_money_field",
    "score_name_field",
]

#: Two amounts within one cent are equal. Legal amounts are exact: "$250,001" is not "$250,000".
MONEY_TOLERANCE = 0.01

#: Credit for a date that agrees on two components (or lands within the near window).
DATE_PARTIAL_CREDIT = 0.67
#: Credit for a date that agrees only on its year or only on its month.
DATE_WEAK_CREDIT = 0.33
#: Two full dates this many days apart or closer earn :data:`DATE_PARTIAL_CREDIT`.
DATE_NEAR_WINDOW_DAYS = 45
#: The most a dated prediction scores against a label that cannot verify its year.
UNVERIFIED_YEAR_CAP = 0.67

_NOT_LOWER_ALPHANUMERIC = re.compile(r"[^a-z0-9]")


def score_id_field(predicted: object, expected: object, rescue: EmbeddingRescue | None = None) -> float:
    """Return 1.0 when both sides normalize to the same identifier, else 0.0.

    Normalization is :func:`~mailroom_dojo.normalization.text.normalize_text`. Two
    sides that both normalize to nothing match; one empty side does not.
    """
    normalized_predicted, normalized_expected = normalize_text(predicted), normalize_text(expected)
    if not normalized_predicted and not normalized_expected:
        return 1.0
    if not normalized_predicted or not normalized_expected:
        return 0.0
    return 1.0 if normalized_predicted == normalized_expected else 0.0


def score_money_field(predicted: object, expected: object, rescue: EmbeddingRescue | None = None) -> float:
    """Return 1.0 when both amounts parse and differ by at most one cent, 0.0 when they differ more.

    When either side does not parse as an amount, the pair is scored as names
    (:func:`score_name_field`) instead of 0.0.
    """
    predicted_amount, expected_amount = parse_money(predicted), parse_money(expected)
    if predicted_amount is not None and expected_amount is not None:
        return 1.0 if abs(predicted_amount - expected_amount) <= MONEY_TOLERANCE else 0.0
    return score_name_field(predicted, expected, rescue)


def score_name_field(predicted: object, expected: object, rescue: EmbeddingRescue | None = None) -> float:
    """Return a fuzzy name match score.

    Both sides are normalized (:func:`~mailroom_dojo.normalization.text.normalize_text`).
    Both empty: 1.0; one empty: 0.0. When every expected token appears in the
    prediction: 1.0. Otherwise the token-set ratio, raised to the Jaro-Winkler
    similarity when the two share a token, then offered to embedding rescue.
    """
    normalized_predicted, normalized_expected = normalize_text(predicted), normalize_text(expected)
    if not normalized_predicted and not normalized_expected:
        return 1.0
    if not normalized_predicted or not normalized_expected:
        return 0.0
    expected_tokens = set(tokenize(normalized_expected))
    predicted_tokens = set(tokenize(normalized_predicted))
    if expected_tokens and expected_tokens <= predicted_tokens:
        return 1.0
    base = token_set_ratio(normalized_predicted, normalized_expected)
    if predicted_tokens & expected_tokens:
        base = max(base, jaro_winkler(normalized_predicted, normalized_expected))
    return with_embedding_rescue(base, predicted, expected, rescue)


def score_label_field(predicted: object, expected: object, rescue: EmbeddingRescue | None = None) -> float:
    """Return 1.0 when both labels compact (lowercase letters and digits) to the same non-empty text, else
    0.0.

    There is no partial credit. Callers fold aliases onto vocabulary tokens first.
    """
    compact_predicted = _NOT_LOWER_ALPHANUMERIC.sub("", str(predicted or "").lower())
    compact_expected = _NOT_LOWER_ALPHANUMERIC.sub("", str(expected or "").lower())
    return 1.0 if compact_predicted and compact_predicted == compact_expected else 0.0


def score_free_text_field(
    predicted: object, expected: object, rescue: EmbeddingRescue | None = None
) -> float:
    """Return the token F1 of the prediction against the expectation, offered to embedding rescue.

    A non-string side is read as ``str(value or "")``.
    """
    if not isinstance(predicted, str):
        predicted = str(predicted or "")
    if not isinstance(expected, str):
        expected = str(expected or "")
    return with_embedding_rescue(token_f1(predicted, expected), predicted, expected, rescue)


def score_containment_field(
    predicted: object, expected: object, rescue: EmbeddingRescue | None = None
) -> float:
    """Return the share of the expected text's tokens found in the prediction, rounded to four places.

    Tokens leave out containment stopwords. No expected tokens: 1.0 when the
    prediction has none either, else 0.0. A non-string side is read as ``str(value or "")``.
    """
    if not isinstance(predicted, str):
        predicted = str(predicted or "")
    if not isinstance(expected, str):
        expected = str(expected or "")
    expected_tokens = containment_tokens(expected)
    predicted_tokens = containment_tokens(predicted)
    if not expected_tokens:
        return 1.0 if not predicted_tokens else 0.0
    if not predicted_tokens:
        return 0.0
    return round(len(expected_tokens & predicted_tokens) / len(expected_tokens), 4)


def score_date_field(predicted: object, expected: object, rescue: EmbeddingRescue | None = None) -> float:
    """Return a date match score. Never reads the clock.

    An integral float side (a year column promoted to float: ``2014.0``) is read as
    its integer text. The rules, in order:

    1. A blank template or bare label as the expectation (no real date): 1.0 for a
       blank prediction or an exact copy of a non-template label, else 0.0. A
       relative date ("nine months after the date hereof") is real text and skips this.
    2. The expected date phrase appears in the prediction: 1.0. The reverse counts
       only when the prediction states every component the label states.
    3. Both sides fully dated: same date 1.0; same year and month, or within 45
       days, 0.67; same year or same month only 0.33; else 0.0.
    4. A side partly dated: only the components both state are compared. All agree
       and the prediction states everything the label does: 1.0. All agree but the
       prediction leaves a label component out: 0.67 with two or more agreeing,
       0.33 with only the year or month, else 0.0. A shared component differs: 0.33
       when the shared year or month still agrees, else 0.0.
    5. The label is dated but the prediction is not a date: 0.0 unless the
       prediction names a year; then the name score, at most 0.33 when it misses
       the labelled year and at most 0.67 against a partial label.
    6. Otherwise the name score.

    Finally, a prediction stating a year scores at most 0.67 against a label that
    cannot verify that year (:func:`date_year_unverified`).
    """
    predicted, expected = integral_float_as_text(predicted), integral_float_as_text(expected)
    score = _score_date_uncapped(predicted, expected, rescue)
    if score > UNVERIFIED_YEAR_CAP and date_year_unverified(predicted, expected):
        return UNVERIFIED_YEAR_CAP
    return score


def date_year_unverified(predicted: object, expected: object) -> bool:
    """True when the label cannot verify the year(s) the prediction states.

    A label that states no year cannot verify any year the prediction states, in a
    parsed date or in prose. A prediction naming several years that the label does
    not all state is ambiguous; a label that itself names those years is not.
    """
    predicted, expected = integral_float_as_text(predicted), integral_float_as_text(expected)
    predicted_parts, expected_parts = parse_date_parts(predicted), parse_date_parts(expected)
    predicted_years = stated_years(predicted) | (
        {predicted_parts.year} if predicted_parts is not None and predicted_parts.year else set()
    )
    if expected_parts is not None and expected_parts.year is None and predicted_years:
        return True
    return len(predicted_years) > 1 and not predicted_years <= stated_years(expected)


def date_match_similarity(
    predicted: object, expected: object, rescue: EmbeddingRescue | None = None
) -> float:
    """Return the date similarity used for pairing list items and audits.

    Like :func:`score_date_field`, except a pair whose year the label cannot verify
    is 0.0: a dated prediction never pairs with a yearless label.
    """
    if date_year_unverified(predicted, expected):
        return 0.0
    return score_date_field(predicted, expected, rescue)


def _score_date_uncapped(predicted: object, expected: object, rescue: EmbeddingRescue | None) -> float:
    """Apply rules 1 to 6 of :func:`score_date_field` (without the unverified-year cap)."""
    if not isinstance(predicted, str):
        predicted = str(predicted or "")
    if not isinstance(expected, str):
        expected = str(expected or "")
    predicted_parts, expected_parts = parse_date_parts(predicted), parse_date_parts(expected)
    if expected_parts is None and expected_date_is_null(expected):
        if not str(predicted).strip():
            return 1.0
        if not date_is_blank_or_bare_label(expected) and normalize_text(predicted) == normalize_text(
            expected
        ):
            return 1.0
        return 0.0
    prediction_omits_label_component = expected_parts is not None and (
        predicted_parts is None or bool(expected_parts.stated - predicted_parts.stated)
    )
    if date_phrase_contained(predicted, expected) or (
        not prediction_omits_label_component and date_phrase_contained(expected, predicted)
    ):
        return 1.0
    if predicted_parts is not None and expected_parts is not None:
        if predicted_parts.is_full and expected_parts.is_full:
            return _score_full_dates(datetime.date(*predicted_parts), datetime.date(*expected_parts))
        return _score_partial_dates(predicted_parts, expected_parts)
    if expected_parts is not None:
        predicted_years = stated_years(predicted)
        if not predicted_years:
            return 0.0
        score = score_name_field(predicted, expected, rescue)
        if expected_parts.year is not None and expected_parts.year not in predicted_years:
            score = min(score, DATE_WEAK_CREDIT)
        if not expected_parts.is_full:
            score = min(score, DATE_PARTIAL_CREDIT)
        return score
    return score_name_field(predicted, expected, rescue)


def _score_full_dates(predicted: datetime.date, expected: datetime.date) -> float:
    """Score two full dates: exact, year and month or near window, year or month only."""
    if predicted == expected:
        return 1.0
    shared = sum(
        1
        for first, second in ((predicted.year, expected.year), (predicted.month, expected.month))
        if first == second
    )
    if shared == 2:
        return DATE_PARTIAL_CREDIT
    if abs((predicted - expected).days) <= DATE_NEAR_WINDOW_DAYS:
        return DATE_PARTIAL_CREDIT
    if shared == 1:
        return DATE_WEAK_CREDIT
    return 0.0


def _score_partial_dates(predicted: DateParts, expected: DateParts) -> float:
    """Score when a side lacks a year or a day: compare only the components both state."""
    common = predicted.stated & expected.stated
    agreeing = {index for index in common if predicted[index] == expected[index]}
    year_or_month = {0, 1}
    if agreeing == common and common:
        if expected.stated <= predicted.stated:
            return 1.0
        if len(agreeing) >= 2:
            return DATE_PARTIAL_CREDIT
        return DATE_WEAK_CREDIT if agreeing & year_or_month else 0.0
    return DATE_WEAK_CREDIT if agreeing & year_or_month else 0.0
