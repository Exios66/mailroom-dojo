"""Shared pytest fixtures for the Dojo tests (plain helpers live in dojo_testkit.py)."""

from __future__ import annotations

import os

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
