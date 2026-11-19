"""The statistics add floats like CPython 3.12 on every supported Python (see
``mailroom_dojo.values.summation``): ten tenths sum to exactly 1.0, where 3.11's naive
``sum()`` gives 0.9999999999999999."""

from __future__ import annotations

import random

from mailroom_dojo.statistics.bootstrap import resample_means


def test_a_resampled_mean_of_ten_tenths_is_exactly_a_tenth() -> None:
    assert set(resample_means([0.1] * 10, 5, random.Random(7))) == {0.1}
