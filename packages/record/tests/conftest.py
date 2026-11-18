"""Shared pytest fixtures for the record tests (plain helpers live in record_testkit.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import os

import pytest
from hypothesis import HealthCheck, settings
from record_testkit import run_fields

from mailroom_record.blobs import BlobStore
from mailroom_record.log import EventLog
from mailroom_record.models import Run

# Hypothesis profiles. "record" (default) keeps the suite fast enough for every run;
# "thorough" (HYPOTHESIS_PROFILE=thorough) explores ten times more cases for a soak.
# deadline=None: these tests touch the disk, so a single example's time varies.
settings.register_profile(
    "record", max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "thorough", max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "record"))


@pytest.fixture
def store(tmp_path: Path) -> SimpleNamespace:
    """A blob store and event log under ``tmp_path`` with run ``r1`` created."""
    blobs = BlobStore(tmp_path)
    log = EventLog(tmp_path, blobs)
    run = Run(**run_fields())
    log.create_run(run)
    return SimpleNamespace(root=tmp_path, blobs=blobs, log=log, run=run)


@pytest.fixture
def store_with_events(store: SimpleNamespace) -> SimpleNamespace:
    """``store`` with flushed events: ``d1`` has 2, ``d2`` has 1 (writer ``w0``, closed)."""
    with store.log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="classify", kind="decision", decision={"chosen": "contract"})
        w.append(doc_id="d1", node="extract", kind="output")
        w.append(doc_id="d2", node="classify", kind="decision")
    return store
