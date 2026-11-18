"""Label snapshots: a directory of JSONL files plus a ``manifest.json`` that pins them.

Building (``build_manifest``, ``write_snapshot``) is deterministic: rows are written as
canonical JSON (``mailroom_record.hashing``), files are listed sorted by path, and the
manifest holds no timestamp of its own, so the same input gives byte-identical files and
the same ``manifest_hash``. ``manifest_hash`` is ``hash_obj`` of the manifest without that
key, so any edit to the manifest (a file hash, a row count, the source) changes it.

Loading (``load_snapshot``) verifies the whole manifest **before** a single row is handed
out: the manifest's own hash, safe and sorted paths, every listed file present with the
listed size, sha256 and row count, and no unlisted file. Each failure raises
``ManifestMismatch`` with its own ``kind``. Rows are then streamed line by line (``rows()``); a line
that is not one JSON object raises ``CorruptLine`` with the file and line number, and a
file that changed after verification raises ``ManifestMismatch("changed")`` when its
stream opens or ends.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from mailroom_labels.errors import (
    CorruptLine,
    DuplicateSnapshot,
    LabelsError,
    ManifestMismatch,
    ManifestMissing,
    UnknownSnapshotSchema,
)
from mailroom_labels.models import SNAPSHOT_ID_PATTERN, SnapshotManifest
from mailroom_record.hashing import canonical_json, hash_obj

__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "LabelSnapshot",
    "Row",
    "build_manifest",
    "label_snapshot_ids",
    "load_snapshot",
    "load_snapshot_set",
    "manifest_hash_of",
    "snapshot_refs",
    "write_jsonl",
    "write_snapshot",
]

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "mailroom-labels/manifest/v1"
_KNOWN_MANIFEST_SCHEMAS = (MANIFEST_SCHEMA,)


@dataclass(frozen=True)
class Row:
    """One parsed JSONL row and where it came from (``line`` counts from 1)."""

    file: str
    line: int
    data: dict[str, Any]


class _BadJson(ValueError):
    pass


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _BadJson(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constant(name: str) -> Any:
    raise _BadJson(f"non-finite number {name}")


def _parse_line(raw: bytes, path: Path, line_no: int) -> dict[str, Any]:
    """One JSONL line as a dict. Refuses (``CorruptLine``) a missing newline, a blank line,
    invalid UTF-8 or JSON, a repeated key, ``NaN`` / ``Infinity``, nesting too deep to
    parse, and anything but an object."""
    if not raw.endswith(b"\n"):
        raise CorruptLine(path, line_no, "last line has no newline (truncated write?)")
    body = raw[:-1]
    if not body.strip():
        raise CorruptLine(path, line_no, "blank line")
    try:
        obj = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, _BadJson) as exc:
        raise CorruptLine(path, line_no, f"not JSON: {exc}") from exc
    except RecursionError as exc:
        raise CorruptLine(path, line_no, "JSON nested too deeply") from exc
    if not isinstance(obj, dict):
        raise CorruptLine(path, line_no, f"a row must be a JSON object, got {type(obj).__name__}")
    return obj


def _file_facts(path: Path, *, parse: bool) -> tuple[str, int, int]:
    """(sha256, rows, bytes) of a JSONL file; with ``parse`` every row must be a JSON object."""
    digest = sha256()
    rows = 0
    size = 0
    with path.open("rb") as fh:
        for raw in fh:
            digest.update(raw)
            size += len(raw)
            rows += 1
            if parse:
                _parse_line(raw, path, rows)
    return digest.hexdigest(), rows, size


def _jsonl_lines(data: bytes) -> list[bytes]:
    """The lines of a JSONL file as reading it in binary mode gives them (split on ``\\n``
    only, each keeping its newline; a last line without one is kept as it is)."""
    parts = data.split(b"\n")
    return [part + b"\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def manifest_hash_of(manifest: Mapping[str, Any]) -> str:
    """``hash_obj`` of a manifest mapping with its ``manifest_hash`` key left out."""
    return hash_obj({k: v for k, v in manifest.items() if k != "manifest_hash"})


def _data_files(directory: Path) -> list[str]:
    """Every file under ``directory`` except the manifest, as sorted POSIX relative paths."""
    out = []
    for path in directory.rglob("*"):
        if path.is_file() or path.is_symlink():
            rel = path.relative_to(directory).as_posix()
            if rel != MANIFEST_NAME:
                out.append(rel)
    return sorted(out)


def _safe_path(rel: str) -> bool:
    pure = PurePosixPath(rel)
    return (
        rel != ""
        and not pure.is_absolute()
        and ".." not in pure.parts
        and "\\" not in rel
        and pure.suffix == ".jsonl"
        and rel != MANIFEST_NAME
    )


def _check_snapshot_id(snapshot_id: Any) -> None:
    if not (isinstance(snapshot_id, str) and re.fullmatch(SNAPSHOT_ID_PATTERN, snapshot_id)):
        raise ValueError(f"snapshot id {snapshot_id!r} must match {SNAPSHOT_ID_PATTERN}")


def build_manifest(
    directory: Path | str,
    *,
    snapshot_id: str,
    kind: str,
    layer_schema: str,
    source: Mapping[str, Any],
    builder: str,
    known_warnings: Sequence[Mapping[str, str | None]] = (),
) -> SnapshotManifest:
    """Hash every data file in ``directory`` and write ``manifest.json``; return the manifest.

    Every data file must be a ``.jsonl`` file whose rows are JSON objects (``CorruptLine``
    otherwise), so a snapshot can never be committed with a line it cannot load.
    """
    directory = Path(directory)
    _check_snapshot_id(snapshot_id)
    files = []
    for rel in _data_files(directory):
        if not _safe_path(rel):
            raise ManifestMismatch("unsafe_path", directory / rel, "data files must be .jsonl")
        digest, rows, size = _file_facts(directory / rel, parse=True)
        files.append({"path": rel, "sha256": digest, "rows": rows, "bytes": size})
    warnings = sorted(
        (dict(w) for w in known_warnings),
        key=lambda w: canonical_json(w),
    )
    manifest: dict[str, Any] = {
        "manifest_schema": MANIFEST_SCHEMA,
        "snapshot_id": snapshot_id,
        "kind": kind,
        "layer_schema": layer_schema,
        "source": dict(source),
        "builder": builder,
        "files": files,
        "known_warnings": warnings,
    }
    manifest["manifest_hash"] = manifest_hash_of(manifest)
    model = SnapshotManifest.model_validate_json(json.dumps(manifest))
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    (directory / MANIFEST_NAME).write_text(text, encoding="utf-8")
    return model


def write_jsonl(path: Path | str, rows: Iterable[Mapping[str, Any]]) -> None:
    """Write ``rows`` as canonical JSON lines, in the order given."""
    with Path(path).open("wb") as fh:
        for row in rows:
            fh.write(canonical_json(row) + b"\n")


def _check_replaceable(directory: Path, snapshot_id: str) -> None:
    """Refuse (``FileExistsError``, touching nothing) a ``directory`` that is not safe to
    replace. It may be missing or empty, or hold exactly an earlier build of the same
    snapshot id: a valid manifest, the files it lists and the directories holding them,
    nothing else."""
    if not directory.exists() or not any(directory.iterdir()):
        return
    try:
        old = _read_manifest(directory)
    except LabelsError as exc:
        raise FileExistsError(
            f"{directory} is not empty and holds no valid snapshot manifest ({exc})"
        ) from exc
    if old.snapshot_id != snapshot_id:
        raise FileExistsError(f"{directory} holds snapshot {old.snapshot_id!r}, not {snapshot_id!r}")
    listed = {f.path for f in old.files}
    if not all(_safe_path(rel) for rel in listed):
        raise FileExistsError(f"{directory}: its manifest lists an unsafe path")
    unknown = sorted(
        rel
        for p in directory.rglob("*")
        if (rel := p.relative_to(directory).as_posix()) not in listed | {MANIFEST_NAME}
        and not (p.is_dir() and not p.is_symlink())
    )
    if unknown:
        raise FileExistsError(
            f"{directory} holds {len(unknown)} file(s) its manifest does not list: {unknown[0]}"
        )


def write_snapshot(
    directory: Path | str,
    files: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    sort_key: Callable[[Mapping[str, Any]], Any] | None = None,
    **manifest_args: Any,
) -> SnapshotManifest:
    """Write each file's rows (sorted by ``sort_key``, default their canonical JSON) and the
    manifest. Input order never changes the bytes written. ``directory`` must be missing,
    empty, or an earlier build of the same snapshot id (``FileExistsError`` otherwise,
    nothing touched), which is replaced whole, so no file of an older build is ever listed
    as current.

    The new build is written into a temporary directory beside ``directory`` and swapped in
    only once every row is serialised and the manifest validates. A failure before that
    leaves the earlier build as it was and removes the temporary directory."""
    directory = Path(directory)
    snapshot_id = manifest_args.get("snapshot_id")
    _check_snapshot_id(snapshot_id)
    _check_replaceable(directory, snapshot_id)
    directory.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{directory.name}.build-", dir=directory.parent))
    try:
        build = scratch / "new"
        build.mkdir()  # a plain mkdir, so the snapshot gets the usual permissions
        key = sort_key or canonical_json
        for rel, rows in files.items():
            target = build / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            write_jsonl(target, sorted(rows, key=key))
        model = build_manifest(build, **manifest_args)
        _swap_in(build, directory, scratch / "old")
        return model
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _swap_in(build: Path, directory: Path, aside: Path) -> None:
    """Move ``build`` to ``directory``; an existing ``directory`` is moved to ``aside`` first
    (and moved back if the second rename fails). Both renames stay in one parent directory."""
    if not directory.exists() and not directory.is_symlink():
        build.rename(directory)
        return
    directory.rename(aside)
    try:
        build.rename(directory)
    except BaseException:
        aside.rename(directory)
        raise


@dataclass(frozen=True)
class LabelSnapshot:
    """A verified snapshot. Construct it with ``load_snapshot``, never directly."""

    directory: Path
    manifest: SnapshotManifest

    @property
    def snapshot_id(self) -> str:
        return self.manifest.snapshot_id

    @property
    def manifest_hash(self) -> str:
        return self.manifest.manifest_hash

    @property
    def kind(self) -> str:
        return self.manifest.kind

    def rows(self, on_corrupt: Callable[[CorruptLine], None] | None = None) -> Iterator[Row]:
        """Stream every row of every file, in manifest order, re-hashing as it reads.

        A corrupt line raises ``CorruptLine``; with ``on_corrupt`` it is passed there instead
        and reading goes on, so the quality gate can report it beside its valid neighbours.
        """
        for entry in self.manifest.files:
            path = self.directory / entry.path
            # Read once, verify those bytes, then parse the same bytes: what is yielded is
            # exactly what was hashed, so a change after load_snapshot (or during the read)
            # is caught before any row of the file is yielded. Files are a few MB at most.
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ManifestMismatch("missing", path, f"cannot read: {exc}") from exc
            lines = _jsonl_lines(data)
            if sha256(data).hexdigest() != entry.sha256 or len(lines) != entry.rows:
                raise ManifestMismatch("changed", path, "file changed after it was verified")
            for line_no, raw in enumerate(lines, start=1):
                try:
                    row = _parse_line(raw, path, line_no)
                except CorruptLine as exc:
                    if on_corrupt is None:
                        raise
                    on_corrupt(exc)
                    continue
                yield Row(entry.path, line_no, row)


def _read_manifest(directory: Path) -> SnapshotManifest:
    path = directory / MANIFEST_NAME
    if not path.is_file():
        raise ManifestMissing(directory)
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys, parse_constant=_no_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _BadJson, RecursionError) as exc:
        raise ManifestMismatch("manifest_invalid", path, f"not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestMismatch("manifest_invalid", path, "not a JSON object")
    if raw.get("manifest_schema") not in _KNOWN_MANIFEST_SCHEMAS:
        raise UnknownSnapshotSchema(raw.get("manifest_schema"), path)
    try:
        model = SnapshotManifest.model_validate_json(json.dumps(raw))
    except ValidationError as exc:
        raise ManifestMismatch("manifest_invalid", path, str(exc).splitlines()[0]) from exc
    if manifest_hash_of(raw) != raw["manifest_hash"]:
        raise ManifestMismatch("manifest_hash", path, "manifest edited after it was built")
    return model


def load_snapshot(directory: Path | str) -> LabelSnapshot:
    """Verify ``directory`` against its manifest and return the snapshot (see module doc)."""
    directory = Path(directory)
    manifest = _read_manifest(directory)
    listed = [f.path for f in manifest.files]
    for rel in listed:
        if not _safe_path(rel):
            raise ManifestMismatch("unsafe_path", directory / rel)
    if listed != sorted(set(listed)):
        raise ManifestMismatch("file_order", directory / MANIFEST_NAME, "files must be sorted and unique")
    on_disk = _data_files(directory)
    for rel in listed:
        if rel not in on_disk:
            raise ManifestMismatch("missing", directory / rel)
    extra = [rel for rel in on_disk if rel not in set(listed)]
    if extra:
        raise ManifestMismatch("extra", directory / extra[0], f"{len(extra)} unlisted file(s)")
    needed = {parent.as_posix() for rel in listed for parent in PurePosixPath(rel).parents} - {"."}
    stray = sorted(
        p.relative_to(directory).as_posix()
        for p in directory.rglob("*")
        if p.is_dir() and not p.is_symlink() and p.relative_to(directory).as_posix() not in needed
    )
    if stray:
        raise ManifestMismatch("extra", directory / stray[0], f"{len(stray)} unlisted directory(ies)")
    for entry in manifest.files:
        path = directory / entry.path
        try:
            size = path.stat().st_size  # follows a symlink: a dangling one is a missing file
        except OSError as exc:
            raise ManifestMismatch("missing", path, f"cannot read: {exc.__class__.__name__}") from exc
        if size < entry.bytes:
            raise ManifestMismatch("truncated", path, f"{size} bytes, manifest says {entry.bytes}")
        if size > entry.bytes:
            raise ManifestMismatch("grown", path, f"{size} bytes, manifest says {entry.bytes}")
        digest, rows, _ = _file_facts(path, parse=False)
        if digest != entry.sha256:
            raise ManifestMismatch("changed", path, "sha256 differs from the manifest")
        if rows != entry.rows:
            raise ManifestMismatch("row_count", path, f"{rows} rows, manifest says {entry.rows}")
    return LabelSnapshot(directory=directory, manifest=manifest)


def load_snapshot_set(directories: Iterable[Path | str]) -> tuple[LabelSnapshot, ...]:
    """Load several snapshots, sorted by ``snapshot_id``; refuse a repeated id."""
    snapshots = [load_snapshot(d) for d in directories]
    seen: set[str] = set()
    for snap in snapshots:
        if snap.snapshot_id in seen:
            raise DuplicateSnapshot(snap.snapshot_id)
        seen.add(snap.snapshot_id)
    return tuple(sorted(snapshots, key=lambda s: s.snapshot_id))


def label_snapshot_ids(snapshots: Iterable[LabelSnapshot]) -> list[str]:
    """The value of a run header's ``label_snapshot_ids`` (``mailroom_record.models.Run``,
    a list of strings): ``<snapshot_id>@<manifest_hash>`` per snapshot, sorted, so the run
    names the exact manifest of every snapshot it was scored against."""
    return [f"{s.snapshot_id}@{s.manifest_hash}" for s in sorted(snapshots, key=lambda s: s.snapshot_id)]


def snapshot_refs(snapshots: Iterable[LabelSnapshot]) -> tuple[dict[str, str], ...]:
    """What a run records about the labels it was scored against, sorted by snapshot id."""
    return tuple(
        {"snapshot_id": s.snapshot_id, "kind": s.kind, "manifest_hash": s.manifest_hash}
        for s in sorted(snapshots, key=lambda s: s.snapshot_id)
    )
