"""Scores must not depend on the Python version: every float mean in the Dojo adds up its values
with ``compensated_sum``, which gives CPython 3.12's ``sum()`` result on every supported version
(3.11's ``sum()`` adds floats naively, so a rounded mean could differ in its last digit)."""

from __future__ import annotations

import math
import random
import sys

import pytest

from mailroom_dojo.values.summation import _neumaier_sum, compensated_sum


def test_ten_tenths_add_up_to_one_on_every_python() -> None:
    assert compensated_sum([0.1] * 10) == 1.0


def test_an_empty_sum_is_the_integer_zero_and_integers_stay_exact() -> None:
    assert compensated_sum([]) == 0 and type(compensated_sum([])) is int
    assert compensated_sum([2**80, 1, True]) == 2**80 + 2


def _cases() -> list[list[object]]:
    rng = random.Random(20261119)
    cases: list[list[object]] = [
        [],
        [0.0],
        [-0.0],
        [1, 0.5, 2],
        [1e308, 1e308],
        [math.inf, -1.0],
        [math.inf, -math.inf],
        [math.nan, 1.0],
        [1e100, 1.0, -1e100],
        [0.1, 0.2, 0.3],
        [3, 0.1, True, 2**70, 0.2],
        [2**80, 0.1, 0.2, 0.3],
        [2**62, 2**62, 0.1, 0.2],
        [2**63 - 1, 1, 0.1, 0.2],
        [-(2**63), 0.1, 0.2],
        [-(2**63), 2**64, 0.1, 0.2],
    ]
    for _ in range(3000):
        n = rng.randint(1, 12)
        row: list[object] = []
        for _ in range(n):
            pick = rng.random()
            if pick < 0.7:
                row.append(round(rng.uniform(-1, 1), rng.randint(1, 17)))
            elif pick < 0.85:
                row.append(rng.uniform(-1e16, 1e16))
            else:
                row.append(rng.randint(-5, 5))
        cases.append(row)
    return cases


@pytest.mark.skipif(sys.version_info < (3, 12), reason="the reference is CPython 3.12's own sum()")
def test_the_port_matches_the_builtin_sum_of_python_3_12_and_later() -> None:
    for values in _cases():
        got, want = _neumaier_sum(values), sum(values)  # type: ignore[arg-type]
        assert type(got) is type(want), values
        assert got == want or (math.isnan(got) and math.isnan(want)), values
        assert math.copysign(1.0, got) == math.copysign(1.0, want), values
