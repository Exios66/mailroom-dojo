"""Shared pytest fixtures for the labels tests (plain helpers live in labels_testkit.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from labels_testkit import profile as _profile
from labels_testkit import standard_set
from mailroom_labels.profile import ClientProfile

# Same profile names as the record tests, so one HYPOTHESIS_PROFILE drives both suites.
settings.register_profile(
    "record", max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "thorough", max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "record"))


@pytest.fixture
def profile() -> ClientProfile:
    """The synthetic client profile."""
    return _profile()


@pytest.fixture
def snapshot_dirs(tmp_path: Path) -> list[Path]:
    """The clean four-layer synthetic set (hub, interim, near_gold, review)."""
    return standard_set(tmp_path)
