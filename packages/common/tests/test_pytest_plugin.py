"""The shared fixtures, exercised the way another package uses them: a test directory with
no conftest at all, run in a separate pytest process. The fixtures arrive through the
``pytest11`` entry point that ``mailroom-common`` installs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TESTS = """
import json
import logging

import pytest

from mailroom_common.logging import bind_context
from mailroom_common.testing import FROZEN_START, FakeModelClient, ScriptExhaustedError
from mailroom_common.models import ChatMessage, ModelRequest
from mailroom_common.yamlio import load_yaml


def test_frozen_clock(frozen_clock):
    assert frozen_clock.now() == FROZEN_START
    assert frozen_clock.now() == frozen_clock.now()


def test_fake_model_is_empty_and_strict(fake_model):
    assert isinstance(fake_model, FakeModelClient)
    request = ModelRequest(messages=[ChatMessage(role="user", content="x")], max_output_tokens=1)
    with pytest.raises(ScriptExhaustedError):
        fake_model.complete(request)


def test_writers_are_rooted_at_tmp_path(tmp_path, write_yaml, write_json, write_jsonl):
    assert write_yaml("a/b.yaml", {"k": "no"}) == tmp_path / "a/b.yaml"
    assert load_yaml(tmp_path / "a/b.yaml") == {"k": "no"}
    assert json.loads(write_json("c.json", [1]).read_text()) == [1]
    assert write_jsonl("d.jsonl", [{"x": 1}]).read_text() == '{"x":1}\\n'


def test_json_log_records(json_log_records):
    with bind_context(run_id="r9"):
        logging.getLogger("plugin.check").info("hello %s", "world", extra={"api_key": "sk-abcdefghijkl"})
    (record,) = json_log_records.records
    assert record["event"] == "hello world"
    assert record["run_id"] == "r9"
    assert record["api_key"] == "[REDACTED]"


def test_json_log_records_does_not_leak_between_tests(json_log_records):
    assert json_log_records.records == []
    assert logging.getLogger().level == logging.DEBUG


def test_root_level_restored_after_capture():
    assert logging.getLogger().level == logging.WARNING
"""


def run_pytest(directory: Path) -> subprocess.CompletedProcess[str]:
    """Run pytest on ``directory`` in a fresh interpreter with plug-in autoloading on."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(directory),
            str(directory),
        ],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_fixtures_resolve_without_any_conftest_line(tmp_path: Path) -> None:
    (tmp_path / "test_consumer.py").write_text(TESTS, encoding="utf-8")
    assert not list(tmp_path.rglob("conftest.py"))
    result = run_pytest(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "6 passed" in result.stdout


def test_failing_fixture_use_is_reported_not_hidden(tmp_path: Path) -> None:
    """Positive control: the subprocess run really executes the assertions."""
    (tmp_path / "test_bad.py").write_text(
        """
def test_fake_model_scripted_twice(fake_model):
    from mailroom_common.models import ChatMessage, ModelRequest
    request = ModelRequest(messages=[ChatMessage(role="user", content="x")], max_output_tokens=1)
    fake_model.complete(request)
""",
        encoding="utf-8",
    )
    result = run_pytest(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 failed" in result.stdout
    assert "script exhausted: call 1 has no scripted reply" in result.stdout


def test_plugin_is_registered_as_a_pytest11_entry_point() -> None:
    from importlib.metadata import entry_points

    (point,) = [ep for ep in entry_points(group="pytest11") if ep.name == "mailroom_common"]
    assert point.value == "mailroom_common.pytest_plugin"
