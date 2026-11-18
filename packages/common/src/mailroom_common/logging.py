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
(``api_key``, ``token``, ``secret``, ``password``, ``authorization``, ``cookie``,
``credential(s)``, ``private_key``, ``passphrase``, matched as a whole word segment in any
case, so ``access_token`` and ``apiKey`` match but ``input_tokens`` does not; a key ending
in ``count``, ``budget``, ``limit``, ``size``, ``total`` or ``ratio`` is a number and stays)
is replaced by ``[REDACTED]``, at any depth of nested mappings and lists, and so is the
second item of a two-item pair whose first item names a credential (header tuples). Text
is masked by :func:`redact_text`. Redaction is idempotent, and the filter never raises
into the logging call.
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

_SENSITIVE_WORDS = (
    r"api[_-]?key|apikey|token|secret|password|passwd|authorization|cookie"
    r"|credentials?|private[_-]?key|passphrase"
)
# A key is sensitive when one of its _-./ separated segments is a sensitive word,
# optionally followed by digits (``password2``, ``token_v2`` matches through ``token``).
_SENSITIVE_KEY = re.compile(rf"(?:^|[_\-./ ])(?:{_SENSITIVE_WORDS})[0-9]*(?:$|[_\-./ ])")
# Words that name a credential on their own, so a segment that merely ENDS in one is still
# sensitive (``dbpassword``, ``clientsecret``): failing closed beats leaking a run-together key.
_SENSITIVE_SUFFIX = re.compile(
    r"(?:^|[_\-./ ])[a-z0-9]*(?:password|passwd|passphrase|secret)[0-9]*(?:$|[_\-./ ])"
)
_KEY_SEPARATORS = re.compile(r"[_\-./ ]")
# A key ending in one of these names a number about a credential, not the credential.
_NUMERIC_SUFFIXES = frozenset({"count", "budget", "limit", "size", "total", "ratio"})
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Every text pattern runs in linear time: a pattern starts only at the beginning of a run
# of its characters (a lookbehind), and runs and separators use possessive quantifiers, so
# a failed attempt never rescans the run it started in.
_HEADER_VALUE = re.compile(
    r"(?i)(?<![\w-])((?:proxy-)?authorization|(?:set-)?cookie)(['\"]?\s*+[=:]\s*+)(['\"]?)"
    r"(?!\[REDACTED\])[^\s'\"][^\r\n'\"]*+"
)
_MAX_PASSES = 8
_URL_AUTHORITY = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*+://)([^\s/?#]*+)")
_KEY_SEPARATOR = re.compile(r"(?<![\w.-])([\w.-]++)(['\"]?\s*+[=:]\s*+)(['\"]?)")
_VALUE = re.compile(r"(?!\[REDACTED\]|(?i:bearer)\s)[^\s'\",;&]++")
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(?<![\w-])bearer\s++(?!\[REDACTED\])[A-Za-z0-9._~+/=-]++"), f"Bearer {REDACTED}"),
    (re.compile(r"(?<![\w-])sk-[A-Za-z0-9_-]{8,}+"), REDACTED),
    (re.compile(r"(?<![\w-])key-(?=[A-Za-z]*+[0-9])[A-Za-z0-9]{16,}+(?![_-])"), REDACTED),
    (
        re.compile(r"(?<![\w-])(?:hf_|gh[pousr]_)[A-Za-z0-9]{20,}+|(?<![\w-])github_pat_[A-Za-z0-9_]{20,}+"),
        REDACTED,
    ),
)


def _is_sensitive_key(key: str) -> bool:
    normalized = _CAMEL_BOUNDARY.sub("_", key).lower()
    if _KEY_SEPARATORS.split(normalized)[-1] in _NUMERIC_SUFFIXES:
        return False
    return _SENSITIVE_KEY.search(normalized) is not None or _SENSITIVE_SUFFIX.search(normalized) is not None


def _mask_userinfo(match: re.Match[str]) -> str:
    """Mask everything before the last ``@`` of a URL authority when it holds a password."""
    authority = match.group(2)
    at = authority.rfind("@")
    if at < 0 or ":" not in authority[:at]:
        return match.group(0)
    return f"{match.group(1)}{REDACTED}{authority[at:]}"


def _mask_key_values(text: str) -> str:
    """Mask the value after every ``key=``/``key:`` whose key is sensitive.

    A non-sensitive key's value is not consumed, so a pair inside it (``KeyError:
    'token=…'``) is still found; each step moves forward, so the scan stays linear.
    """
    parts: list[str] = []
    pos = 0
    while (match := _KEY_SEPARATOR.search(text, pos)) is not None:
        parts.append(text[pos : match.end()])
        pos = match.end()
        if _is_sensitive_key(match.group(1)) and (value := _VALUE.match(text, pos)) is not None:
            parts.append(REDACTED)
            pos = value.end()
    parts.append(text[pos:])
    return "".join(parts)


def redact_text(text: str) -> str:
    """Mask credentials in ``text``.

    Masked: an ``Authorization``/``Proxy-Authorization``/``Cookie``/``Set-Cookie`` value
    (whole, to the end of the line or quote), ``Bearer <token>``, ``sk-…``, Mailgun-style
    ``key-…`` (16+ letters and digits, at least one digit), ``hf_``/``ghp_``-style and
    ``github_pat_`` tokens, URL userinfo (everything before the last ``@`` of the
    authority, when it holds a ``:``) and the value of any ``key=value``/``key: value``
    whose key is sensitive by the same segment rule as structured keys. Linear in the
    length of ``text``.
    """
    # One mask can join text into a shape another pattern masks (a value that held the
    # ``//`` ending a URL authority), so passes repeat until nothing changes: that makes
    # redaction idempotent. Each pass only masks, so this settles within a few passes.
    for _ in range(_MAX_PASSES):
        before = text
        text = _HEADER_VALUE.sub(rf"\1\2\3{REDACTED}", text)
        for pattern, replacement in _TEXT_PATTERNS:
            text = pattern.sub(replacement, text)
        text = _URL_AUTHORITY.sub(_mask_userinfo, text)
        text = _mask_key_values(text)
        if text == before:
            break
    return text


def redact(value: Any, key: str | None = None) -> Any:
    """Return a JSON-friendly copy of ``value`` with credentials masked.

    Mappings and sequences are copied recursively; strings are scanned with
    :func:`redact_text`; numbers, booleans and ``None`` pass through; any other object is
    rendered with ``str`` and then scanned. ``key`` is the name the value is stored under.
    A two-item list or tuple whose first item is a string naming a credential (a header
    pair such as ``("Authorization", "Basic …")``) is treated as a key and its value.
    """
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], str):
        return [redact_text(value[0]), redact(value[1], value[0])]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value if isinstance(value, str) else _safe_str(value))


def _safe_str(value: object) -> str:
    """``str(value)``, or a placeholder naming the type if ``__str__`` raises."""
    try:
        return str(value)
    except Exception:
        return f"<unprintable {type(value).__name__}>"


def _render_message(record: logging.LogRecord) -> str:
    """The record's message; if ``msg % args`` fails, the format and the argument types.

    Argument values are left out on purpose: apart from the format, a bare secret has no
    ``token=`` beside it for redaction to recognise.
    """
    try:
        return record.getMessage()
    except Exception as exc:
        args = record.args if isinstance(record.args, tuple) else (record.args,)
        types = ", ".join(type(arg).__name__ for arg in args)
        return f"{_safe_str(record.msg)} [unformattable: {type(exc).__name__}; argument types: {types}]"


def current_context() -> dict[str, Any]:
    """The fields bound in the calling thread or task, as a new dict."""
    return dict(_CONTEXT.get())


@contextmanager
def bind_context(**fields: Any) -> Iterator[None]:
    """Add ``fields`` to every record logged inside the block; inner bindings win.

    asyncio tasks copy the context when they are created, so they inherit the bindings.
    A plain ``threading.Thread`` or a thread-pool worker starts with an empty context; to
    carry the bindings into one, submit ``contextvars.copy_context().run`` with the
    function, for example ``executor.submit(contextvars.copy_context().run, fn, arg)``.
    """
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

    It never raises into the logging call: a message whose ``%`` arguments do not fit is
    rendered as its format plus the (redacted) arguments, and an object whose ``__str__``
    raises is shown as ``<unprintable TypeName>``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(_render_message(record))
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
        "event": _render_message(record),
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
