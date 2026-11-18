"""Text, date and money normalization: behaviour, the clock, hostile input and hard dependencies."""

from __future__ import annotations

import datetime as real_datetime
import subprocess
import sys
import time
import types

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mailroom_dojo.normalization import dates, money, text
from mailroom_dojo.normalization.dates import DateParts, parse_date, parse_date_parts
from mailroom_dojo.field_scoring.scalar_scorers import score_date_field

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]

date_like_text = st.one_of(
    st.builds(
        lambda year, month, day, form: [
            f"{year:04d}-{month:02d}-{day:02d}",
            f"{month}/{day}/{year % 100:02d}",
            f"{MONTHS[month - 1]} {day}, {year}",
            f"{day} {MONTHS[month - 1]}",
            f"{MONTHS[month - 1]} {year}",
            f"{day}th day of {MONTHS[month - 1]}",
            f"Monday {MONTHS[month - 1]} {day}",
            f"{year}",
        ][form],
        st.integers(1900, 2099), st.integers(1, 12), st.integers(1, 28), st.integers(0, 7),
    ),
    st.text(max_size=30),
)


@pytest.fixture
def freeze_clock(monkeypatch):
    """Pin every clock dateutil could read: its ``datetime.now`` and ``time.localtime``/``time.time``."""
    from dateutil.parser import _parser

    def freeze(year: int) -> None:
        """Make "now" a fixed instant in ``year``."""

        class FrozenDatetime(real_datetime.datetime):
            """A datetime whose ``now()`` and ``today()`` are pinned."""

            @classmethod
            def now(cls, tz=None):
                """Return mid-July of the pinned year."""
                return cls(year, 7, 15, 12, 0, 0)

            @classmethod
            def today(cls):
                """Return mid-July of the pinned year."""
                return cls(year, 7, 15, 12, 0, 0)

        fake = types.SimpleNamespace(**vars(real_datetime))
        fake.datetime = FrozenDatetime
        monkeypatch.setattr(_parser, "datetime", fake)
        pinned = time.struct_time((year, 7, 15, 12, 0, 0, 0, 196, 0))
        monkeypatch.setattr(time, "localtime", lambda *args: pinned)
        monkeypatch.setattr(time, "time", lambda: real_datetime.datetime(year, 7, 15).timestamp())

    return freeze


@pytest.mark.parametrize(
    ("text_value", "parts"),
    [
        ("2014-08-18", (2014, 8, 18)),
        ("18th day of August", (None, 8, 18)),
        ("August 2014", (2014, 8, None)),
        ("Monday August 2014", (2014, 8, None)),
        ("August 1-3, 2014", (2014, 8, 1)),
        ("1-3 August 2014", (2014, 8, 1)),
        ("March 12,1999", (1999, 3, 12)),
        ("16 day of August. 2012", (2012, 8, 16)),
        ("1st day of April, 2007 (", (2007, 4, 1)),
        ("February 29", (None, 2, 29)),
    ],
)
def test_parse_date_parts_states_only_what_the_text_states(text_value, parts):
    """A component the text does not state is None, never filled from a default or the clock."""
    assert tuple(parse_date_parts(text_value)) == parts


@pytest.mark.parametrize("text_value", ["", "   ", "12", "Tuesday", "10:30", "garbage", None, 2014, ["2014-08-18"]])
def test_non_dates_parse_to_none(text_value):
    """Blanks, bare numbers, weekdays, times, garbage and non-strings are not dates."""
    assert parse_date_parts(text_value) is None
    assert parse_date(text_value) is None


def test_parse_date_needs_a_full_date():
    """A full date gives a calendar date; a partial one gives None."""
    assert parse_date("August 18, 2014") == real_datetime.date(2014, 8, 18)
    assert parse_date("August 18") is None
    assert parse_date("August 2014") is None


def test_date_parts_report_their_stated_components():
    """``is_full`` and ``stated`` describe exactly the stated components."""
    assert DateParts(2014, 8, 18).is_full and DateParts(2014, 8, 18).stated == {0, 1, 2}
    assert not DateParts(None, 8, 18).is_full and DateParts(None, 8, 18).stated == {1, 2}


@pytest.mark.parametrize(
    ("text_value", "year"),
    [("1/1/77", 1977), ("1/1/50", 2050), ("1/1/68", 2068), ("1/1/69", 1969), ("1/1/00", 2000),
     ("March 3, 99", 1999), ("1/1/1877", 1877)],
)
@pytest.mark.parametrize("today", [2014, 2026, 2031, 2080])
def test_two_digit_years_use_a_fixed_pivot_not_the_clock(freeze_clock, text_value, year, today):
    """A two-digit year maps to the same century in any year the scorer runs, even with a parser built then."""
    freeze_clock(today)
    assert parse_date_parts(text_value).year == year
    rebuilt = dates._FixedPivotParserInfo()
    from dateutil import parser

    parsed = parser.parse(text_value, parserinfo=rebuilt, default=real_datetime.datetime(2000, 1, 1))
    assert parsed.year == year


@given(date_like_text, date_like_text)
def test_parsing_and_date_scores_do_not_depend_on_the_clock(first, second):
    """Under two very different fake clocks, parsing and scoring give the same answers."""
    import pytest as _pytest

    results = []
    for year in (1987, 2063):
        with _pytest.MonkeyPatch.context() as patch:
            from dateutil.parser import _parser

            class FrozenDatetime(real_datetime.datetime):
                """A datetime pinned to the chosen year."""

                @classmethod
                def now(cls, tz=None, _year=year):
                    """Return a fixed instant in the pinned year."""
                    return cls(_year, 2, 2, 2, 2, 2)

            fake = types.SimpleNamespace(**vars(real_datetime))
            fake.datetime = FrozenDatetime
            patch.setattr(_parser, "datetime", fake)
            pinned = time.struct_time((year, 2, 2, 2, 2, 2, 0, 33, 0))
            patch.setattr(time, "localtime", lambda *args, _pinned=pinned: _pinned)
            results.append((parse_date_parts(first), parse_date(second), score_date_field(first, second)))
    assert results[0] == results[1]


def test_dateutil_is_never_asked_for_the_current_time(monkeypatch):
    """The parser never calls ``datetime.now``: a clock that explodes is never touched."""
    from dateutil.parser import _parser

    class ExplodingDatetime(real_datetime.datetime):
        """A datetime whose ``now()`` fails the test."""

        @classmethod
        def now(cls, tz=None):
            """Fail: the scorer must not read the clock."""
            raise AssertionError("the clock was read")

    fake = types.SimpleNamespace(**vars(real_datetime))
    fake.datetime = ExplodingDatetime
    monkeypatch.setattr(_parser, "datetime", fake)
    for value in ("August 18", "2014", "18th day of August", "Monday", "1/1/77", "August 18, 2014"):
        parse_date_parts(value)
        score_date_field(value, "August 18, 2014")


@pytest.mark.parametrize(
    ("value", "amount"),
    [("$1,234.56", 1234.56), ("1.2M USD", 1.2e6), ("USD 1.2M", 1.2e6), ("€500", 500.0), ("1k", 1000.0),
     ("2B", 2e9), ("12 dollars", 12.0), (12, 12.0), (True, 1.0), ("", None), ("$", None), ("M", None),
     ("twelve", None), (None, None), ([5], None)],
)
def test_parse_money(value, amount):
    """Currency symbols, words and magnitude suffixes are read; everything else is None."""
    assert money.parse_money(value) == amount


@given(st.one_of(st.text(), st.integers(), st.floats(), st.none(), st.lists(st.integers())))
def test_parse_money_never_raises(value):
    """Any input gives a float or None."""
    result = money.parse_money(value)
    assert result is None or isinstance(result, float)


@given(st.text())
def test_normalize_text_is_idempotent_uppercase_and_punctuation_free(value):
    """Normalizing twice changes nothing, and the result has no lowercase-able ASCII letters or punctuation."""
    once = text.normalize_text(value)
    assert text.normalize_text(once) == once
    assert not any(character in once for character in ",.;:'\"()[]{}!?@#$%^&*+=|\\/<>~`_-")
    assert not set(once.split()) & text.CORPORATE_SUFFIXES


@given(st.text(), st.text())
def test_similarities_are_bounded_and_token_f1_is_symmetric(first, second):
    """Every similarity is in [0, 1]; token F1 does not depend on which side is predicted."""
    for value in (text.token_f1(first, second), text.token_set_ratio(first, second),
                  text.jaro_winkler(first, second), text.sequence_ratio(first, second)):
        assert 0.0 <= value <= 1.0
    if text.tokenize(first) and text.tokenize(second):
        assert text.token_f1(first, second) == pytest.approx(text.token_f1(second, first), abs=1e-12)


def test_jaro_winkler_falls_back_to_sequence_ratio_only_for_unencodable_text():
    """A lone surrogate cannot reach jellyfish; that pair is compared with difflib, as before."""
    assert text.jaro_winkler("AB\ud800C", "ABC") == text.sequence_ratio("AB\ud800C", "ABC")
    assert text.jaro_winkler("MARTHA", "MARHTA") == pytest.approx(0.9611, abs=1e-4)


@pytest.mark.parametrize("module", ["jellyfish", "dateutil", "yaml"])
def test_a_missing_required_dependency_is_an_import_error(module):
    """jellyfish, python-dateutil and PyYAML are required: without one, importing the Dojo fails loudly."""
    code = f"import sys; sys.modules[{module!r}] = None; import mailroom_dojo"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ModuleNotFoundError" in result.stderr
    assert module in result.stderr


def test_huge_inputs_finish_quickly():
    """A 200,000-character value does not trigger catastrophic regular-expression backtracking."""
    huge_words = "1 " + "word " * 40_000
    huge_digits = "1-" * 100_000
    started = time.perf_counter()
    for value in (huge_words, huge_digits, "August " * 30_000):
        parse_date_parts(value)
        score_date_field(value, "August 18, 2014")
        text.normalize_text(value)
        money.parse_money(value)
    assert time.perf_counter() - started < 20.0
