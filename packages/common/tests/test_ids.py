from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mailroom_common.clock import ManualClock
from mailroom_common.errors import ConfigError
from mailroom_common.hashing import hash_obj
from mailroom_common.ids import (
    MAX_SLUG_LENGTH,
    ParsedRunId,
    new_run_id,
    parse_run_id,
    short_hash,
    slugify,
    validate_slug,
)

START = datetime(2026, 11, 18, 8, 42, 0, tzinfo=UTC)
RUN_ID_SHAPE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")


def test_run_id_shape_and_timestamp() -> None:
    run_id = new_run_id(ManualClock(START), random.Random(1))
    assert RUN_ID_SHAPE.fullmatch(run_id)
    assert run_id.startswith("20261118T084200Z-")


def test_run_id_is_deterministic_under_manual_clock_and_seeded_rng() -> None:
    first = new_run_id(ManualClock(START), random.Random(42))
    second = new_run_id(ManualClock(START), random.Random(42))
    other_seed = new_run_id(ManualClock(START), random.Random(43))
    assert first == second
    assert first != other_seed


def test_run_id_uses_utc_whatever_the_clock_zone() -> None:
    from datetime import timezone

    clock = ManualClock(datetime(2026, 11, 18, 3, 42, tzinfo=timezone(timedelta(hours=-5))))
    assert new_run_id(clock, random.Random(0)).startswith("20261118T084200Z-")


def test_run_id_refuses_a_clock_that_returns_a_naive_datetime() -> None:
    class NaiveClock:
        def now(self) -> datetime:
            return datetime(2026, 1, 1, 0, 0, 0)

    with pytest.raises(ValueError, match="clock returned a naive datetime"):
        new_run_id(NaiveClock(), random.Random(0))


def test_run_id_default_sources_work() -> None:
    assert RUN_ID_SHAPE.fullmatch(new_run_id())
    assert len({new_run_id() for _ in range(50)}) == 50


def test_run_id_suffix_is_zero_padded() -> None:
    class Zero:
        def getrandbits(self, k: int, /) -> int:
            return 0

    assert new_run_id(ManualClock(START), Zero()) == "20261118T084200Z-00000000"


def test_run_id_for_years_below_1000_keeps_four_digits() -> None:
    clock = ManualClock(datetime(5, 1, 2, 3, 4, 5, tzinfo=UTC))
    run_id = new_run_id(clock, random.Random(0))
    assert run_id.startswith("00050102T030405Z-")
    assert parse_run_id(run_id).timestamp == datetime(5, 1, 2, 3, 4, 5, tzinfo=UTC)


@given(
    st.datetimes(min_value=datetime(1, 1, 1), max_value=datetime(9999, 12, 31), timezones=st.just(UTC)),
    st.datetimes(min_value=datetime(1, 1, 1), max_value=datetime(9999, 12, 31), timezones=st.just(UTC)),
    st.integers(0, 2**32 - 1),
    st.integers(0, 2**32 - 1),
)
def test_lexical_order_is_time_order(a: datetime, b: datetime, seed_a: int, seed_b: int) -> None:
    id_a = new_run_id(ManualClock(a), random.Random(seed_a))
    id_b = new_run_id(ManualClock(b), random.Random(seed_b))
    second_a, second_b = a.replace(microsecond=0), b.replace(microsecond=0)
    if second_a < second_b:
        assert id_a < id_b
    elif second_a > second_b:
        assert id_a > id_b


@given(st.datetimes(min_value=datetime(1, 1, 1), timezones=st.just(UTC)), st.integers(0, 2**32 - 1))
def test_parse_round_trips(moment: datetime, seed: int) -> None:
    run_id = new_run_id(ManualClock(moment), random.Random(seed))
    parsed = parse_run_id(run_id)
    assert parsed == ParsedRunId(timestamp=moment.replace(microsecond=0), suffix=run_id[-8:])
    assert parsed.timestamp.tzinfo is UTC


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "20261118T084200Z",
        "20261118T084200Z-3FA9C1D2",
        "20261118T084200Z-3fa9c1d",
        "20261118T084200-3fa9c1d2",
        "20261118T084200Z-3fa9c1d2\n",
        " 20261118T084200Z-3fa9c1d2",
        "2026-11-18T08:42:00Z-3fa9c1d2",
    ],
)
def test_parse_refuses_malformed_ids(bad: str) -> None:
    with pytest.raises(ConfigError, match="not a run id") as info:
        parse_run_id(bad)
    assert info.value.context["run_id"] == bad


@pytest.mark.parametrize(
    "bad", ["20261318T084200Z-3fa9c1d2", "20260230T084200Z-3fa9c1d2", "20261118T246000Z-3fa9c1d2"]
)
def test_parse_refuses_impossible_dates(bad: str) -> None:
    with pytest.raises(ConfigError, match="impossible timestamp"):
        parse_run_id(bad)


def test_parse_refuses_non_strings() -> None:
    with pytest.raises(ConfigError, match="not a run id"):
        parse_run_id(20261118)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "good", ["a", "0", "amfam", "deploy_v1", "client-2.v3", "a" * MAX_SLUG_LENGTH, "a.-_b"]
)
def test_valid_slugs_pass_unchanged(good: str) -> None:
    assert validate_slug(good) == good


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("", "must not be empty"),
        ("..", "must not contain '..'"),
        ("a..b", "must not contain '..'"),
        (".", "must be lowercase"),
        (".hidden", "must be lowercase"),
        ("-rf", "must be lowercase"),
        ("_x", "must be lowercase"),
        ("a/b", "must be lowercase"),
        ("a\\b", "must be lowercase"),
        ("a\x00b", "must be lowercase"),
        ("Upper", "must be lowercase"),
        ("with space", "must be lowercase"),
        ("ü", "must be lowercase"),
        ("a\n", "must be lowercase"),
        ("a" * (MAX_SLUG_LENGTH + 1), f"longer than {MAX_SLUG_LENGTH}"),
    ],
)
def test_invalid_slugs_are_refused_with_reason(bad: str, reason: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(reason)) as info:
        validate_slug(bad)
    assert info.value.context["name"] == bad


def test_validate_slug_refuses_non_strings() -> None:
    with pytest.raises(ConfigError, match="must be a string, got NoneType"):
        validate_slug(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("AmFam Insurance", "amfam-insurance"),
        ("Ünïcödé Straße", "unicode-strasse"),
        ("../../etc/passwd", "etc-passwd"),
        ("...hidden", "hidden"),
        ("--rf", "rf"),
        ("a....b", "a.b"),
        ("a\\b\x00c", "a-b-c"),
        ("ＦＵＬＬＷＩＤＴＨ", "fullwidth"),
    ],
)
def test_slugify_examples(text: str, expected: str) -> None:
    assert slugify(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "🙂🙂", "///", "..", "中文", "\ud800"])
def test_slugify_falls_back_to_a_deterministic_hash_slug(text: str) -> None:
    slug = slugify(text)
    assert re.fullmatch(r"x-[0-9a-f]{12}", slug)
    assert slugify(text) == slug
    assert validate_slug(slug) == slug


def test_slugify_fallbacks_differ_for_different_texts() -> None:
    assert slugify("🙂") != slugify("🙃")


def test_slugify_cuts_long_text() -> None:
    assert slugify("A" * 500) == "a" * MAX_SLUG_LENGTH


@given(st.text())
def test_slugify_always_returns_a_valid_slug(text: str) -> None:
    assert validate_slug(slugify(text))


@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=200))
def test_slugify_is_idempotent(text: str) -> None:
    once = slugify(text)
    assert slugify(once) == once


@given(st.from_regex(r"[a-z0-9][a-z0-9._-]{0,63}", fullmatch=True).filter(lambda s: ".." not in s))
def test_valid_slugs_are_fixed_points_of_slugify(slug: str) -> None:
    assert slugify(slug) == validate_slug(slug)


def test_short_hash_is_a_prefix_of_hash_obj() -> None:
    obj = {"b": [1, 2], "a": "x"}
    assert short_hash(obj) == hash_obj(obj)[:12]
    assert short_hash(obj, 4) == hash_obj(obj)[:4]
    assert short_hash(obj, 64) == hash_obj(obj)
    assert short_hash({"a": "x", "b": [1, 2]}) == short_hash(obj)


@pytest.mark.parametrize("length", [0, 3, 65, -1])
def test_short_hash_refuses_bad_lengths(length: int) -> None:
    with pytest.raises(ValueError, match=f"between 4 and 64, got {length}"):
        short_hash({}, length)
