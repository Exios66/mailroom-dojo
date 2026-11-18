from __future__ import annotations

import json
import logging
import sys
import threading
import types
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from mailroom_common.models import (
    ChatMessage,
    ModelRequest,
    ModelResponse,
    RateLimitError,
    TransientProviderError,
    Usage,
)
from mailroom_common.retry import RetryPolicy, call_with_retry
from mailroom_common.testing import (
    FROZEN_START,
    FakeModelClient,
    JsonLogCapture,
    ScriptExhaustedError,
    fake_token_count,
    make_frozen_clock,
    write_json,
    write_jsonl,
    write_yaml,
)
from mailroom_common.yamlio import load_yaml


def ask(content: str = "abcdefgh") -> ModelRequest:
    return ModelRequest(messages=(ChatMessage(role="user", content=content),), max_output_tokens=16)


# --- FakeModelClient ------------------------------------------------------------------------


def test_script_is_consumed_in_order_and_requests_recorded() -> None:
    canned = ModelResponse(
        text="canned",
        usage=Usage(input_tokens=1, output_tokens=1),
        model_id="m",
        latency_ms=3.0,
        finish_reason="stop",
    )
    error = TransientProviderError("blip")
    client = FakeModelClient(["first", error, canned])
    first = client.complete(ask("12345"))
    assert (first.text, first.usage.input_tokens, first.usage.output_tokens) == ("first", 2, 2)
    assert first.model_id == "fake-model"
    with pytest.raises(TransientProviderError) as info:
        client.complete(ask("second"))
    assert info.value is error
    assert client.complete(ask("third")) is canned
    assert [r.messages[0].content for r in client.requests] == ["12345", "second", "third"]
    assert client.calls == 3
    assert client.remaining == 0
    client.assert_exhausted()


def test_exhausted_script_fails_loudly_and_never_reuses() -> None:
    client = FakeModelClient(["only"])
    client.complete(ask())
    with pytest.raises(ScriptExhaustedError, match="script exhausted: call 2 has no scripted reply"):
        client.complete(ask())
    assert client.calls == 2  # the failed call is still recorded


def test_exhaustion_is_not_swallowed_by_provider_error_handling() -> None:
    client = FakeModelClient([TransientProviderError("blip")])
    with pytest.raises(ScriptExhaustedError):
        call_with_retry(lambda: client.complete(ask()), RetryPolicy(), sleep=lambda _: None, rng=lambda: 0.5)


def test_assert_exhausted_reports_unused_items() -> None:
    client = FakeModelClient(["a", "b", "c"])
    client.complete(ask())
    with pytest.raises(AssertionError, match=r"2 unused scripted item\(s\) after 1 call\(s\)"):
        client.assert_exhausted()


def test_handler_mode() -> None:
    def handler(request: ModelRequest) -> str:
        content = request.messages[-1].content
        if content == "throttle":
            raise RateLimitError("429", retry_after=1.0)
        return content.upper()

    client = FakeModelClient(handler=handler, model_id="stub")
    assert client.complete(ask("hello")).text == "HELLO"
    assert client.complete(ask("hello")).model_id == "stub"
    with pytest.raises(RateLimitError):
        client.complete(ask("throttle"))
    assert client.calls == 3
    assert client.remaining == 0


def test_script_and_handler_together_is_refused() -> None:
    with pytest.raises(ValueError, match="either a script or a handler"):
        FakeModelClient(["x"], handler=lambda r: "y")


def test_usage_is_deterministic_from_text_length() -> None:
    assert [fake_token_count("x" * n) for n in (0, 1, 4, 5, 8, 9)] == [0, 1, 1, 2, 2, 3]
    request = ModelRequest(
        messages=(ChatMessage(role="system", content="s" * 6), ChatMessage(role="user", content="u" * 6)),
        max_output_tokens=4,
    )
    usage = FakeModelClient(["y" * 10]).complete(request).usage
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (3, 3, 0)


def test_concurrent_callers_each_get_a_distinct_item() -> None:
    client = FakeModelClient([f"reply-{n}" for n in range(64)])
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(8):
            text = client.complete(ask()).text
            with lock:
                results.append(text)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(results) == sorted(f"reply-{n}" for n in range(64))
    assert client.calls == 64


# --- frozen clock -----------------------------------------------------------------------------


def test_make_frozen_clock_without_clock_module_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mailroom_common.clock", None)  # import now raises ImportError
    with pytest.raises(RuntimeError, match=r"frozen_clock needs mailroom_common\.clock\.ManualClock"):
        make_frozen_clock()


def test_make_frozen_clock_passes_the_fixed_start(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubManualClock:
        def __init__(self, start: datetime) -> None:
            self.start = start

        def now(self) -> datetime:
            return self.start

    stub = types.ModuleType("mailroom_common.clock")
    stub.ManualClock = StubManualClock  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mailroom_common.clock", stub)
    clock = make_frozen_clock()
    assert isinstance(clock, StubManualClock)
    assert clock.now() == FROZEN_START
    assert FROZEN_START.isoformat() == "2026-01-01T00:00:00+00:00"


# --- file writers -----------------------------------------------------------------------------


def test_writers_create_parents_and_round_trip(tmp_path: Path) -> None:
    data: dict[str, Any] = {"name": "ünïcode", "values": [1, 2.5, None, True], "nested": {"no": "on"}}
    yaml_path = write_yaml(tmp_path, "config/clients/acme/profile.yaml", data)
    json_path = write_json(tmp_path, Path("out/data.json"), data)
    jsonl_path = write_jsonl(tmp_path, "logs/rows.jsonl", [data, {"b": 2}])
    assert yaml_path == tmp_path / "config/clients/acme/profile.yaml"
    assert load_yaml(yaml_path) == data  # strict loader agrees with the writer (quoted 'no'/'on')
    assert json.loads(json_path.read_text(encoding="utf-8")) == data
    raw = jsonl_path.read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 2
    assert [json.loads(line) for line in raw.decode("utf-8").splitlines()] == [data, {"b": 2}]


def test_write_jsonl_with_no_rows_writes_an_empty_file(tmp_path: Path) -> None:
    assert write_jsonl(tmp_path, "empty.jsonl", []).read_bytes() == b""


@pytest.mark.parametrize("bad", ["../escape.yaml", "a/../../escape.yaml", "/abs/file.yaml"])
def test_writers_refuse_paths_outside_root(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="must be relative and stay inside"):
        write_yaml(tmp_path / "root", bad, {})
    assert not (tmp_path / "escape.yaml").exists()


def test_write_json_refuses_unserializable_data(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        write_json(tmp_path, "x.json", {"when": object()})


# --- JsonLogCapture ---------------------------------------------------------------------------


def test_capture_installs_and_restores_root_level() -> None:
    root = logging.getLogger()
    before_level, before_handlers = root.level, root.handlers[:]
    capture = JsonLogCapture()
    capture.install()
    try:
        logging.getLogger("x").debug("token=%s", "abc123")
    finally:
        capture.uninstall()
    logging.getLogger("x").warning("after uninstall")
    assert (root.level, root.handlers) == (before_level, before_handlers)
    assert [r["event"] for r in capture.records] == ["token=[REDACTED]"]
    assert len(capture) == 1


def test_writers_refuse_to_follow_a_symlink_out_of_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="must be relative and stay inside"):
        write_json(root, "link/x.json", {"a": 1})
    with pytest.raises(ValueError, match="must be relative and stay inside"):
        write_yaml(root, "link/deeper/x.yaml", {"a": 1})
    assert list(outside.iterdir()) == []
