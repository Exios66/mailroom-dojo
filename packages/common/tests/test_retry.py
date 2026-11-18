from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from mailroom_common.models import (
    ContextLengthError,
    PermanentProviderError,
    ProviderError,
    RateLimitError,
    TransientProviderError,
)
from mailroom_common.retry import RetryPolicy, call_with_retry


class Recorder:
    """Injected sleep: records waits instead of sleeping."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def failing(errors: Sequence[BaseException], result: str = "ok") -> Callable[[], str]:
    calls = iter(errors)

    def fn() -> str:
        try:
            error = next(calls)
        except StopIteration:
            return result
        raise error

    return fn


def mid() -> float:
    return 0.5  # jitter factor exactly 1


def test_succeeds_first_try_without_sleeping() -> None:
    sleep = Recorder()
    assert call_with_retry(lambda: "ok", RetryPolicy(), sleep=sleep, rng=mid) == "ok"
    assert sleep.waits == []


def test_exact_exponential_sequence_without_jitter_effect() -> None:
    sleep = Recorder()
    retries: list[tuple[int, str, float]] = []
    fn = failing([TransientProviderError(f"t{n}") for n in range(4)])
    result = call_with_retry(
        fn, RetryPolicy(), sleep=sleep, rng=mid, on_retry=lambda a, e, d: retries.append((a, e.message, d))
    )
    assert result == "ok"
    assert sleep.waits == [1.0, 2.0, 4.0, 8.0]
    assert retries == [(1, "t0", 1.0), (2, "t1", 2.0), (3, "t2", 4.0), (4, "t3", 8.0)]


def test_jitter_bounds_follow_rng() -> None:
    low, high = Recorder(), Recorder()
    errors = [TransientProviderError("t")] * 3
    call_with_retry(failing(list(errors)), RetryPolicy(), sleep=low, rng=lambda: 0.0)
    call_with_retry(failing(list(errors)), RetryPolicy(), sleep=high, rng=lambda: 1.0)
    assert low.waits == pytest.approx([0.7, 1.4, 2.8])
    assert high.waits == pytest.approx([1.3, 2.6, 5.2])


def test_rate_limit_uses_its_own_base_and_is_capped() -> None:
    sleep = Recorder()
    policy = RetryPolicy(max_attempts=6)
    call_with_retry(failing([RateLimitError("429")] * 5), policy, sleep=sleep, rng=mid)
    assert sleep.waits == [8.0, 16.0, 32.0, 60.0, 60.0]


def test_jitter_never_pushes_past_max_delay() -> None:
    sleep = Recorder()
    call_with_retry(failing([RateLimitError("429")] * 4), RetryPolicy(), sleep=sleep, rng=lambda: 1.0)
    assert sleep.waits == pytest.approx([10.4, 20.8, 41.6, 60.0])


def test_retry_after_raises_the_wait_but_is_capped() -> None:
    sleep = Recorder()
    errors = [
        TransientProviderError("t", retry_after=5.0),  # above the 1.0 ladder step
        TransientProviderError("t", retry_after=0.5),  # below the 2.0 step: ladder wins
        RateLimitError("429", retry_after=60.0),  # exactly the cap: still waited
    ]
    call_with_retry(failing(errors), RetryPolicy(), sleep=sleep, rng=mid)
    assert sleep.waits == [5.0, 2.0, 60.0]


def test_retry_after_beyond_max_delay_raises_at_once_without_burning_an_attempt() -> None:
    sleep = Recorder()
    retried: list[int] = []
    errors: list[BaseException] = [TransientProviderError("t"), RateLimitError("429", retry_after=3600.0)]
    calls: list[int] = []
    fn = failing(errors)

    def counted() -> str:
        calls.append(1)
        return fn()

    with pytest.raises(RateLimitError) as info:
        call_with_retry(
            counted, RetryPolicy(), sleep=sleep, rng=mid, on_retry=lambda a, e, d: retried.append(a)
        )
    assert info.value is errors[1]
    assert info.value.context == {
        "retry_after": 3600.0,
        "attempts": 2,
        "gave_up": "retry_after exceeds max_delay",
    }
    assert calls == [1, 1]
    assert sleep.waits == [1.0]
    assert retried == [1]


def test_jitter_of_one_is_refused_because_it_allows_a_zero_wait() -> None:
    with pytest.raises(ValidationError, match="jitter"):
        RetryPolicy(jitter=1.0)
    assert RetryPolicy(jitter=0.999).jitter == 0.999


@pytest.mark.parametrize(
    "fields", [{"max_attempts": "3"}, {"max_attempts": 3.0}, {"base_delay": "1.0"}, {"jitter": True}]
)
def test_policy_is_strict_about_types(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RetryPolicy(**fields)


def test_exhausted_ladder_raises_last_error_with_attempt_count() -> None:
    sleep = Recorder()
    errors = [TransientProviderError(f"t{n}", status=503) for n in range(3)]
    with pytest.raises(TransientProviderError) as info:
        call_with_retry(failing(errors), RetryPolicy(max_attempts=3), sleep=sleep, rng=mid)
    assert info.value is errors[2]
    assert info.value.context == {"status": 503, "attempts": 3}
    assert str(info.value) == "t2 (attempts=3, status=503)"
    assert sleep.waits == [1.0, 2.0]  # no sleep after the final attempt


@pytest.mark.parametrize("error", [ContextLengthError("too long"), PermanentProviderError("401")])
def test_non_retryable_error_is_raised_at_once(error: ProviderError) -> None:
    sleep = Recorder()
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        raise error

    with pytest.raises(type(error)) as info:
        call_with_retry(fn, RetryPolicy(), sleep=sleep, rng=mid)
    assert info.value is error
    assert info.value.context["attempts"] == 1
    assert calls == [1]
    assert sleep.waits == []


def test_non_retryable_after_retryable_stops_the_ladder() -> None:
    sleep = Recorder()
    errors: list[BaseException] = [TransientProviderError("t"), PermanentProviderError("bad request")]
    with pytest.raises(PermanentProviderError) as info:
        call_with_retry(failing(errors), RetryPolicy(), sleep=sleep, rng=mid)
    assert info.value.context["attempts"] == 2
    assert sleep.waits == [1.0]


def test_bugs_are_not_retried() -> None:
    sleep = Recorder()
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        raise KeyError("bug")

    with pytest.raises(KeyError):
        call_with_retry(fn, RetryPolicy(), sleep=sleep, rng=mid)
    assert calls == [1]
    assert sleep.waits == []


def test_one_attempt_policy_never_sleeps() -> None:
    sleep = Recorder()
    with pytest.raises(RateLimitError):
        call_with_retry(failing([RateLimitError("429")]), RetryPolicy(max_attempts=1), sleep=sleep, rng=mid)
    assert sleep.waits == []


def test_rng_out_of_range_is_refused() -> None:
    with pytest.raises(ValueError, match=r"rng must return a value in \[0, 1\], got 1.5"):
        call_with_retry(
            failing([TransientProviderError("t")]), RetryPolicy(), sleep=Recorder(), rng=lambda: 1.5
        )


def test_defaults_match_the_carried_over_ladder() -> None:
    assert RetryPolicy().model_dump() == {
        "max_attempts": 5,
        "base_delay": 1.0,
        "rate_limit_base_delay": 8.0,
        "max_delay": 60.0,
        "jitter": 0.3,
    }


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"base_delay": 0}, "base_delay"),
        ({"rate_limit_base_delay": -1}, "rate_limit_base_delay"),
        ({"max_delay": 0}, "max_delay"),
        ({"jitter": 1.5}, "jitter"),
        ({"jitter": -0.1}, "jitter"),
        ({"max_delay": 4.0}, r"max_delay \(4.0\) is below"),
        ({"retries": 3}, "retries"),
    ],
)
def test_policy_validation(fields: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        RetryPolicy(**fields)


@given(
    attempt=st.integers(1, 200),
    u=st.floats(0.0, 1.0),
    retry_after=st.none() | st.floats(0.0, 1e6),
    rate_limited=st.booleans(),
    jitter=st.floats(0.0, 1.0, exclude_max=True),
)
def test_property_delay_is_within_zero_and_max(
    attempt: int, u: float, retry_after: float | None, rate_limited: bool, jitter: float
) -> None:
    policy = RetryPolicy(jitter=jitter)
    error: ProviderError = (
        RateLimitError("r", retry_after=retry_after)
        if rate_limited
        else TransientProviderError("t", retry_after=retry_after)
    )
    wait = policy.delay(attempt, error, u)
    assert 0.0 <= wait <= policy.max_delay
    if retry_after is not None:
        assert wait >= min(retry_after, policy.max_delay)


@given(u=st.floats(0.0, 1.0))
def test_property_delays_never_decrease_for_a_fixed_draw(u: float) -> None:
    policy = RetryPolicy()
    waits = [policy.delay(n, TransientProviderError("t"), u) for n in range(1, 12)]
    assert waits == sorted(waits)
