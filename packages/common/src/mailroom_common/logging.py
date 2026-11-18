"""Structured, redacted logging on top of the standard library.

One call, :func:`configure_logging`, installs a single handler on the root logger that
writes one line per record. In JSON mode a line is an object with ``ts`` (ISO-8601 UTC),
``level``, ``logger``, ``event`` (the rendered message), every field bound with
:func:`bind_context`, every ``extra=`` field, and ``exc``/``stack`` when present. Calling
it again replaces that handler instead of adding a second one.

Context is held in a :class:`contextvars.ContextVar`, so a binding is visible to the code
inside its ``with`` block only: it nests, is restored when the block exits (also on an
exception), and does not leak between threads or between asyncio tasks.

Redaction runs on everything a handler built here writes: the message, the exception and
stack text, bound context and ``extra`` fields. A value whose key names a credential
(``api_key``, ``token``, ``secret``, ``password``, ``authorization``, ``cookie``, matched
as a whole word segment in any case, so ``access_token`` and ``apiKey`` match but
``input_tokens`` does not) is replaced by ``[REDACTED]``, at any depth of nested mappings
and lists. Inside text, ``Bearer <token>``, ``sk-…``/``key-…`` style secrets and
``password=…``-style pairs are masked. Redaction is idempotent.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, TextIO

from mailroom_common.errors import ConfigError

__all__ = [
    "REDACTED",
    "ContextFilter",
    "JsonFormatter",
    "RedactionFilter",
    "bind_context",
    "configure_logging",
    "current_context",
    "get_logger",
    "make_handler",
    "redact",
    "redact_text",
]

REDACTED = "[REDACTED]"

_CONTEXT: ContextVar[Mapping[str, Any]] = ContextVar("mailroom_log_context", default=MappingProxyType({}))
_CONTEXT_ATTR = "mailroom_context"
_OWNED_ATTR = "_mailroom_owned"
_CONFIGURE_LOCK = threading.Lock()

# Attributes every LogRecord has; anything else on a record came from ``extra=``.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    _CONTEXT_ATTR,
}
_BASE_KEYS = ("ts", "level", "logger", "event")

_SENSITIVE_WORDS = r"api[_-]?key|apikey|token|secret|password|passwd|authorization|cookie"
# A key is sensitive when one of its _-./ separated segments is a sensitive word.
_SENSITIVE_KEY = re.compile(rf"(?:^|[_\-./ ])(?:{_SENSITIVE_WORDS})(?:$|[_\-./ ])")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {REDACTED}"),
    (re.compile(r"\b(?:sk|key)-[A-Za-z0-9_-]{8,}"), REDACTED),
    (
        re.compile(
            rf"(?i)\b([\w.-]*?(?:{_SENSITIVE_WORDS}))\b(['\"]?\s*[=:]\s*)(['\"]?)(?!\[REDACTED\]|bearer\s)[^\s'\",;&]+"
        ),
        rf"\1\2\3{REDACTED}",
    ),
)


def _is_sensitive_key(key: str) -> bool:
    return _SENSITIVE_KEY.search(_CAMEL_BOUNDARY.sub("_", key).lower()) is not None


def redact_text(text: str) -> str:
    """Mask bearer tokens, ``sk-``/``key-`` style secrets and ``secret=value`` pairs in ``text``."""
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any, key: str | None = None) -> Any:
    """Return a JSON-friendly copy of ``value`` with credentials masked.

    Mappings and sequences are copied recursively; strings are scanned with
    :func:`redact_text`; numbers, booleans and ``None`` pass through; any other object is
    rendered with ``str`` and then scanned. ``key`` is the name the value is stored under.
    """
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value if isinstance(value, str) else str(value))


def current_context() -> dict[str, Any]:
    """The fields bound in the calling thread or task, as a new dict."""
    return dict(_CONTEXT.get())


@contextmanager
def bind_context(**fields: Any) -> Iterator[None]:
    """Add ``fields`` to every record logged inside the block; inner bindings win."""
    token = _CONTEXT.set(MappingProxyType({**_CONTEXT.get(), **fields}))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def get_logger(name: str) -> logging.Logger:
    """A standard logger; pass structured fields with ``extra={...}``."""
    return logging.getLogger(name)


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {key: value for key, value in record.__dict__.items() if key not in _STANDARD_ATTRS}


class ContextFilter(logging.Filter):
    """Stamps the bound context onto each record at the moment it is handled."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, _CONTEXT_ATTR):
            setattr(record, _CONTEXT_ATTR, current_context())
        return True


class RedactionFilter(logging.Filter):
    """Masks credentials in a record in place, so every later formatter sees the masked form.

    The message is rendered and its arguments dropped; exception and stack text are
    rendered and masked; context and ``extra`` fields are replaced by redacted copies.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        for key, value in _extras(record).items():
            setattr(record, key, redact(value, key))
        context = getattr(record, _CONTEXT_ATTR, None)
        if context is not None:
            setattr(record, _CONTEXT_ATTR, redact(context))
        return True


def _payload(record: logging.LogRecord) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
        "level": record.levelname,
        "logger": record.name,
        "event": record.getMessage(),
    }
    fields = {**getattr(record, _CONTEXT_ATTR, {}), **_extras(record)}
    # The base keys always win over a field of the same name.
    payload = {**base, **{k: v for k, v in fields.items() if k not in _BASE_KEYS}}
    if record.exc_info and not record.exc_text:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    if record.exc_text:
        payload["exc"] = record.exc_text
    if record.stack_info:
        payload["stack"] = record.stack_info
    redacted: dict[str, Any] = redact(payload)
    return redacted


class JsonFormatter(logging.Formatter):
    """One JSON object per record, on one line. Redacts again on its own (idempotent)."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_payload(record), default=str)


class _TextFormatter(logging.Formatter):
    """``ts LEVEL logger: event key=value ...`` with the exception on the following lines."""

    def format(self, record: logging.LogRecord) -> str:
        payload = _payload(record)
        head = f"{payload.pop('ts')} {payload.pop('level')} {payload.pop('logger')}: {payload.pop('event')}"
        tail = [payload.pop(key) for key in ("exc", "stack") if key in payload]
        fields = " ".join(f"{key}={json.dumps(value, default=str)}" for key, value in payload.items())
        return "\n".join([f"{head} {fields}".rstrip(), *tail])


def make_handler(stream: TextIO | io.StringIO, *, json: bool = True) -> logging.Handler:
    """A stream handler with context stamping, redaction and the chosen formatter."""
    handler = logging.StreamHandler(stream)
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactionFilter())
    handler.setFormatter(JsonFormatter() if json else _TextFormatter())
    return handler


def _level_number(level: str | int) -> int:
    if isinstance(level, int):
        return level
    mapping = logging.getLevelNamesMapping()
    if level.upper() not in mapping:
        raise ConfigError("unknown log level", level=level, known=sorted(mapping))
    return mapping[level.upper()]


def configure_logging(level: str | int = "INFO", json: bool = True, stream: TextIO | None = None) -> None:
    """Install (or replace) this workspace's one handler on the root logger.

    Idempotent: a second call swaps the handler, so records are never written twice.
    Handlers installed by others are left alone. Raises :class:`ConfigError` for an
    unknown level name. ``stream`` defaults to ``sys.stderr``.
    """
    number = _level_number(level)
    handler = make_handler(stream if stream is not None else sys.stderr, json=json)
    setattr(handler, _OWNED_ATTR, True)
    root = logging.getLogger()
    with _CONFIGURE_LOCK:
        for old in [h for h in root.handlers if getattr(h, _OWNED_ATTR, False)]:
            root.removeHandler(old)
        root.addHandler(handler)
        root.setLevel(number)
