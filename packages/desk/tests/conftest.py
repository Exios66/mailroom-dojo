"""Shared pytest fixtures for the desk tests (plain helpers live in desk_testkit.py)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, settings

from desk_testkit import FakeClock, make_run, policy
from mailroom_desk import Desk

# Same profile names as the record and labels tests, so one HYPOTHESIS_PROFILE drives all.
settings.register_profile(
    "record", max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "thorough", max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "record"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def env(tmp_path: Path, clock: FakeClock) -> SimpleNamespace:
    """A record root with experiment run ``r1`` (unsealed), a desk on it with a fake clock,
    and the synthetic policy (sampling off, so only causes put documents in review)."""
    log = make_run(tmp_path, "r1")
    desk = Desk(tmp_path, clock=clock, lock_timeout=5)
    return SimpleNamespace(
        root=tmp_path,
        log=log,
        blobs=log.blobs,
        desk=desk,
        clock=clock,
        policy=policy(sample={"rate": 0.0, "seed": ""}),
    )
