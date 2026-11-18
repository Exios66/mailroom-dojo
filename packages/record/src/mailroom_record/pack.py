"""Pack a sealed run into one archive, unpack it, and prune a packed run (Task 11).

Archive format (``.tar.zst``): one zstd frame with a content checksum, holding a POSIX tar
stream. Members, in this order::

    runs/<run_id>/run.json
    runs/<run_id>/segments/<writer>[.<k>].jsonl
    runs/<run_id>/seals/<n>.json
    runs/<run_id>/columns/events-<n>.parquet
    blobs/<aa>/<bb>/<sha256>.zst | .tombstone      (unless include_blobs=False)
    manifest.json                                   always last

``manifest.json`` holds the format, the run id, the latest seal's ``n`` and hash, whether
blobs are included, every blob the run names (``blobs``) and every other member's SHA-256
and size (``members``). It holds hashes, sizes and file names only, never content. Tar
metadata is fixed (mtime 0, uid and gid 0, mode 0600), so packing one run twice gives the
same bytes.

Rules
-----
- ``pack_run`` refuses with ``RunNotSealed`` unless the latest seal covers every flushed
  event (its ``doc_heads`` equal the log's), and with ``PackRefused`` unless ``verify_run``
  finds the run ok, so an archive never holds a run that could not be restored. It checks
  the seal coverage again after writing (a writer may have flushed meanwhile; then the
  archive is removed and ``RunNotSealed`` raised), reads the written archive back against
  its manifest, and only then links it to ``out`` (never replacing an existing file:
  ``FileExistsError``). A failed write leaves nothing at ``out``. It records the pack in
  ``runs/<run_id>/pack.json`` (archive path and SHA-256, the seal it covers, the members),
  which ``prune_run`` requires.
- ``unpack_run`` reads the archive as a stream into a staging store under the target root
  (``.unpack-*``), refusing with ``PackCorrupt`` anything that is not exactly what
  ``pack_run`` writes: a zstd or tar error, a truncated stream, a member that is not a
  regular file or whose name is not one of the names above (absolute, ``..``, links,
  unknown files), a member twice, a member missing from or not listed in the manifest, a
  member whose hash or size differs. Blobs the run names but the archive lacks
  (``include_blobs=False``) are taken from the target root's blob store. It then runs
  ``verify_run`` on the staging store; any failing problem is ``PackCorrupt``. Only then
  does anything reach the target: if a run file already there differs from the archive's,
  ``UnpackConflict`` and nothing is changed; equal files are left alone, missing ones are
  hard-linked into place (blobs first, ``run.json`` last), so a second unpack is a no-op and
  an unpack interrupted part way is finished by the next. A blob already present or
  tombstoned in the target is never replaced. From taking blobs from the target until
  ``run.json`` is placed, it holds the maintenance lock shared, as a writer does (waiting
  up to ``lock_timeout`` seconds while a prune holds it: ``StoreBusy``), so a concurrent
  ``prune_run`` of another run cannot delete a blob this unpack relies on before the
  unpacked run names it. The staging store is removed on return or
  error; a process killed mid-unpack leaves it behind (remove ``.unpack-*`` by hand).
- ``prune_run`` removes the segments and seals (and, unless ``keep_columns``, the Parquet
  files) of a packed run, and the blobs its archive holds that no other run names. It
  holds the store's maintenance lock exclusive (``log.MAINTENANCE_LOCK``; every live writer
  holds it shared), waiting up to ``lock_timeout`` seconds (``StoreBusy``), so no append
  can name a blob between the check and the delete. What other runs name is read from
  their logs, the authoritative record, never the index: every 64-hex string in every other
  run's segments (whole, torn or corrupt lines alike), plus the blobs of every other run
  recorded as packed without blobs. It refuses (``PruneRefused``) unless ``pack.json`` names
  an archive whose bytes still hash to the recorded SHA-256 and that covers the latest
  seal, and (``RunNotSealed``) unless the latest seal covers every flushed event; it deletes
  a run file only when the archive holds the same bytes. It writes ``prune.json`` before
  deleting, so a prune interrupted part way is finished by the next call (which also
  re-reads what other runs name), and a prune of a pruned run is a no-op. A pack made
  without blobs deletes no blob. Blob content is unlinked, not tombstoned: the archive keeps
  it, and ``unpack_run`` puts it back.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import zstandard

from mailroom_record.blobs import BlobStore, _dir_lock, _fsync_dir, _makedirs_durable, _open_fd_for_write
from mailroom_record.hashing import canonical_json
from mailroom_record.log import MAINTENANCE_LOCK, EventLog
from mailroom_record.models import _RUN_ID_RE
from mailroom_record.seal import list_seals
from mailroom_record.verify import verify_run

__all__ = [
    "PACK_FORMAT",
    "PackCorrupt",
    "PackManifest",
    "PackRefused",
    "PruneRefused",
    "PruneReport",
    "RunNotSealed",
    "StoreBusy",
    "UnpackConflict",
    "pack_run",
    "prune_run",
    "unpack_run",
]

PACK_FORMAT = "mailroom-record-pack/1"
_MANIFEST = "manifest.json"
_PACK_RECORD = "pack.json"
_PRUNE_RECORD = "prune.json"
_CHUNK = 1 << 20
_MAX_MANIFEST = 256 * 1024 * 1024
_HEX64 = re.compile(rb"[0-9a-f]{64}")
_SHA = r"[0-9a-f]{64}"
_RUN_MEMBER = re.compile(
    r"runs/(?P<run>[^/]+)/(?:run\.json"
    r"|segments/[A-Za-z0-9][A-Za-z0-9_-]{0,99}(?:\.[1-9][0-9]*)?\.jsonl"
    r"|seals/(?:0|[1-9][0-9]*)\.json"
    r"|columns/events-(?:0|[1-9][0-9]*)\.parquet)"
)
_BLOB_MEMBER = re.compile(rf"blobs/(?P<a>[0-9a-f]{{2}})/(?P<b>[0-9a-f]{{2}})/(?P<sha>{_SHA})\.(?P<ext>zst|tombstone)")


class RunNotSealed(RuntimeError):
    """The run has no seal, or flushed events its latest seal does not cover: seal it first."""


class PackRefused(RuntimeError):
    """The run does not verify, so an archive of it could not be restored."""


class PackCorrupt(ValueError):
    """The archive is damaged, incomplete or not one ``pack_run`` writes."""


class UnpackConflict(FileExistsError):
    """The target root holds a different file where the archive's run would go."""


class PruneRefused(RuntimeError):
    """Pruning would lose data: no current pack of the run, or files the pack lacks."""


class StoreBusy(TimeoutError):
    """The maintenance lock was not free within the timeout (a writer, unpack or prune is live)."""


@dataclass(frozen=True)
class PackManifest:
    run_id: str
    last_seal_n: int
    last_seal_hash: str
    include_blobs: bool
    members: dict[str, dict[str, Any]]
    blobs: list[str]
    path: Path
    archive_sha256: str


@dataclass(frozen=True)
class PruneReport:
    run_id: str
    archive: str
    blobs_removed: list[str] = field(default_factory=list)
    blobs_kept: int = 0
    segments_removed: int = 0
    seals_removed: int = 0
    columns_removed: int = 0


# --- shared helpers ---------------------------------------------------------------


def _open_store(root: Path) -> tuple[EventLog, BlobStore]:
    blobs = BlobStore(root)
    return EventLog(root, blobs), blobs


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _coverage(log: EventLog, run_id: str) -> tuple[int, str]:
    """``(n, hash)`` of the latest seal if it covers every flushed event, else ``RunNotSealed``."""
    seals = list_seals(log, run_id)
    if not seals:
        raise RunNotSealed(f"run {run_id!r} has no seal; seal it first")
    latest = seals[-1]
    if dict(latest.doc_heads) != log.doc_heads(run_id):
        raise RunNotSealed(f"run {run_id!r} has flushed events not covered by seal {latest.n}; seal it first")
    return latest.n, latest.hash


def _run_files(run_dir: Path, *, columns: bool = True) -> list[tuple[str, Path]]:
    """``(member name relative to the run dir, path)`` of the files an archive holds, in order."""
    out: list[tuple[str, Path]] = [("run.json", run_dir / "run.json")]
    for sub, pattern in (("segments", "*.jsonl"), ("seals", "*.json"), ("columns", "*.parquet")):
        if sub == "columns" and not columns:
            continue
        d = run_dir / sub
        if d.is_dir():
            out += [(f"{sub}/{p.name}", p) for p in sorted(d.glob(pattern)) if p.is_file() and not p.name.startswith(".")]
    return out


def _named_blobs(log: EventLog, run_id: str) -> list[str]:
    """Every blob SHA-256 the run's events name (inputs and outputs), sorted."""
    names: set[str] = set()
    for s in log.iter_stored(run_id):
        for key in ("inputs", "outputs"):
            refs = s.raw.get(key)
            for ref in refs if isinstance(refs, list) else []:
                if isinstance(ref, dict) and isinstance(ref.get("sha256"), str):
                    names.add(ref["sha256"])
    return sorted(names)


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


class _ZstdIn(io.RawIOBase):
    """Read one whole zstd frame from ``f``; a truncated frame or trailing bytes raise ``PackCorrupt``.

    ``zstandard``'s stream reader returns a frame cut inside its checksum without error, so
    the frame's end is checked here: at end of input the decompressor must have seen the
    whole frame (``eof``) and nothing may follow it.
    """

    def __init__(self, f: Any) -> None:
        self._f = f
        self._d = zstandard.ZstdDecompressor().decompressobj()
        self._pending = b""
        self._done = False

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        view = memoryview(b).cast("B")
        while not self._pending and not self._done:
            raw = self._f.read(_CHUNK)
            if not raw:
                if not self._d.eof:
                    raise PackCorrupt("the archive ends before its zstd frame does (truncated)")
                self._done = True
                break
            if self._d.eof:
                raise PackCorrupt("bytes after the archive's zstd frame")
            try:
                self._pending = self._d.decompress(raw)
            except zstandard.ZstdError as exc:
                raise PackCorrupt(f"zstd: {exc}") from None
            if self._d.eof and self._d.unused_data:
                raise PackCorrupt("bytes after the archive's zstd frame")
        n = min(len(view), len(self._pending))
        view[:n] = self._pending[:n]
        self._pending = self._pending[n:]
        return n

    def drain(self) -> None:
        while self.read(_CHUNK):
            pass


@dataclass
class _Read:
    run_id: str
    manifest: dict[str, Any]


def _read_archive(archive: Path, sink: Callable[[str, Iterator[bytes]], None]) -> _Read:
    """Stream ``archive``: check every member's name, type, hash and size against the
    manifest, handing each data member's chunks to ``sink(name, chunks)``."""
    seen: dict[str, dict[str, Any]] = {}
    run_id: str | None = None
    manifest_raw: bytes | None = None
    try:
        with archive.open("rb") as f:
            z = _ZstdIn(f)
            with tarfile.open(fileobj=io.BufferedReader(z, _CHUNK), mode="r|") as tar:
                for m in tar:
                    name = m.name
                    if manifest_raw is not None:
                        raise PackCorrupt(f"member after {_MANIFEST}")
                    if not m.isreg():
                        raise PackCorrupt("a member is not a regular file")
                    if name in seen:
                        raise PackCorrupt("a member appears twice")
                    member_file = tar.extractfile(m)
                    if member_file is None:
                        raise PackCorrupt("a member has no data")
                    if name == _MANIFEST:
                        if m.size > _MAX_MANIFEST:
                            raise PackCorrupt(f"{_MANIFEST} is too large")
                        manifest_raw = member_file.read()
                        continue
                    rm = _RUN_MEMBER.fullmatch(name)
                    bm = _BLOB_MEMBER.fullmatch(name)
                    if rm:
                        if not _RUN_ID_RE.fullmatch(rm["run"]) or (run_id is not None and rm["run"] != run_id):
                            raise PackCorrupt("a member names another run or a bad run id")
                        run_id = rm["run"]
                    elif not (bm and bm["sha"][:2] == bm["a"] and bm["sha"][2:4] == bm["b"]):
                        raise PackCorrupt("a member name is not one a pack holds")
                    h = hashlib.sha256()
                    size = 0

                    def chunks(member_file: Any = member_file) -> Iterator[bytes]:
                        nonlocal size
                        for chunk in iter(lambda: member_file.read(_CHUNK), b""):
                            h.update(chunk)
                            size += len(chunk)
                            yield chunk

                    sink(name, chunks())
                    for _ in chunks():  # whatever the sink left unread
                        pass
                    seen[name] = {"sha256": h.hexdigest(), "size": size}
            z.drain()
    except PackCorrupt:
        raise
    except (tarfile.TarError, EOFError, zstandard.ZstdError) as exc:
        raise PackCorrupt(f"{type(exc).__name__} while reading the archive") from None
    if manifest_raw is None:
        raise PackCorrupt(f"no {_MANIFEST}")
    try:
        manifest = json.loads(manifest_raw)
    except (ValueError, RecursionError):
        raise PackCorrupt(f"{_MANIFEST} is not JSON") from None
    if not isinstance(manifest, dict) or manifest.get("format") != PACK_FORMAT:
        raise PackCorrupt(f"{_MANIFEST} is not a {PACK_FORMAT} manifest")
    members = manifest.get("members")
    if not isinstance(members, dict) or members != seen:
        raise PackCorrupt(f"the members differ from {_MANIFEST} (missing, extra or changed)")
    if run_id is None or manifest.get("run_id") != run_id:
        raise PackCorrupt(f"{_MANIFEST} names another run, or the archive has no run")
    blobs = manifest.get("blobs")
    if not isinstance(blobs, list) or not all(isinstance(b, str) and re.fullmatch(_SHA, b) for b in blobs):
        raise PackCorrupt(f"{_MANIFEST} has no valid blob list")
    return _Read(run_id, manifest)


# --- pack -------------------------------------------------------------------------


def pack_run(root: Path, run_id: str, out: Path, *, include_blobs: bool = True) -> PackManifest:
    """Write ``run_id`` and the blobs it names to the archive ``out`` (see the module doc)."""
    root, out = Path(root), Path(out)
    log, blobs = _open_store(root)
    run_dir = log.run_dir(run_id)
    if not (run_dir / "run.json").is_file():
        raise FileNotFoundError(f"no run {run_id!r} under {root}")
    seal_n, seal_hash = _coverage(log, run_id)
    report = verify_run(log, blobs, run_id)
    if not report.ok:
        kinds = sorted({p.kind for p in report.problems if p.fails})
        raise PackRefused(f"run {run_id!r} does not verify ({', '.join(kinds)}); run verify")
    if out.exists():
        raise FileExistsError(f"{out} exists; packs are never overwritten")
    named = _named_blobs(log, run_id)

    files = [(f"runs/{run_id}/{rel}", p) for rel, p in _run_files(run_dir)]
    if include_blobs:
        for sha in named:
            for p in (blobs.content_path(sha), blobs.tombstone_path(sha)):
                if p.is_file():
                    files.append((str(p.relative_to(root)), p))

    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        members: dict[str, dict[str, Any]] = {}
        with _open_fd_for_write(fd, tmp.name) as raw:
            cctx = zstandard.ZstdCompressor(level=3, write_checksum=True)
            with cctx.stream_writer(raw, closefd=False) as zf, tarfile.open(
                fileobj=zf, mode="w|", format=tarfile.PAX_FORMAT
            ) as tar:
                for name, path in files:
                    data = path.read_bytes()
                    members[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                    tar.addfile(_tarinfo(name, len(data)), io.BytesIO(data))
                manifest = {
                    "format": PACK_FORMAT,
                    "run_id": run_id,
                    "last_seal": {"n": seal_n, "hash": seal_hash},
                    "include_blobs": include_blobs,
                    "blobs": named,
                    "members": members,
                }
                body = canonical_json(manifest)
                tar.addfile(_tarinfo(_MANIFEST, len(body)), io.BytesIO(body))
            raw.flush()
            os.fsync(raw.fileno())
        _read_archive(tmp, lambda _name, _chunks: None)  # what was written reads back whole
        # A writer may have flushed while we copied: the archive must hold the sealed state.
        if _coverage(log, run_id) != (seal_n, seal_hash):
            raise RunNotSealed(f"run {run_id!r} changed while it was packed; seal it and pack again")
        archive_sha = _sha_file(tmp)
        os.link(tmp, out)  # never replaces an existing file
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    _fsync_dir(out.parent)
    record = {
        "archive": str(out.resolve()),
        "archive_sha256": archive_sha,
        "last_seal": {"n": seal_n, "hash": seal_hash},
        "include_blobs": include_blobs,
        "blobs": named,
        "members": members,
    }
    _write_replace(run_dir / _PACK_RECORD, canonical_json(record))
    return PackManifest(
        run_id=run_id,
        last_seal_n=seal_n,
        last_seal_hash=seal_hash,
        include_blobs=include_blobs,
        members=members,
        blobs=named,
        path=out,
        archive_sha256=archive_sha,
    )


def _write_replace(path: Path, body: bytes) -> None:
    """Write ``body`` to ``path`` atomically (temp file, fsync, rename, directory fsync)."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with _open_fd_for_write(fd, tmp_name) as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
    _fsync_dir(path.parent)


# --- unpack -----------------------------------------------------------------------


def _write_staged(path: Path, chunks: Iterator[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with _open_fd_for_write(fd, path.name) as f:
        for chunk in chunks:
            f.write(chunk)
        f.flush()
        os.fsync(f.fileno())


def _same_bytes(a: Path, b: Path) -> bool:
    return a.stat().st_size == b.stat().st_size and _sha_file(a) == _sha_file(b)


def unpack_run(archive: Path, root: Path, *, lock_timeout: float = 60.0) -> str:
    """Restore the run in ``archive`` under ``root`` and return its id (see the module doc)."""
    archive, root = Path(archive), Path(root)
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=root, prefix=".unpack-"))
    try:
        read = _read_archive(archive, lambda name, chunks: _write_staged(staging / name, chunks))
        with _maintenance(root, lock_timeout, exclusive=False):
            return _place(read, staging, root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _place(read: _Read, staging: Path, root: Path) -> str:
    """Verify the staged run and place it under ``root``; the caller holds the maintenance lock."""
    run_id = read.run_id
    s_log, s_blobs = _open_store(staging)
    target = BlobStore(root)
    for sha in read.manifest["blobs"]:
        if s_blobs.exists(sha) != "missing":
            continue
        for src, dst in (
            (target.content_path(sha), s_blobs.content_path(sha)),
            (target.tombstone_path(sha), s_blobs.tombstone_path(sha)),
        ):
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copyfile(src, dst)
    report = verify_run(s_log, s_blobs, run_id)
    if not report.ok:
        kinds = sorted({p.kind for p in report.problems if p.fails})
        raise PackCorrupt(f"the unpacked run does not verify: {', '.join(kinds)}")

    run_rel = [rel for rel in read.manifest["members"] if rel.startswith("runs/")]
    blob_rel = [rel for rel in read.manifest["members"] if rel.startswith("blobs/")]
    for rel in run_rel:  # check everything before placing anything
        dst = root / rel
        if dst.exists() and not (dst.is_file() and _same_bytes(dst, staging / rel)):
            raise UnpackConflict(f"runs/{run_id}: {rel.split('/', 2)[2]} differs from the archive's")

    for rel in blob_rel:
        m = _BLOB_MEMBER.fullmatch(rel)
        assert m is not None  # checked by _read_archive
        sha = m["sha"]
        shard = target.content_path(sha).parent
        _makedirs_durable(shard, root)
        with _dir_lock(shard):
            if target.exists(sha) == "missing":
                with contextlib.suppress(FileExistsError):
                    os.link(staging / rel, root / rel)
        _fsync_dir(shard)

    def order(rel: str) -> tuple[int, str]:  # the order they were written in: run.json last here
        kind = rel.split("/")[2]
        return ({"segments": 0, "seals": 1, "columns": 2}.get(kind, 3), rel)

    placed_dirs: set[Path] = set()
    for rel in sorted(run_rel, key=order):
        dst = root / rel
        _makedirs_durable(dst.parent, root)
        try:
            os.link(staging / rel, dst)
        except FileExistsError:
            if not _same_bytes(dst, staging / rel):  # appeared since the check
                raise UnpackConflict(f"runs/{run_id}: {rel.split('/', 2)[2]} differs from the archive's") from None
        placed_dirs.add(dst.parent)
    for d in sorted(placed_dirs):
        _fsync_dir(d)
    prune_record = root / "runs" / run_id / _PRUNE_RECORD
    if prune_record.exists():  # the run is whole again
        prune_record.unlink()
        _fsync_dir(prune_record.parent)
    return run_id


# --- prune ------------------------------------------------------------------------


@contextlib.contextmanager
def _maintenance(root: Path, timeout: float, *, exclusive: bool = True) -> Iterator[None]:
    """Hold the store's maintenance lock (exclusive for a prune, shared for an unpack)."""
    fd = os.open(root / MAINTENANCE_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StoreBusy(f"the store is busy (a writer, unpack or prune is live) after {timeout:g}s; try again") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # releases the lock


def _names_elsewhere(log: EventLog, run_id: str) -> set[str]:
    """Every 64-hex string in other runs' segments, and the blobs of other runs packed
    without blobs: what pruning ``run_id`` must keep."""
    names: set[str] = set()
    runs = log.root / "runs"
    for other in sorted(runs.iterdir()) if runs.is_dir() else []:
        if other.name == run_id or not other.is_dir():
            continue
        seg_dir = other / "segments"
        for seg in sorted(seg_dir.iterdir()) if seg_dir.is_dir() else []:
            if not seg.is_file():
                continue
            with seg.open("rb") as f:
                tail = b""
                while chunk := f.read(_CHUNK):
                    buf = tail + chunk  # a name split across two chunks is still found
                    names.update(m.decode() for m in _HEX64.findall(buf))
                    tail = buf[-63:]
        record = other / _PACK_RECORD
        if record.is_file():
            try:
                rec = json.loads(record.read_bytes())
            except (ValueError, RecursionError):
                raise PruneRefused(f"run {other.name!r} has an unreadable {_PACK_RECORD}") from None
            if not rec.get("include_blobs", True):
                names.update(b for b in rec.get("blobs", []) if isinstance(b, str))
    return names


def prune_run(root: Path, run_id: str, *, keep_columns: bool = True, lock_timeout: float = 60.0) -> PruneReport:
    """Remove a packed run's segments and seals and its unshared blobs (see the module doc)."""
    root = Path(root)
    log, blobs = _open_store(root)
    run_dir = log.run_dir(run_id)
    if not (run_dir / "run.json").is_file():
        raise FileNotFoundError(f"no run {run_id!r} under {root}")
    with _maintenance(root, lock_timeout):
        record_path = run_dir / _PACK_RECORD
        if not record_path.is_file():
            raise PruneRefused(f"run {run_id!r}: no pack recorded; pack it first")
        record = json.loads(record_path.read_bytes())
        archive = Path(record["archive"])
        if not archive.is_file() or _sha_file(archive) != record["archive_sha256"]:
            raise PruneRefused(f"run {run_id!r}: the recorded archive is missing or changed; pack it again")
        prune_path = run_dir / _PRUNE_RECORD
        resuming = False
        if prune_path.is_file():
            with contextlib.suppress(ValueError, RecursionError):
                resuming = json.loads(prune_path.read_bytes()).get("archive_sha256") == record["archive_sha256"]
        if not resuming:
            n, h = _coverage(log, run_id)
            if (n, h) != (record["last_seal"]["n"], record["last_seal"]["hash"]):
                raise PruneRefused(f"run {run_id!r}: the pack does not cover the latest seal {n}; pack it again")

        members: dict[str, dict[str, Any]] = record["members"]
        doomed = _run_files(run_dir, columns=not keep_columns)
        doomed = [(rel, p) for rel, p in doomed if rel != "run.json"]
        for rel, p in doomed:  # delete nothing the archive does not hold byte for byte
            want = members.get(f"runs/{run_id}/{rel}")
            if want is None or want["size"] != p.stat().st_size or want["sha256"] != _sha_file(p):
                raise PruneRefused(f"run {run_id!r}: {rel} is not in the pack as it is on disk; seal and pack again")

        _write_replace(prune_path, canonical_json({"state": "started", "archive_sha256": record["archive_sha256"]}))
        candidates: list[str] = []
        if record.get("include_blobs", True):
            for name in members:
                m = _BLOB_MEMBER.fullmatch(name)
                if m and m["ext"] == "zst":
                    candidates.append(m["sha"])
        keep = _names_elsewhere(log, run_id)
        removed: list[str] = []
        kept = 0
        for sha in sorted(candidates):
            if sha in keep:
                kept += 1
                continue
            path = blobs.content_path(sha)
            with _dir_lock(path.parent) if path.parent.is_dir() else contextlib.nullcontext():
                if path.exists():
                    path.unlink()
                    removed.append(sha)
            if path.parent.is_dir():
                _fsync_dir(path.parent)

        counts = {"segments": 0, "seals": 0, "columns": 0}
        for rel, p in doomed:
            p.unlink()
            counts[rel.split("/", 1)[0]] += 1
        for sub in counts:
            if (run_dir / sub).is_dir():
                _fsync_dir(run_dir / sub)
        _write_replace(prune_path, canonical_json({"state": "done", "archive_sha256": record["archive_sha256"]}))
        return PruneReport(
            run_id=run_id,
            archive=str(archive),
            blobs_removed=removed,
            blobs_kept=kept,
            segments_removed=counts["segments"],
            seals_removed=counts["seals"],
            columns_removed=counts["columns"],
        )
