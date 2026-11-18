"""Shared helpers of the importers: source hashing, lenient JSONL reading, and ``--check``.

An importer is a pure function from explicit source files to a snapshot directory. It
never touches the network, never reads a clock, and writes through ``write_snapshot`` /
``build_manifest``, so the same sources always give byte-identical files and the same
``manifest_hash``. Anything it cannot map is not dropped silently: it is counted and listed
under the manifest's ``source`` (``rejected_lines``, ``superseded``, ...), so a reviewer
sees it beside the labels that did import.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from mailroom_labels.errors import LabelsError
from mailroom_labels.models import SnapshotManifest

__all__ = [
    "BUILDER_VERSION",
    "CheckResult",
    "ImportRefused",
    "SourceLine",
    "builder_name",
    "check_snapshot",
    "file_sha256",
    "known_warnings_for",
    "read_jsonl_lenient",
]

BUILDER_VERSION = "0.1.0"


def known_warnings_for(path: Path | str | None, snapshot_id: str) -> list[dict[str, str | None]]:
    """The accepted warnings of one snapshot from a known-warnings YAML file
    (``{<snapshot_id>: [{kind, doc, field, reason}, ...]}``); every entry needs a reason."""
    if path is None:
        return []
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = raw.get(snapshot_id, []) if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ImportRefused(f"{path}: {snapshot_id} must map to a list of warnings")
    out = []
    for entry in entries:
        if not (isinstance(entry, dict) and set(entry) == {"kind", "doc", "field", "reason"}
                and isinstance(entry["kind"], str) and isinstance(entry["reason"], str) and entry["reason"].strip()):
            raise ImportRefused(f"{path}: a known warning must be {{kind, doc, field, reason}} with a reason: {entry!r}")
        out.append({k: entry[k] for k in ("kind", "doc", "field", "reason")})
    return out


def builder_name(command: str) -> str:
    """The manifest ``builder`` string of an importer command."""
    return f"mailroom-labels {command} {BUILDER_VERSION}"


class ImportRefused(LabelsError):
    """A source failed an integrity check (a hash, a listing); nothing was written."""


def file_sha256(path: Path | str) -> str:
    """Lowercase hex sha256 of a file's bytes."""
    digest = sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceLine:
    """One JSON object read from a source JSONL file (``line`` counts from 1)."""

    file: str
    line: int
    data: dict[str, Any]

    @property
    def origin(self) -> str:
        return f"{self.file}:{self.line}"


def read_jsonl_lenient(path: Path, name: str) -> tuple[list[SourceLine], list[dict[str, Any]]]:
    """Every JSON-object line of ``path`` and a ``rejected`` entry for every other non-blank
    line (``{file, line, reason}``). ``name`` is how the file is named in the manifest."""
    rows: list[SourceLine] = []
    rejected: list[dict[str, Any]] = []
    with Path(path).open("rb") as fh:
        for number, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                rejected.append({"file": name, "line": number, "reason": f"not JSON: {exc.__class__.__name__}"})
                continue
            if not isinstance(obj, dict):
                rejected.append({"file": name, "line": number, "reason": f"not a JSON object ({type(obj).__name__})"})
                continue
            rows.append(SourceLine(name, number, obj))
    return rows, rejected


@dataclass
class CheckResult:
    """Whether a committed snapshot is byte-identical to a fresh build of its sources."""

    identical: bool
    differences: list[str] = field(default_factory=list)


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def check_snapshot(committed: Path | str, build: Callable[[Path], SnapshotManifest]) -> CheckResult:
    """Re-derive into a temporary directory with ``build(out_dir)`` and compare every file
    (data files and ``manifest.json``) with ``committed``, byte for byte. A snapshot holds
    nothing else (``load_snapshot`` refuses any unlisted file), so the whole tree is compared."""
    committed = Path(committed)
    tmp = Path(tempfile.mkdtemp(prefix="labels-check-"))
    try:
        fresh_dir = tmp / committed.name
        build(fresh_dir)
        fresh = _tree(fresh_dir)
        old = _tree(committed) if committed.is_dir() else {}
        diffs = []
        for name in sorted(set(fresh) | set(old)):
            if name not in old:
                diffs.append(f"missing from the committed snapshot: {name}")
            elif name not in fresh:
                diffs.append(f"not produced by a fresh build: {name}")
            elif fresh[name] != old[name]:
                diffs.append(f"differs from a fresh build: {name}")
        return CheckResult(identical=not diffs, differences=diffs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
