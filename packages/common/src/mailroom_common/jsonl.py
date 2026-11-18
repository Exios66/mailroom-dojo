"""The shared JSON Lines IO: append one row, rewrite a file atomically, read it back.

Every row is written as :func:`~mailroom_common.hashing.canonical_json` plus ``\\n``, so a
row's bytes are the bytes its hash is computed over. Lines are split on the byte ``\\n``
only (never on ``str.splitlines`` separators such as U+2028, which canonical JSON leaves
unescaped inside strings).

Reading is strict about JSON (:func:`parse_json`): a duplicated object key or a bare
``NaN``/``Infinity`` is a bad line, because either would make the row hash ambiguous.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal, overload

from mailroom_common.errors import IntegrityError
from mailroom_common.hashing import canonical_json

__all__ = [
    "DEFAULT_MAX_LINE_BYTES",
    "BadLine",
    "append_jsonl",
    "iter_jsonl",
    "parse_json",
    "write_jsonl_atomic",
]

DEFAULT_MAX_LINE_BYTES = 64 * 1024 * 1024

_BOM = b"\xef\xbb\xbf"
_SKIP_CHUNK = 1 << 20
_RAW_PREVIEW_BYTES = 1024


@dataclass(frozen=True)
class BadLine:
    """A line that :func:`iter_jsonl` could not read, reported in tolerant mode.

    ``line_no`` is 1-based. ``raw`` is the line's bytes without its newline (for a line
    over the size limit, only its first ``min(max_line_bytes, 1024)`` bytes). ``truncated_tail`` is true for a final
    line with no newline that does not parse: what a writer killed mid-append leaves.
    """

    line_no: int
    raw: bytes
    error: str
    truncated_tail: bool = False


def _encode_line(obj: Any) -> bytes:
    body = canonical_json(obj)
    if b"\n" in body or b"\r" in body:  # json escapes both; this guards the one-line invariant
        raise IntegrityError("canonical JSON contains a line break")
    return body + b"\n"


def append_jsonl(path: str | Path, obj: Any, *, fsync: bool = False) -> None:
    """Append ``obj`` as one canonical line, creating the file if needed.

    The line is written with a single ``os.write`` on an ``O_APPEND`` descriptor, so
    concurrent appenders (threads or processes on a local filesystem) never interleave
    within a line. With ``fsync=True`` the line is on disk when this returns. A short
    write raises :class:`IntegrityError` (the partial bytes stay; :func:`iter_jsonl`
    reports them as a bad line or truncated tail). ``TypeError``/``ValueError`` from
    canonical encoding (non-string keys, NaN) and ``OSError`` (disk full, fsync error)
    propagate, and nothing is written in the encoding case.
    """
    line = _encode_line(obj)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        written = os.write(fd, line)
        if written != len(line):
            raise IntegrityError(
                "short write while appending a JSONL line",
                path=str(path),
                written=written,
                expected=len(line),
            )
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_jsonl_atomic(path: str | Path, rows: Iterable[Any]) -> int:
    """Replace ``path`` with ``rows``, one canonical line each; return the number of rows.

    Rows are streamed into a temporary file in the same directory, which is fsynced and
    moved over ``path`` with ``os.replace``; the directory is then fsynced so the rename
    survives a crash. If anything fails before the rename (a row that cannot be encoded,
    an exception from ``rows``, a write or fsync error) the temporary file is removed,
    the old file is left exactly as it was, and the error propagates.
    """
    target = Path(path)
    directory = target.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{target.name}.", suffix=".tmp")
    count = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            for row in rows:
                handle.write(_encode_line(row))
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    _fsync_directory(directory)
    return count


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def parse_json(text: str | bytes) -> Any:
    """Decode one JSON document, refusing duplicate object keys and ``NaN``/``Infinity``.

    Raises ``ValueError`` (``json.JSONDecodeError`` is a subclass) with the reason.
    """
    return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_refuse_constant)


def _read_line(handle: IO[bytes], max_line_bytes: int) -> tuple[bytes, bool, bool] | None:
    """Next physical line as ``(content, had_newline, oversized)``; ``None`` at end of file."""
    chunk = handle.readline(max_line_bytes + 1)
    if not chunk:
        return None
    if chunk.endswith(b"\n"):
        return chunk[:-1], True, False
    if len(chunk) <= max_line_bytes:
        return chunk, False, False
    # Over the limit: skip the rest of the line in bounded chunks, keep only a preview.
    had_newline = False
    while rest := handle.readline(_SKIP_CHUNK):
        if rest.endswith(b"\n"):
            had_newline = True
            break
    return chunk[: min(max_line_bytes, _RAW_PREVIEW_BYTES)], had_newline, True


@overload
def iter_jsonl(
    path: str | Path, *, tolerant: Literal[False] = False, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
) -> Iterator[Any]: ...


@overload
def iter_jsonl(
    path: str | Path, *, tolerant: Literal[True], max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
) -> Iterator[Any | BadLine]: ...


@overload
def iter_jsonl(
    path: str | Path, *, tolerant: bool, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
) -> Iterator[Any | BadLine]: ...


def iter_jsonl(
    path: str | Path, *, tolerant: bool = False, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
) -> Iterator[Any | BadLine]:
    """Yield each row of a JSONL file, streaming (memory is bounded by ``max_line_bytes``).

    Accepted: ``\\n`` or ``\\r\\n`` endings, a UTF-8 byte order mark at the start of the
    file, blank or whitespace-only lines (skipped), and a last line without a newline if
    it parses. A bad line is one that is not UTF-8, is not strict JSON (see
    :func:`parse_json`), or is longer than ``max_line_bytes``.

    Strict mode (the default) raises :class:`IntegrityError` at the first bad line, with
    ``path`` and the 1-based ``line`` in its context (plus ``truncated_tail=True`` for an
    unparseable last line without a newline). Tolerant mode yields a :class:`BadLine` in
    the row's place and carries on. ``OSError`` from opening or reading propagates.
    """
    if max_line_bytes < 1:
        raise ValueError(f"max_line_bytes must be at least 1, got {max_line_bytes}")
    with open(path, "rb") as handle:
        line_no = 0
        while (item := _read_line(handle, max_line_bytes)) is not None:
            raw, had_newline, oversized = item
            line_no += 1
            if line_no == 1 and raw.startswith(_BOM):
                raw = raw[len(_BOM) :]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            truncated_tail = False
            if oversized:
                error = f"line is longer than max_line_bytes={max_line_bytes}"
            elif not raw.strip(b" \t\r"):
                continue
            else:
                try:
                    value = parse_json(raw.decode("utf-8"))
                except UnicodeDecodeError as exc:
                    error = f"not valid UTF-8 at byte {exc.start}"
                except ValueError as exc:
                    error = f"not valid JSON: {exc}"
                else:
                    yield value
                    continue
                truncated_tail = not had_newline
            if not tolerant:
                context: dict[str, Any] = {"path": str(path), "line": line_no}
                if truncated_tail:
                    context["truncated_tail"] = True
                raise IntegrityError(f"bad JSONL line: {error}", **context)
            yield BadLine(line_no=line_no, raw=raw, error=error, truncated_tail=truncated_tail)
