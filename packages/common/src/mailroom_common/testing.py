"""Test doubles and helpers shared by every package's tests.

Importable without pytest. The pytest fixtures that wrap these live in
``mailroom_common.pytest_plugin``.
"""

from __future__ import annotations

import io
import json
import logging
import math
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from mailroom_common.logging import make_handler
from mailroom_common.models import ModelRequest, ModelResponse, Usage

__all__ = [
    "FROZEN_START",
    "FakeModelClient",
    "JsonLogCapture",
    "ScriptExhaustedError",
    "fake_token_count",
    "make_frozen_clock",
    "write_json",
    "write_jsonl",
    "write_yaml",
]

FROZEN_START = datetime(2026, 1, 1, tzinfo=UTC)
FAKE_MODEL_ID = "fake-model"

ScriptItem = ModelResponse | str | BaseException
Handler = Callable[[ModelRequest], "ModelResponse | str"]


class ScriptExhaustedError(AssertionError):
    """A :class:`FakeModelClient` was called more times than it was scripted for.

    An ``AssertionError`` on purpose: code under test that catches ``ProviderError`` or
    ``MailroomError`` cannot swallow it.
    """


def fake_token_count(text: str) -> int:
    """Deterministic stand-in for a tokenizer: one token per four characters, rounded up."""
    return math.ceil(len(text) / 4)


class FakeModelClient:
    """A :class:`~mailroom_common.models.ModelClient` that never touches a network.

    Scripted mode: each call consumes the next item of ``script``. A ``str`` becomes a
    response with that text, a :class:`ModelResponse` is returned as is, and an exception is
    raised. Calling past the end raises :class:`ScriptExhaustedError`; a response is never
    reused. Callable mode: ``handler(request)`` produces each reply (a ``str`` or a
    response) or raises. Every request is recorded in :attr:`requests`, including ones that
    raised. Usage for ``str`` replies comes from :func:`fake_token_count`. Thread-safe.
    """

    def __init__(
        self,
        script: Iterable[ScriptItem] = (),
        *,
        handler: Handler | None = None,
        model_id: str = FAKE_MODEL_ID,
    ) -> None:
        self._script: list[ScriptItem] = list(script)
        if handler is not None and self._script:
            raise ValueError("give either a script or a handler, not both")
        self._handler = handler
        self._model_id = model_id
        self._lock = threading.Lock()
        self.requests: list[ModelRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def remaining(self) -> int:
        """Scripted items not yet consumed (always 0 in callable mode)."""
        return len(self._script)

    def assert_exhausted(self) -> None:
        """Fail if scripted items were never consumed (the code made fewer calls than planned)."""
        if self._script:
            raise AssertionError(
                f"FakeModelClient has {len(self._script)} unused scripted item(s) after {self.calls} call(s)"
            )

    def complete(self, request: ModelRequest) -> ModelResponse:
        with self._lock:
            self.requests.append(request)
            call = len(self.requests)
            if self._handler is None:
                if not self._script:
                    raise ScriptExhaustedError(
                        f"FakeModelClient script exhausted: call {call} has no scripted reply"
                    )
                item: ScriptItem = self._script.pop(0)
        if self._handler is not None:
            item = self._handler(request)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ModelResponse):
            return item
        return self._response(request, item)

    def _response(self, request: ModelRequest, text: str) -> ModelResponse:
        prompt = "".join(message.content for message in request.messages)
        return ModelResponse(
            text=text,
            usage=Usage(input_tokens=fake_token_count(prompt), output_tokens=fake_token_count(text)),
            model_id=self._model_id,
            latency_ms=0.0,
            finish_reason="stop",
        )


def make_frozen_clock(start: datetime = FROZEN_START) -> Any:
    """A ``mailroom_common.clock.ManualClock`` set to ``start``.

    Imported lazily so this module works before the clock module exists; raises
    ``RuntimeError`` naming the missing module if it cannot be imported.
    """
    try:
        from mailroom_common.clock import ManualClock  # type: ignore[import-not-found, unused-ignore]
    except ImportError as exc:
        raise RuntimeError("frozen_clock needs mailroom_common.clock.ManualClock") from exc
    return ManualClock(start)


def _target(root: Path, name: str | Path) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"path must be relative and stay inside {root}: {name}")
    path = root / relative
    # A symlink inside root must not let a writer escape it: check where the file really lands.
    if not path.parent.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path must be relative and stay inside {root}: {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_yaml(root: Path, name: str | Path, data: Any) -> Path:
    """Dump ``data`` as block-style YAML to ``root/name`` (parents created); return the path."""
    path = _target(root, name)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def write_json(root: Path, name: str | Path, data: Any) -> Path:
    """Dump ``data`` as indented JSON to ``root/name`` (parents created); return the path."""
    path = _target(root, name)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def write_jsonl(root: Path, name: str | Path, rows: Iterable[Any]) -> Path:
    """Write one compact JSON value per line, newline-terminated, to ``root/name``; return the path."""
    path = _target(root, name)
    lines = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows]
    path.write_text("".join(lines), encoding="utf-8")
    return path


class JsonLogCapture:
    """Captures what the workspace's JSON handler would write, from every logger.

    :meth:`install` adds a handler (with context stamping and redaction) to the root logger
    and lowers the root level to ``DEBUG``; :meth:`uninstall` restores both.
    """

    def __init__(self) -> None:
        self._stream = io.StringIO()
        self._handler = make_handler(self._stream, json=True)
        self._saved_level: int | None = None

    def install(self) -> None:
        root = logging.getLogger()
        self._saved_level = root.level
        root.addHandler(self._handler)
        root.setLevel(logging.DEBUG)

    def uninstall(self) -> None:
        root = logging.getLogger()
        root.removeHandler(self._handler)
        if self._saved_level is not None:
            root.setLevel(self._saved_level)

    @property
    def text(self) -> str:
        return self._stream.getvalue()

    @property
    def records(self) -> list[dict[str, Any]]:
        """Each captured line parsed as JSON (a line that is not JSON fails loudly)."""
        return [json.loads(line) for line in self.text.splitlines()]

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)
