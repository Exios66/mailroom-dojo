"""Shared pytest fixtures for the record tests (plain helpers live in record_testkit.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import os

import pytest
from hypothesis import HealthCheck, settings
from record_testkit import copy_fixture, crash_fixture, run_fields

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


@pytest.fixture
def tmp_store(tmp_path: Path) -> SimpleNamespace:
    """An empty store (no run yet) under ``tmp_path/store``."""
    root = tmp_path / "store"
    root.mkdir()
    blobs = BlobStore(root)
    return SimpleNamespace(root=root, blobs=blobs, log=EventLog(root, blobs))


@pytest.fixture
def fixture_folder(tmp_path: Path) -> Path:
    """A private copy of the hand-made sandbox run folder (run ``ar-fixture-r00``)."""
    return copy_fixture(tmp_path / "src" / "ar-fixture-r00")


@pytest.fixture
def fixture_folder_replicate(tmp_path: Path) -> Path:
    """The same three documents, run again as ``ar-fixture-r01``."""
    return copy_fixture(tmp_path / "src" / "ar-fixture-r01", run_id="ar-fixture-r01")


@pytest.fixture
def fixture_folder_no_prompt_lock(tmp_path: Path) -> Path:
    folder = copy_fixture(tmp_path / "src" / "no-prompt-lock")
    (folder / "prompt.lock.json").unlink()
    return folder


@pytest.fixture
def fixture_folder_crashed(tmp_path: Path) -> Path:
    """The fixture stopped mid-document (see ``record_testkit.crash_fixture``)."""
    return crash_fixture(copy_fixture(tmp_path / "src" / "crashed", run_id="ar-fixture-crashed"))
