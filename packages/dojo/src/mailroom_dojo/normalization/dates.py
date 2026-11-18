"""Reading dates from text without ever reading the clock.

A date states some of year, month and day. A component the text does not state is
never invented: not from today's date and not from a placeholder. dateutil fills
unstated components from a default datetime (its own default is the current date), so
the text is parsed twice against two fixed, different defaults and only the
components that come out the same are kept. Two-digit years use a fixed pivot.
"""

from __future__ import annotations

import datetime
import re
from typing import NamedTuple

from dateutil import parser as dateutil_parser

__all__ = [
    "MONTH_NAMES",
    "TWO_DIGIT_YEAR_PIVOT",
    "DateParts",
    "contains_phrase",
    "date_is_blank_or_bare_label",
    "date_phrase_contained",
    "expected_date_is_null",
    "expected_date_is_relative",
    "integral_float_as_text",
    "parse_date",
    "parse_date_parts",
    "stated_years",
]

MONTH_NAMES: tuple[str, ...] = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

#: Two-digit years 00-68 read as 2000-2068 and 69-99 as 1969-1999 (Python's ``%y``).
TWO_DIGIT_YEAR_PIVOT = 69

# The two fill-in defaults differ in year, month and day, so a component that is the
# same in both parses was stated by the text. Both are leap years, so "February 29"
# parses.
_FIRST_PROBE = datetime.datetime(2000, 1, 1)
_SECOND_PROBE = datetime.datetime(2004, 3, 3)


class DateParts(NamedTuple):
    """The year, month and day a date text states; ``None`` marks a component it does not state."""

    year: int | None
    month: int | None
    day: int | None

    @property
    def is_full(self) -> bool:
        """True when year, month and day are all stated."""
        return None not in self

    @property
    def stated(self) -> frozenset[int]:
        """The positions (0 year, 1 month, 2 day) of the stated components."""
        return frozenset(index for index, value in enumerate(self) if value is not None)


class _FixedPivotParserInfo(dateutil_parser.parserinfo):
    """A dateutil ``parserinfo`` whose two-digit-year century never depends on the clock."""

    def convertyear(self, year: int, century_specified: bool = False) -> int:
        """Map a two-digit year with :data:`TWO_DIGIT_YEAR_PIVOT`; leave a stated century alone."""
        if year < 100 and not century_specified:
            return year + (1900 if year >= TWO_DIGIT_YEAR_PIVOT else 2000)
        return year


# Built once at import and never changed. Its constructor notes the current year,
# but convertyear above is the only place dateutil would use it.
_PARSER_INFO = _FixedPivotParserInfo()

_WHITESPACE = re.compile(r"\s+")
_ORDINAL_SUFFIX = re.compile(r"(\d)(st|nd|rd|th)\b", re.IGNORECASE)
_COMMA_BEFORE_DIGIT = re.compile(r",(?=\d)")
_DAY_RANGE_AFTER_MONTH = re.compile(
    r"\b([A-Za-z]{3,}\.?\s+\d{1,2})\s*[-–]\s*\d{1,2}\b(?![-/.]\d)"
)
_DAY_RANGE_BEFORE_MONTH = re.compile(r"(?<![\d/.\-])(\d{1,2})\s*[-–]\s*\d{1,2}(?=\s+[A-Za-z]{3,})")
_WEEKDAY = re.compile(
    r"\b(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b\.?,?",
    re.IGNORECASE,
)
_DAY_OF_WORDS = re.compile(r"\b(\d{1,2})[a-z]*(?:\s+[a-z]+)*\s+day\s+of\b", re.IGNORECASE)
_DAY_OF_MONTH = re.compile(r"\b(\d{1,2})\s+day\s+of\s+([A-Za-z]+)[,.\s]*(\d{4})?", re.IGNORECASE)
_TRAILING_OPENERS = re.compile(r"[(\[\{,]+$")
_STATED_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

_NUMBER_WORD = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred)"
)
_RELATIVE_DATE = re.compile(
    rf"\b(?:\d+|{_NUMBER_WORD}(?:[\s-]+(?:and\s+)?{_NUMBER_WORD})*)\s*(?:\(\d+\)\s*)?"
    r"(?:(?:business|calendar|consecutive)\s+)?(?:days?|weeks?|months?|years?)\s+"
    r"(?:after|before|from|following|of|prior\s+to|subsequent\s+to)\b",
    re.IGNORECASE,
)


def _clean_date_text(text: str) -> str:
    """Rewrite date prose into a form dateutil reads without inventing components."""
    cleaned = _ORDINAL_SUFFIX.sub(r"\1", _WHITESPACE.sub(" ", text.strip()))
    # "March 12,1999": dateutil drops a year glued to the comma.
    cleaned = _COMMA_BEFORE_DIGIT.sub(", ", cleaned)
    # A day range ("August 1-3, 2014", "1-3 August 2014") names its first day. ISO and
    # numeric dates are untouched: the range must sit next to a word.
    cleaned = _DAY_RANGE_AFTER_MONTH.sub(r"\1", cleaned)
    cleaned = _DAY_RANGE_BEFORE_MONTH.sub(r"\1", cleaned)
    # A weekday is not a day of the month: dateutil would move to the next such weekday.
    cleaned = _WEEKDAY.sub(" ", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    cleaned = _DAY_OF_WORDS.sub(r"\1 day of", cleaned)
    # "16 day of August. 2012": a period before the year is typing noise.
    day_of_month = _DAY_OF_MONTH.search(cleaned)
    if day_of_month:
        day, month, year = day_of_month.group(1), day_of_month.group(2), day_of_month.group(3)
        cleaned = f"{month} {day}, {year}" if year else f"{month} {day}"
    return _TRAILING_OPENERS.sub("", cleaned).strip()


def parse_date_parts(text: object) -> DateParts | None:
    """Return the year, month and day that ``text`` states, or None.

    None means: not a string, blank, unparseable, or a parse that states neither a
    year nor a month (a bare day number or a time is not a date). "18th day of
    August" gives ``(None, 8, 18)`` and "August 2014" gives ``(2014, 8, None)``.
    Handles ISO and numeric dates, "March 3, 2024", ordinal prose ("10th day of
    January 2000"), day ranges, weekday names and stray trailing brackets or commas.
    The result never depends on the clock.
    """
    if not isinstance(text, str):
        return None
    if not _WHITESPACE.sub(" ", text.strip()):
        return None
    cleaned = _clean_date_text(text)
    try:
        first = dateutil_parser.parse(cleaned, parserinfo=_PARSER_INFO, default=_FIRST_PROBE)
        second = dateutil_parser.parse(cleaned, parserinfo=_PARSER_INFO, default=_SECOND_PROBE)
    except (ValueError, OverflowError):
        return None
    parts = DateParts(
        first.year if first.year == second.year else None,
        first.month if first.month == second.month else None,
        first.day if first.day == second.day else None,
    )
    if parts.year is None and parts.month is None:
        return None
    return parts


def parse_date(text: object) -> datetime.date | None:
    """Return the calendar date ``text`` states, or None.

    A date is returned only when the text states year, month and day; a partial date
    ("18th day of August", "August 2014") or an unparseable text gives None. Never
    reads the clock.
    """
    parts = parse_date_parts(text)
    if parts is None or not parts.is_full:
        return None
    try:
        return datetime.date(parts.year, parts.month, parts.day)
    except ValueError:
        return None


def stated_years(text: object) -> set[int]:
    """Return the four-digit years from 1900 to 2099 written in ``str(text)``."""
    return {int(year) for year in _STATED_YEAR.findall(str(text))}


def integral_float_as_text(value: object) -> object:
    """Return an integral float as its integer text (``2014.0`` gives ``"2014"``), any other value unchanged."""
    if isinstance(value, float) and value == value and value.is_integer():
        return str(int(value))
    return value


def expected_date_is_relative(expected: object) -> bool:
    """True for a relative date ("nine months after the date hereof").

    A relative date states a period, not a calendar date, and is not a blank
    template: text with an underscore or the word "blank" is never relative.
    """
    text = str(expected)
    if "_" in text or "blank" in text.lower():
        return False
    return _RELATIVE_DATE.search(text) is not None


def expected_date_is_null(expected: str) -> bool:
    """True when an expected date text carries no real date.

    That is a blank template line (an underscore or the word "blank") or text that
    names no month and no year from 1900 to 2099, unless it is a relative date.
    """
    if "_" in expected or "blank" in expected.lower():
        return True
    if expected_date_is_relative(expected):
        return False
    lowered = expected.lower()
    if not any(month in lowered for month in MONTH_NAMES):
        if not re.search(r"\b(19|20)\d{2}\b", lowered):
            return True
    return False


def date_is_blank_or_bare_label(expected: object) -> bool:
    """True for a blank template line ("_____ day of ____") or a bare label ("Effective Date:")."""
    lowered = str(expected).strip().lower()
    return (
        "_" in lowered
        or "blank" in lowered
        or lowered.endswith(":")
        or bool(re.fullmatch(r"[a-z ]*\bdate\b[a-z ]*", lowered))
    )


def _date_phrase_key(text: object) -> str:
    """Lowercase a date phrase, drop ordinal suffixes and collapse every separator to one space."""
    without_ordinals = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", str(text).lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", without_ordinals)).strip()


def contains_phrase(haystack: str, phrase: str) -> bool:
    """True when ``phrase`` occurs in ``haystack`` with no letter or digit touching either end."""
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", haystack))


def date_phrase_contained(container: object, target: object) -> bool:
    """True when the date phrase stated in ``target`` appears in ``container``.

    Both sides are compared as lowercase words. The phrase counts when it occurs on
    word boundaries and is explicit: three or more words, a month name after another
    word ("18 august"), a month name with a number ("august 18", "august 2014"), or
    a year-month form ("2014-08"). Failing that, a fully dated target also counts
    when its ISO form, "month day year" or "day month year" occurs in the container.
    """
    container_key = _date_phrase_key(container)
    if not container_key:
        return False
    target_key = _date_phrase_key(target)
    if not target_key:
        return False
    target_date = parse_date(target)
    explicit_month = (
        any(f" {month}" in target_key for month in MONTH_NAMES)
        or (
            any(re.search(rf"\b{month}\b", target_key) for month in MONTH_NAMES)
            and bool(re.search(r"\b\d+(?:st|nd|rd|th)?\b", target_key))
        )
        or bool(re.search(r"\b\d{4}-\d{1,2}\b", target_key))
    )
    if contains_phrase(container_key, target_key) and (
        len(target_key.split()) >= 3 or explicit_month
    ):
        return True
    if target_date is None:
        return False
    month = MONTH_NAMES[target_date.month - 1]
    renderings = (
        f"{target_date.year:04d}-{target_date.month:02d}-{target_date.day:02d}",
        f"{month} {target_date.day} {target_date.year}",
        f"{target_date.day} {month} {target_date.year}",
    )
    return any(contains_phrase(container_key, _date_phrase_key(rendering)) for rendering in renderings)
