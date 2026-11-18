"""Scalar field scorers: documented examples, bounds on hostile input, symmetry and identity."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mailroom_dojo.field_scoring import scalar_scorers as scorers
from mailroom_dojo.field_scoring.plugins.cms_identifiers import cms_document_signature, score_cms_doc_ref
from mailroom_dojo.field_scoring.scorer_registry import BUILTIN_SCORERS
from mailroom_dojo.normalization.money import parse_money

ALL_SCORERS = {**BUILTIN_SCORERS, "cms_doc_ref": score_cms_doc_ref}

hostile = st.one_of(
    st.text(max_size=60),
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(),
    st.lists(st.text(max_size=8), max_size=3),
    st.dictionaries(st.text(max_size=4), st.integers(), max_size=2),
    st.sampled_from(
        ["N/A", "null", "[]", "{}", "$", "1e309", "-0", "\u200b", "２０１４年８月１８日", "Ⅻ", "ﬁ"]
    ),
)


@pytest.mark.parametrize(
    ("scorer", "predicted", "expected", "score"),
    [
        (scorers.score_id_field, "ABC-123", "abc 123", 1.0),
        (scorers.score_id_field, "ABC-123", "ABC-124", 0.0),
        (scorers.score_money_field, "$1,000", "1000.004", 1.0),
        (scorers.score_money_field, "$1,000", "$1,020", 0.0),
        (scorers.score_money_field, "1.2M", "$1,200,000", 1.0),
        (scorers.score_date_field, "August 18, 2014", "2014-08-18", 1.0),
        (scorers.score_date_field, "August 2014", "2014-08-18", scorers.DATE_PARTIAL_CREDIT),
        (scorers.score_date_field, "2014-08-20", "2014-08-18", scorers.DATE_PARTIAL_CREDIT),
        (scorers.score_date_field, "2015-08-18", "2014-08-18", scorers.DATE_WEAK_CREDIT),
        (scorers.score_date_field, "August 18", "2014-08-18", scorers.UNVERIFIED_YEAR_CAP),
        (scorers.score_date_field, "not a date", "2014-08-18", 0.0),
        (scorers.score_date_field, "", "2014-08-18", 0.0),
        (scorers.score_label_field, "Auto", "auto", 1.0),
        (scorers.score_label_field, "auto", "home", 0.0),
        (scorers.score_label_field, "", "", 0.0),
        (scorers.score_containment_field, "State of Delaware", "Delaware", 1.0),
        (scorers.score_containment_field, "Delaware", "State of Delaware", 0.5),
        (scorers.score_name_field, "Acme Inc.", "ACME", 1.0),
        (scorers.score_name_field, "x", "", 0.0),
        (scorers.score_free_text_field, "the quick fox", "quick brown fox", 2 / 3),
        (score_cms_doc_ref, "NPI 1234567890", "provider npi 1234567890", 1.0),
        (score_cms_doc_ref, "NPI 1234567890", "NPI 1234567891", 0.0),
    ],
)
def test_documented_examples(scorer, predicted, expected, score):
    """Representative pairs score as the scorers' docstrings describe."""
    assert scorer(predicted, expected) == pytest.approx(score, abs=1e-12)


def test_date_credit_constants_are_the_old_values():
    """The partial-credit ladder and the money tolerance are part of the scoring contract."""
    assert (scorers.DATE_PARTIAL_CREDIT, scorers.DATE_WEAK_CREDIT, scorers.UNVERIFIED_YEAR_CAP) == (
        0.67,
        0.33,
        0.67,
    )
    assert (scorers.MONEY_TOLERANCE, scorers.DATE_NEAR_WINDOW_DAYS) == (0.01, 45)


def test_dates_outside_the_near_window_get_no_day_credit():
    """Within 45 days earns partial credit; further apart in the same year does not."""
    near = scorers.score_date_field("2014-09-30", "2014-08-18")
    far = scorers.score_date_field("2014-12-30", "2014-08-18")
    assert near > far


def test_money_tolerance_is_one_cent_in_binary_floating_point():
    """Half a cent matches and two cents do not; exactly one cent depends on float rounding (kept from the old
    Dojo)."""
    assert scorers.score_money_field("100.00", "100.005") == 1.0
    assert scorers.score_money_field("100.00", "100.02") == 0.0
    assert scorers.score_money_field("0.00", "0.01") == 1.0
    assert scorers.score_money_field("100.00", "100.01") == 0.0  # 100.01 - 100.00 > 0.01 in binary


def test_money_that_does_not_parse_is_scored_as_a_name():
    """When a side is not an amount, the pair falls back to name scoring rather than 0.0."""
    assert scorers.score_money_field("ten dollars", "ten dollars") == 1.0
    assert scorers.score_money_field("ten", "$10") == scorers.score_name_field("ten", "$10")


@pytest.mark.parametrize("name", sorted(ALL_SCORERS))
@given(predicted=hostile, expected=hostile)
def test_every_scorer_returns_a_bounded_float_for_any_input(name, predicted, expected):
    """No input, however strange, raises or escapes [0, 1]."""
    score = ALL_SCORERS[name](predicted, expected)
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0


@pytest.mark.parametrize("name", ["id", "name", "money", "containment"])
@given(value=st.text(max_size=40))
def test_a_value_matches_itself(name, value):
    """id, name, money and containment give any text a full match against itself.

    The one exception is money text that parses to infinity or NaN ("inf", "NAN"): the old
    scorer compares the two values by subtraction, which is NaN, and scores 0.0. That is
    kept for parity and recorded in the score changelog.
    """
    if name == "money" and (parsed := parse_money(value)) is not None and not math.isfinite(parsed):
        assert BUILTIN_SCORERS[name](value, value) == 0.0
        return
    assert BUILTIN_SCORERS[name](value, value) == 1.0


@given(value=st.text(max_size=40))
def test_a_label_matches_itself_exactly_when_it_has_ascii_letters_or_digits(value):
    """Labels compact to ASCII letters and digits; a label with none never matches, even itself."""
    has_ascii = any(character.isascii() and character.isalnum() for character in value.lower())
    assert scorers.score_label_field(value, value) == (1.0 if has_ascii else 0.0)


@given(value=st.text(max_size=40))
def test_a_parsed_full_date_matches_itself(value):
    """A text that parses to a full date scores 1.0 against itself."""
    from mailroom_dojo.normalization.dates import parse_date

    if parse_date(value) is not None:
        assert scorers.score_date_field(value, value) == 1.0


@pytest.mark.parametrize("name", ["id", "label"])
@given(first=hostile, second=hostile)
def test_exact_match_scorers_are_symmetric(name, first, second):
    """id and label ignore which side is the prediction (money, dates and names are deliberately not
    symmetric)."""
    assert BUILTIN_SCORERS[name](first, second) == BUILTIN_SCORERS[name](second, first)


def test_date_scoring_is_directional():
    """A prediction that leaves out a stated day loses credit; one that adds a day to a month label does
    not."""
    assert scorers.score_date_field("August 2014", "2014-08-18") == scorers.DATE_PARTIAL_CREDIT
    assert scorers.score_date_field("2014-08-18", "August 2014") == 1.0


@pytest.mark.parametrize(
    ("text", "signature"),
    [
        ("facility provider 360029", ("facility_provider", "360029")),
        ("NDC 0703-0510-2", ("ndc", "00703051002")),
        ("NDC 703051002", ("ndc", "00703051002")),
        ("provider NPI 5040521237", ("npi", "5040521237")),
        ("drug 00703051002", ("ndc", "00703051002")),
        ("HCPCS detail (1 codes)", ("descriptor", "hcpcs detail")),
        (None, ("descriptor", "")),
    ],
)
def test_cms_document_signatures(text, signature):
    """Supporting-document items are classified by identifier kind, normalized NDC form, or descriptor
    letters."""
    assert cms_document_signature(text) == signature
