"""Shared pytest fixtures. Enable in a package's ``tests/conftest.py`` with
``pytest_plugins = ["mailroom_common.pytest_plugin"]``.

Fixtures:

* ``frozen_clock``: a ``ManualClock`` at 2026-01-01T00:00:00Z (``testing.FROZEN_START``);
* ``fake_model``: an empty :class:`~mailroom_common.testing.FakeModelClient` in scripted
  mode; tests needing a script or handler build their own;
* ``write_yaml``, ``write_json``, ``write_jsonl``: ``(relative_path, data) -> Path``
  writers rooted at ``tmp_path``;
* ``json_log_records``: a :class:`~mailroom_common.testing.JsonLogCapture` installed for
  the test; ``.records`` holds the parsed lines.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from mailroom_common import testing

__all__ = [
    "fake_model",
    "frozen_clock",
    "json_log_records",
    "write_json",
    "write_jsonl",
    "write_yaml",
]

Writer = Callable[[str | Path, Any], Path]


@pytest.fixture
def frozen_clock() -> Any:
    return testing.make_frozen_clock()


@pytest.fixture
def fake_model() -> testing.FakeModelClient:
    return testing.FakeModelClient()


@pytest.fixture
def write_yaml(tmp_path: Path) -> Writer:
    return partial(testing.write_yaml, tmp_path)


@pytest.fixture
def write_json(tmp_path: Path) -> Writer:
    return partial(testing.write_json, tmp_path)


@pytest.fixture
def write_jsonl(tmp_path: Path) -> Writer:
    return partial(testing.write_jsonl, tmp_path)


@pytest.fixture
def json_log_records() -> Iterator[testing.JsonLogCapture]:
    capture = testing.JsonLogCapture()
    capture.install()
    try:
        yield capture
    finally:
        capture.uninstall()
