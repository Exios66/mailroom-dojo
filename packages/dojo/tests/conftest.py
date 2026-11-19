"""Shared pytest fixtures for the Dojo tests (plain helpers live in dojo_testkit.py)."""

from __future__ import annotations

import os
import sys

import pytest
from hypothesis import HealthCheck, settings

from dojo_testkit import MAILROOM_PROFILE_PATH
from mailroom_dojo.profile import ScoringProfile, load_scoring_profile

# Hypothesis profiles. "dojo" (default) keeps the suite fast; "thorough"
# (HYPOTHESIS_PROFILE=thorough) explores ten times more cases for a soak.
settings.register_profile(
    "dojo", max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "dojo-thorough", max_examples=600, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile("dojo-thorough" if os.environ.get("HYPOTHESIS_PROFILE") == "thorough" else "dojo")


@pytest.fixture(scope="session")
def mailroom_profile() -> ScoringProfile:
    """The committed mailroom-dataset scoring profile."""
    return load_scoring_profile(MAILROOM_PROFILE_PATH)


#: CPython's default integer digit limit, which the tests of too-long integers rely on.
DEFAULT_INT_MAX_STR_DIGITS = 4300


@pytest.fixture
def default_int_digit_limit():
    """Run a test under CPython's default integer digit limit, whatever ``PYTHONINTMAXSTRDIGITS`` says,
    and restore the previous limit afterwards."""
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(DEFAULT_INT_MAX_STR_DIGITS)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)
