"""Reading a sandbox run folder: JSON and JSONL files, timestamps, and our own tokens.

Shared by the importer and the audit. Nothing here writes, and no error message quotes a
stored value: messages name the file, the line number and what is wrong.

- ``read_jsonl`` reads every line of a ``.jsonl`` file. A blank line is skipped. A last
  line with no newline is a torn tail (the writer died mid-line) when it does not parse:
  it is skipped and reported in the warnings. One that parses is kept, as the sandbox's
  own readers keep it. Any other line that is not a JSON object raises
  ``SandboxFolderInvalid``: corruption inside a file is never skipped silently.
- ``epoch`` reads a timestamp the way the sandbox's floor replay does (epoch seconds,
  epoch milliseconds above 1e11, or ISO-8601; a naive ISO time is UTC); ``iso`` writes
  the record's form (UTC, microseconds, ``Z``).
- ``scrub`` keeps a value made of numbers, booleans, ``None`` and short tokens (ids, names,
  labels such as ``low_confidence``); any other string becomes its SHA-256 and length, so
  free text from a model or a document never reaches the record. A token is at most 80
  characters of ``A-Z a-z 0-9 _ . : / + # = -``, and never a path or a URL (a leading ``/``,
  a drive letter, a ``.`` or ``..`` segment, or ``//``); ``@`` is not among its characters,
  so an email is not a token. A single word is still a token, whatever wrote it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "SandboxFolderInvalid",
    "cause_token",
    "epoch",
    "iso",
    "label",
    "read_json",
    "read_jsonl",
    "scrub",
    "shown",
    "token",
]

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:/+#=-]{0,80}")
# A path or a URL is not a token, though its characters are: an absolute or home path, a
# drive letter, a ``.`` or ``..`` segment, an empty segment (``//``, so every ``scheme://``).
# A single ``/`` stays (model ids such as ``qwen/qwen3.7-flash``, schema ids such as
# ``mailroom.spend-authorization/v1``), and so do several (corpus ids such as
# ``allen-p/deleted_items/100.``). ``@`` is never in a token, so neither is an email.
_PATH_RE = re.compile(r"^/|^[A-Za-z]:/|//|(^|/)\.{1,2}(/|$)")
_CAUSE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,79}")


class SandboxFolderInvalid(ValueError):
    """The folder cannot be read as a sandbox run. The message names files and lines only."""


def token(value: Any) -> str | None:
    """``value`` if it is a short token string (safe to store and print), else ``None``."""
    if isinstance(value, str) and _TOKEN_RE.fullmatch(value) and not _PATH_RE.search(value):
        return value
    return None


def label(value: Any) -> Any:
    """A class label, split or revision name as given (at most 200 characters), else scrubbed."""
    if isinstance(value, str) and len(value) <= 200 and value.isprintable():
        return value
    return scrub(value)


def shown(value: Any) -> str:
    """An id for an error message: the id itself when it is a token, else a digest of it."""
    t = token(value)
    if t is not None:
        return t
    return f"<{hashlib.sha256(str(value).encode('utf-8', 'replace')).hexdigest()[:12]}>"


def scrub(value: Any) -> Any:
    """``value`` with every non-token string replaced by ``{"sha256", "chars"}`` (keys too)."""
    if isinstance(value, str):
        if token(value) is not None:
            return value
        return {"sha256": hashlib.sha256(value.encode("utf-8", "replace")).hexdigest(), "chars": len(value)}
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = token(k) or f"key-{hashlib.sha256(str(k).encode('utf-8', 'replace')).hexdigest()[:12]}"
            out[key] = scrub(v)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return None


def cause_token(error: Any) -> str | None:
    """The error's type name (the text before the first ``:``), never its message text."""
    if error is None or error is False:
        return None
    if isinstance(error, str):
        head = error.split(":", 1)[0].strip()
        if _CAUSE_RE.fullmatch(head):
            return head
    return "error"


def epoch(value: Any) -> float | None:
    """Seconds since the epoch, or ``None`` when ``value`` is not a usable timestamp."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return number / 1000.0 if number > 100_000_000_000 else number
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return epoch(float(text))
    except ValueError:
        pass
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def iso(seconds: float) -> str | None:
    """The record's timestamp form of ``seconds``, or ``None`` when it is out of range."""
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (OverflowError, OSError, ValueError):
        return None


def read_json(path: Path) -> dict[str, Any] | None:
    """A JSON object file, ``None`` when absent. Anything else raises ``SandboxFolderInvalid``."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise SandboxFolderInvalid(f"{path.name}: not valid JSON") from None
    if not isinstance(data, dict):
        raise SandboxFolderInvalid(f"{path.name}: not a JSON object")
    return data


def read_jsonl(
    path: Path, warnings: list[str], torn: list[dict[str, Any]] | None = None
) -> list[tuple[int, dict[str, Any]]] | None:
    """``(line number, row)`` for every row of a JSONL file, ``None`` when it is absent. A
    skipped torn last line is a warning and, when ``torn`` is given, ``{"file", "bytes"}`` there."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    lines = raw.split(b"\n")
    tail = lines.pop()  # b"" when the file ends in a newline
    rows: list[tuple[int, dict[str, Any]]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        row = _parse(line)
        if row is None:
            raise SandboxFolderInvalid(f"{path.name} line {number}: not a JSON object")
        rows.append((number, row))
    if tail.strip():
        row = _parse(tail)
        if row is None:
            warnings.append(f"{path.name}: skipped a torn last line ({len(tail)} bytes, no newline)")
            if torn is not None:
                torn.append({"file": path.name, "bytes": len(tail)})
        else:
            rows.append((len(lines) + 1, row))
    return rows


def _parse(line: bytes) -> dict[str, Any] | None:
    try:
        row = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return row if isinstance(row, dict) else None
