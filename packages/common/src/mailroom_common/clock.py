"""Injectable time: everything that needs the current time takes a :class:`Clock`.

Production code passes :class:`SystemClock`; tests pass :class:`ManualClock` and move it
by hand, so no test ever sleeps or depends on the wall clock. Every clock returns an
aware datetime in UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "ManualClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """A source of the current time."""

    def now(self) -> datetime:
        """The current time as an aware datetime in UTC."""
        ...


class SystemClock:
    """The real wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


def _require_aware(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"naive datetime {value.isoformat()} refused: give it a timezone")
    return value.astimezone(UTC)


class ManualClock:
    """A clock that only moves when told to.

    The start time and every :meth:`set` must be timezone-aware (a naive datetime raises
    ``ValueError``, because its meaning depends on the machine); the value is converted to
    UTC. :meth:`advance` refuses a negative step so code under test never sees time run
    backwards by accident; :meth:`set` may move it anywhere on purpose.
    """

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = _require_aware(value)

    def advance(self, step: timedelta | float) -> datetime:
        """Move forward by ``step`` (a timedelta or seconds) and return the new time."""
        delta = step if isinstance(step, timedelta) else timedelta(seconds=step)
        if delta < timedelta(0):
            raise ValueError(f"advance step must not be negative, got {delta}")
        self._now = self._now + delta
        return self._now
