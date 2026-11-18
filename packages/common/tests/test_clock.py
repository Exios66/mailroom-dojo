from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from mailroom_common.clock import Clock, ManualClock, SystemClock

START = datetime(2026, 11, 18, 8, 42, 0, tzinfo=UTC)


def test_system_clock_is_aware_utc_and_current() -> None:
    before = datetime.now(UTC)
    now = SystemClock().now()
    after = datetime.now(UTC)
    assert now.tzinfo is UTC
    assert before <= now <= after


def test_both_clocks_satisfy_the_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(ManualClock(START), Clock)


def test_manual_clock_stands_still_until_moved() -> None:
    clock = ManualClock(START)
    assert clock.now() == START
    assert clock.now() == START
    assert clock.advance(timedelta(minutes=5)) == START + timedelta(minutes=5)
    assert clock.advance(1.5) == START + timedelta(minutes=5, seconds=1.5)
    assert clock.now() == START + timedelta(minutes=5, seconds=1.5)
    clock.advance(0)
    assert clock.now() == START + timedelta(minutes=5, seconds=1.5)


def test_manual_clock_converts_other_zones_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    clock = ManualClock(datetime(2026, 11, 18, 10, 42, tzinfo=plus_two))
    assert clock.now() == START
    assert clock.now().tzinfo is UTC
    assert clock.now().hour == 8


def test_manual_clock_refuses_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="naive datetime 2026-11-18T08:42:00 refused"):
        ManualClock(datetime(2026, 11, 18, 8, 42))
    clock = ManualClock(START)
    with pytest.raises(ValueError, match="naive"):
        clock.set(datetime(2027, 1, 1))
    assert clock.now() == START


def test_manual_clock_refuses_non_datetimes() -> None:
    with pytest.raises(TypeError, match="expected a datetime, got str"):
        ManualClock("2026-11-18")  # type: ignore[arg-type]


def test_set_may_move_backwards_but_advance_may_not() -> None:
    clock = ManualClock(START)
    clock.set(START - timedelta(days=1))
    assert clock.now() == START - timedelta(days=1)
    with pytest.raises(ValueError, match="must not be negative"):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError, match="must not be negative"):
        clock.advance(-0.001)
    assert clock.now() == START - timedelta(days=1)
