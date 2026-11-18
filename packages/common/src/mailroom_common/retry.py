"""Bounded exponential backoff for provider calls.

The ladder (defaults carried over from the current pipeline's ``llm_retry`` settings): after
failed attempt ``n`` (1-based) the wait is ``base * 2 ** (n - 1)``, capped at ``max_delay``,
then scaled by a jitter factor ``1 + jitter * (2u - 1)`` with ``u`` drawn from ``rng`` in
``[0, 1)``. A :class:`RateLimitError` starts from ``rate_limit_base_delay`` instead of
``base_delay``. A provider's ``retry_after`` raises the wait to at least that value. The
final wait never exceeds ``max_delay``, whatever the jitter or ``retry_after`` say.

Only :class:`ProviderError` is caught. A non-retryable one is raised at once; any other
exception is a bug and propagates untouched. When the ladder is exhausted the last error is
raised, with ``attempts`` (how many calls were made) added to its ``context``. A
retryable error whose ``retry_after`` exceeds ``max_delay`` is raised at once (a retry
before the provider's time would only fail and burn an attempt), with ``attempts`` and
``gave_up="retry_after exceeds max_delay"`` in its context. :meth:`RetryPolicy.delay`
itself still caps such a wait at ``max_delay``.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mailroom_common.models import ProviderError, RateLimitError

__all__ = ["RetryPolicy", "call_with_retry"]

T = TypeVar("T")


class RetryPolicy(BaseModel):
    """How many times to call and how long to wait between calls (seconds).

    Strict: ``max_attempts="3"`` or ``3.0`` is refused (an int is accepted for a float).
    ``jitter`` must be below 1, so a wait can never shrink to zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    max_attempts: int = Field(default=5, ge=1)
    base_delay: float = Field(default=1.0, gt=0.0)
    rate_limit_base_delay: float = Field(default=8.0, gt=0.0)
    max_delay: float = Field(default=60.0, gt=0.0)
    jitter: float = Field(default=0.3, ge=0.0, lt=1.0)

    @model_validator(mode="after")
    def _cap_above_bases(self) -> RetryPolicy:
        if self.max_delay < max(self.base_delay, self.rate_limit_base_delay):
            raise ValueError(
                f"max_delay ({self.max_delay}) is below base_delay ({self.base_delay}) "
                f"or rate_limit_base_delay ({self.rate_limit_base_delay})"
            )
        return self

    def delay(self, attempt: int, error: ProviderError, u: float) -> float:
        """The wait after failed attempt ``attempt`` (1-based), given a uniform draw ``u``."""
        if attempt < 1:
            raise ValueError(f"attempt is 1-based, got {attempt}")
        if not 0.0 <= u <= 1.0:
            raise ValueError(f"rng must return a value in [0, 1], got {u!r}")
        base = self.rate_limit_base_delay if isinstance(error, RateLimitError) else self.base_delay
        wait = min(self.max_delay, base * 2.0 ** min(attempt - 1, 64))
        wait *= 1.0 + self.jitter * (2.0 * u - 1.0)
        if error.retry_after is not None:
            wait = max(wait, error.retry_after)
        return min(self.max_delay, wait)


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], object] = time.sleep,
    rng: Callable[[], float] = random.random,
    on_retry: Callable[[int, ProviderError, float], object] | None = None,
) -> T:
    """Call ``fn`` until it returns, retrying retryable :class:`ProviderError` per ``policy``.

    ``on_retry(attempt, error, delay)`` is called before each wait. ``sleep`` and ``rng``
    are injectable so tests are instant and deterministic.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except ProviderError as error:
            if not error.retryable or attempt >= policy.max_attempts:
                error.context = {**error.context, "attempts": attempt}
                raise
            if error.retry_after is not None and error.retry_after > policy.max_delay:
                # A retry sooner than the provider asked would only fail again and burn an attempt.
                error.context = {
                    **error.context,
                    "attempts": attempt,
                    "gave_up": "retry_after exceeds max_delay",
                }
                raise
            wait = policy.delay(attempt, error, rng())
            if on_retry is not None:
                on_retry(attempt, error, wait)
        sleep(wait)
