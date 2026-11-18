"""The shared JSON Lines IO: append one row, rewrite a file atomically, read it back.

Every row is written as :func:`~mailroom_common.hashing.canonical_json` plus ``\\n``, so a
row's bytes are the bytes its hash is computed over. Lines are split on the byte ``\\n``
only (never on ``str.splitlines`` separators such as U+2028, which canonical JSON leaves
unescaped inside strings).

Reading is strict about JSON (:func:`parse_json`): a duplicated object key or a bare
``NaN``/``Infinity`` is a bad line, because either would make the row hash ambiguous.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import stat
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
    over the size limit, only its first ``min(max_line_bytes, 1024)`` bytes). ``truncated_tail`` is true for a
    final line with no newline that does not parse: what a writer killed mid-append leaves.
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

    The line is written on an ``O_APPEND`` descriptor while holding an exclusive
    ``flock`` on the file, so concurrent callers of this function (threads or processes
    on a local filesystem) never interleave within a line, and each sees the previous
    caller's line complete. In the common case the line goes out in a single
    ``os.write``. With ``fsync=True`` the line is on disk when this returns.

    If the file does not end in ``\\n`` (a writer died mid-append and left a torn line),
    nothing is written and :class:`IntegrityError` is raised with ``path`` in its
    context: appending would glue the new row onto the torn bytes and lose both. The
    file must be repaired first (for example rewritten with :func:`write_jsonl_atomic`
    from the rows :func:`iter_jsonl` can still read).

    A short write (rare: a signal, a full disk) is continued with the remaining bytes
    under the same lock. A write that makes no progress raises ``OSError`` and leaves a
    torn line, which the next append refuses. ``TypeError``/``ValueError`` from
    canonical encoding (non-string keys, NaN) and ``OSError`` (disk full, fsync error)
    propagate, and nothing is written in the encoding case.
    """
    line = _encode_line(obj)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # released by close()
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            raise IntegrityError(
                "JSONL file ends in a torn line and must be repaired before appending", path=str(path)
            )
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(errno.EIO, "write made no progress while appending a JSONL line", str(path))
            view = view[written:]
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

    The new file keeps the permission bits of the file it replaces (``0o644`` when
    ``path`` does not exist yet). If ``path`` is a symbolic link, the file it points to
    is replaced and the link is kept.

    A rewrite needs exclusive access to the file: a row that another process appends
    (:func:`append_jsonl`) while the rewrite runs is lost, because it goes to the file
    being replaced. Stop the writers, or hold a lock of your own, around a rewrite.
    """
    target = Path(os.path.realpath(path))
    directory = target.parent
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{target.name}.", suffix=".tmp")
    count = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            for row in rows:
                handle.write(_encode_line(row))
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
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
    # Read one byte past the limit plus room for a CRLF, so a line exactly at the limit is
    # accepted whether it ends in LF or CRLF (the limit counts content, not the line ending).
    chunk = handle.readline(max_line_bytes + 2)
    if not chunk:
        return None
    if chunk.endswith(b"\n"):
        content = chunk[:-1]
        if len(content.removesuffix(b"\r")) <= max_line_bytes:
            return content, True, False
        # The whole over-long line (newline included) is already consumed.
        return chunk[: min(max_line_bytes, _RAW_PREVIEW_BYTES)], True, True
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
    unparsable last line without a newline). Tolerant mode yields a :class:`BadLine` in
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
