"""The shared fixtures, exercised the way another package uses them: a conftest that lists
``mailroom_common.pytest_plugin`` in ``pytest_plugins``, run in a separate pytest process."""

from __future__ import annotations

import pytest

CONFTEST = """
import sys
import types
from datetime import datetime

pytest_plugins = ["mailroom_common.pytest_plugin"]

try:
    import mailroom_common.clock  # noqa: F401
except ImportError:  # clock.py is written by another builder; stand in for it
    class ManualClock:
        def __init__(self, start: datetime) -> None:
            self._now = start

        def now(self) -> datetime:
            return self._now

    stub = types.ModuleType("mailroom_common.clock")
    stub.ManualClock = ManualClock
    sys.modules["mailroom_common.clock"] = stub
"""

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


def test_fixtures_work_from_a_consumer_conftest(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_consumer=TESTS)
    result = pytester.runpytest_subprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=6)


def test_failing_fixture_use_is_reported_not_hidden(pytester: pytest.Pytester) -> None:
    """Positive control: the subprocess run really executes the assertions."""
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(
        test_bad="""
def test_fake_model_scripted_twice(fake_model):
    from mailroom_common.models import ChatMessage, ModelRequest
    request = ModelRequest(messages=[ChatMessage(role="user", content="x")], max_output_tokens=1)
    fake_model.complete(request)
"""
    )
    result = pytester.runpytest_subprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*ScriptExhaustedError*script exhausted: call 1 has no scripted reply*"])
