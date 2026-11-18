"""Raw value helpers: JSON containers and emptiness, including hostile values."""

from __future__ import annotations

import json
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mailroom_dojo.values import NULL_TOKENS, is_empty_value, parse_json_container

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=5), children, max_size=4),
    max_leaves=12,
)
hostile_values = json_values | st.floats() | st.binary() | st.tuples(st.integers()) | st.frozensets(st.integers())


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ("[]", []),
        ("{}", {}),
        (' ["Bylaws"] ', ["Bylaws"]),
        ('{"a": [1, 2]}', {"a": [1, 2]}),
        ("41", "41"),
        ("true", "true"),
        ('"quoted"', '"quoted"'),
        ("[1, 2", "[1, 2"),
        ("{}x", "{}x"),
        ("", ""),
    ],
)
def test_parse_json_container_parses_only_whole_lists_and_objects(value, parsed):
    """Only a whole JSON list or object is parsed; scalars and broken JSON come back as written."""
    assert parse_json_container(value) == parsed
    assert type(parse_json_container(value)) is type(parsed)


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "\t\n", "[]", "{}", " [ ] ", "null", "NULL", " None ", "n/a", "N.A.", "nan", "NaN",
     float("nan"), [], {}, (), set()],
)
def test_empty_values(value):
    """Every absence form is empty."""
    assert is_empty_value(value) is True


@pytest.mark.parametrize(
    "value", [0, 0.0, False, True, "0", "false", "none of them", '[""]', "[null]", [None], [""], {"a": None},
              ("x",), frozenset(), float("inf"), " x"],
)
def test_values_that_are_not_empty(value):
    """Numbers, booleans, non-empty containers (even of blanks) and any text with content are values.

    ``frozenset()`` counts as a value: the rule names list, dict, tuple and set only.
    """
    assert is_empty_value(value) is False


def test_null_tokens_are_the_documented_five():
    """The null tokens are a closed list; adding one changes what is scored."""
    assert NULL_TOKENS == {"null", "none", "n/a", "n.a.", "nan"}


@given(json_values)
def test_a_json_text_of_a_container_parses_back_to_it(value):
    """Round trip: the JSON text of any list or dict parses to an equal value."""
    if isinstance(value, (list, dict)):
        assert parse_json_container(json.dumps(value)) == value


@given(hostile_values)
def test_helpers_never_raise_and_parse_is_idempotent(value):
    """No value makes the helpers raise, and parsing an already parsed value changes nothing."""
    parsed = parse_json_container(value)
    assert parse_json_container(parsed) is parsed
    assert isinstance(is_empty_value(value), bool)


@given(st.text())
def test_emptiness_of_text_agrees_with_its_parsed_container(text):
    """A string holding a JSON container is empty exactly when the container is."""
    parsed = parse_json_container(text)
    if parsed is not text:
        assert is_empty_value(text) == is_empty_value(parsed)
    else:
        assert is_empty_value(text) == (not text.strip() or text.strip().lower() in NULL_TOKENS)


def test_nan_is_empty_but_infinity_is_a_value():
    """NaN is a missing table cell; infinity is a (strange) value."""
    assert is_empty_value(math.nan) and not is_empty_value(math.inf)


def test_a_huge_integer_is_signed_infinity_not_a_crash():
    """An int too large for a float used to raise OverflowError in the old Dojo; it is now infinite money."""
    from mailroom_dojo.field_scoring.scalar_scorers import score_money_field
    from mailroom_dojo.normalization.money import parse_money

    assert parse_money(10**400) == float("inf")
    assert parse_money(-(10**400)) == float("-inf")
    assert score_money_field(10**400, 10**400) == 0.0  # infinite amounts never match, as for "inf" text
    assert score_money_field(10**400, 5) == 0.0


def test_absurdly_nested_json_text_stays_as_written_instead_of_crashing():
    """JSON text nested deeper than the interpreter allows is left unchanged (the old Dojo raised RecursionError)."""
    from mailroom_dojo.values import parse_json_container

    deep = "[" * 100_000 + "]" * 100_000
    assert parse_json_container(deep) == deep
    assert parse_json_container("[[1]]") == [[1]]
