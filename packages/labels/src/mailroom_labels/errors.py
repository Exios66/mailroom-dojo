"""Typed errors raised by the labels package.

Every failure a snapshot can meet has its own exception (or its own ``kind`` on
``ManifestMismatch``), so a test, a CLI and the quality gate can say exactly what went
wrong instead of "could not load". Data problems inside well-formed rows are not
exceptions: they are ``Finding`` records (see ``quality.py``), because one bad label must
never hide its valid neighbours.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "MISMATCH_KINDS",
    "CorruptLine",
    "DuplicateSnapshot",
    "LabelsError",
    "ManifestMismatch",
    "ManifestMissing",
    "ProfileError",
    "UnknownSnapshotSchema",
]

#: Every ``ManifestMismatch.kind``. Each has a tamper test.
MISMATCH_KINDS: tuple[str, ...] = (
    "manifest_invalid",  # manifest.json is not JSON or not the manifest shape
    "manifest_hash",  # manifest_hash does not equal the hash of the rest of the manifest
    "unsafe_path",  # a listed path is absolute, escapes the directory, or is not a .jsonl file
    "file_order",  # the files list is not sorted by path, or lists a path twice
    "missing",  # a listed file is not on disk
    "extra",  # a file on disk is not listed
    "truncated",  # a file is shorter than the manifest says
    "grown",  # a file is longer than the manifest says
    "changed",  # a file has the listed size but a different sha256
    "row_count",  # a file holds a different number of rows than listed
)


class LabelsError(Exception):
    """Base class of every error this package raises on purpose."""


class ManifestMissing(LabelsError):
    """The snapshot directory has no ``manifest.json``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        super().__init__(f"no manifest.json in {self.path}")


class ManifestMismatch(LabelsError):
    """The snapshot on disk does not match its manifest (``kind`` says how)."""

    def __init__(self, kind: str, path: Path, detail: str = "") -> None:
        if kind not in MISMATCH_KINDS:
            raise ValueError(f"unknown mismatch kind {kind!r}")
        self.kind = kind
        self.path = Path(path)
        self.detail = detail
        super().__init__(f"{kind}: {self.path}" + (f" ({detail})" if detail else ""))


class UnknownSnapshotSchema(LabelsError):
    """A manifest or layer schema this code does not know (newer, or misspelled)."""

    def __init__(self, schema: object, path: Path) -> None:
        self.schema = schema
        self.path = Path(path)
        super().__init__(f"unknown snapshot schema {schema!r} in {self.path}")


class CorruptLine(LabelsError):
    """A JSONL line is not one JSON object (``line`` counts from 1)."""

    def __init__(self, path: Path, line: int, detail: str) -> None:
        self.path = Path(path)
        self.line = line
        self.detail = detail
        super().__init__(f"{self.path}:{line}: {detail}")


class DuplicateSnapshot(LabelsError):
    """Two snapshots in one set share a ``snapshot_id``."""

    def __init__(self, snapshot_id: str) -> None:
        self.snapshot_id = snapshot_id
        super().__init__(f"snapshot {snapshot_id!r} given twice")


class ProfileError(LabelsError):
    """A client profile is unreadable or invalid; ``problems`` lists every problem found."""

    def __init__(self, path: Path | None, problems: list[str]) -> None:
        self.path = Path(path) if path is not None else None
        self.problems = list(problems)
        where = f"{self.path}: " if self.path else ""
        super().__init__(where + "; ".join(self.problems))
