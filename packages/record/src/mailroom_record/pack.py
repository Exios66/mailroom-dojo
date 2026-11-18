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
  member whose hash or size differs, a member larger than its cap (``MAX_RUN_FILE`` for a
  segment or column file, ``MAX_SMALL_FILE`` for ``run.json``, a seal or the manifest,
  ``MAX_BLOB`` for a blob, ``MAX_TOMBSTONE`` for a tombstone), read from its tar header
  before a byte of it is staged (``pack_run`` refuses to write such a member:
  ``PackRefused``). The archive's zstd frame is decompressed a few whole blocks at a time,
  never more than 4 MiB per step, so a decompression bomb costs time, not memory. A blob
  member must be one the run names: a ``.zst`` member is decompressed as it streams and must
  hash to its name, a ``.tombstone`` member must be the record ``BlobStore.tombstone`` writes
  for its hash, and a blob member ``manifest.json``'s ``blobs`` does not list is refused; the
  list itself must equal the blobs the verified run's events name. Only those blobs are ever
  linked into the target, so an archive cannot plant content or a tombstone for a hash a
  later run would store. Blobs the run names but the archive lacks
  (``include_blobs=False``) are taken from the target root's blob store. It then runs
  ``verify_run`` on the staging store; any failing problem is ``PackCorrupt``. If the
  target recorded a pack of the run (``pack.json``, or ``prune.json`` for a pruned run),
  the archive must be that pack: an archive whose SHA-256 or latest seal differs from the
  record is ``UnpackConflict`` and nothing is changed (an older archive would otherwise put
  an earlier state back over a run pruned from a newer one). Only then
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

from mailroom_record.blobs import BlobStore, _dir_lock, _fsync_dir, _is_record, _makedirs_durable, _open_fd_for_write
from mailroom_record.hashing import canonical_json
from mailroom_record.log import MAINTENANCE_LOCK, MAX_LINE, PACK_RECORD, PRUNE_RECORD, EventLog
from mailroom_record.models import _RUN_ID_RE
from mailroom_record.seal import list_seals
from mailroom_record.verify import verify_run

__all__ = [
    "MAX_BLOB",
    "MAX_RUN_FILE",
    "MAX_SMALL_FILE",
    "MAX_TOMBSTONE",
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
_PACK_RECORD = PACK_RECORD
_PRUNE_RECORD = PRUNE_RECORD
_CHUNK = 1 << 20
# Size caps, checked from each member's tar header before a byte of it is staged (an
# over-cap member is ``PackCorrupt``) and by ``pack_run`` before it writes one (``PackRefused``),
# so an archive pack writes is always one unpack takes. They bound what a hostile archive can
# make unpack write or decompress, not what a run may hold in memory.
MAX_RUN_FILE = 64 * MAX_LINE  # a segment or column file: 4 GiB, 64 lines of the largest kind
MAX_SMALL_FILE = 256 * 1024 * 1024  # run.json, a seal, manifest.json
MAX_BLOB = 4 * 1024 * 1024 * 1024  # a blob's zstd file, and the bytes it decompresses to
MAX_TOMBSTONE = 64 * 1024  # a tombstone record (a hash, a reason and a time)
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


def _cap(name: str) -> int:
    """The largest size a member (or a packed file) of this name may have, read at call time."""
    if name == _MANIFEST or name.endswith("/run.json") or "/seals/" in name:
        return MAX_SMALL_FILE
    if name.startswith("blobs/"):
        return MAX_TOMBSTONE if name.endswith(".tombstone") else MAX_BLOB
    return MAX_RUN_FILE


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_BLOCK_MAX = 128 * 1024  # a zstd block never regenerates more than 128 KiB (RFC 8878)
_BATCH_BLOCKS = 32  # blocks per decompress call: at most 4 MiB of output at a time


class _ZstdIn(io.RawIOBase):
    """Read one whole zstd frame from ``f``; a truncated frame or trailing bytes raise ``PackCorrupt``.

    The frame is walked block by block (RFC 8878 frame and block headers, as
    ``blobs._zstd_frame_whole`` does for a file) and at most ``_BATCH_BLOCKS`` whole blocks
    are handed to the decompressor at a time. A block regenerates at most 128 KiB, so one
    call yields at most 4 MiB however far the frame expands: a decompression bomb costs
    time in proportion to its output, never memory. Walking the frame also finds its exact
    end: input that stops before the last block and the checksum is truncated, and any byte
    after them is refused (``zstandard``'s stream reader returns a frame cut inside its
    checksum without error, so this cannot be left to it). ``max_output`` (when given)
    refuses a frame that decompresses to more bytes.
    """

    def __init__(self, f: Any, *, what: str = "the archive", max_output: int | None = None) -> None:
        self._f = f
        self._what = what
        self._max_output = max_output
        self._produced = 0
        self._d = zstandard.ZstdDecompressor().decompressobj()
        self._buf = bytearray()
        self._bpos = 0
        self._started = False
        self._checksum = False
        self._done = False
        self._out = memoryview(b"")
        self._opos = 0

    def readable(self) -> bool:
        return True

    def _need(self, k: int) -> None:
        while len(self._buf) - self._bpos < k:
            raw = self._f.read(_CHUNK)
            if not raw:
                raise PackCorrupt(f"{self._what} ends before its zstd frame does (truncated)")
            if self._bpos:
                del self._buf[: self._bpos]
                self._bpos = 0
            self._buf += raw

    def _take(self, k: int) -> bytes:
        self._need(k)
        out = bytes(self._buf[self._bpos : self._bpos + k])
        self._bpos += k
        return out

    def _header(self) -> bytes:
        head = self._take(5)
        if head[:4] != _ZSTD_MAGIC or head[4] & 0x08:  # one frame, no reserved bit
            raise PackCorrupt(f"{self._what} is not one zstd frame")
        fhd = head[4]
        single = (fhd >> 5) & 1
        rest = (0 if single else 1) + (0, 1, 2, 4)[fhd & 3] + ((1 if single else 0), 2, 4, 8)[fhd >> 6]
        self._checksum = bool((fhd >> 2) & 1)
        return head + self._take(rest)

    def _batch(self) -> bytes:
        """The next whole blocks of the frame (with its header first, its checksum last)."""
        parts = []
        if not self._started:
            parts.append(self._header())
            self._started = True
        for _ in range(_BATCH_BLOCKS):
            head = self._take(3)
            v = int.from_bytes(head, "little")
            kind, length = (v >> 1) & 3, v >> 3
            if kind == 3 or length > _BLOCK_MAX:  # reserved block type, or a block too large
                raise PackCorrupt(f"{self._what}: a zstd block header is not valid")
            parts += [head, self._take(1 if kind == 1 else length)]  # an RLE block stores one byte
            if v & 1:  # the last block
                if self._checksum:
                    parts.append(self._take(4))
                self._done = True
                break
        return b"".join(parts)

    def _fill(self) -> bool:
        """Decompress the next batch into ``_out``; ``False`` at the frame's end."""
        while not self._done:
            batch = self._batch()
            try:
                out = self._d.decompress(batch)
            except zstandard.ZstdError as exc:
                raise PackCorrupt(f"{self._what}: zstd: {exc}") from None
            self._produced += len(out)
            if self._max_output is not None and self._produced > self._max_output:
                raise PackCorrupt(f"{self._what} decompresses to more than {self._max_output} bytes")
            if self._done:
                if not self._d.eof:
                    raise PackCorrupt(f"{self._what} ends before its zstd frame does (truncated)")
                if len(self._buf) > self._bpos or self._f.read(1):
                    raise PackCorrupt(f"bytes after {self._what}'s zstd frame")
            if out:
                self._out, self._opos = memoryview(out), 0
                return True
        return False

    def readinto(self, b: Any) -> int:
        view = memoryview(b).cast("B")
        if self._opos >= len(self._out) and not self._fill():
            return 0
        n = min(len(view), len(self._out) - self._opos)
        view[:n] = self._out[self._opos : self._opos + n]
        self._opos += n
        return n

    def drain(self) -> None:
        while self.read(_CHUNK):
            pass


@dataclass
class _Read:
    run_id: str
    manifest: dict[str, Any]
    archive_sha256: str


class _Hashing(io.RawIOBase):
    """Read ``f`` and hash every byte read, so the archive is hashed in the same pass."""

    def __init__(self, f: Any) -> None:
        self._f = f
        self.sha = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        data: bytes = self._f.read(size)
        self.sha.update(data)
        return data


def _read_archive(archive: Path, sink: Callable[[str, Iterator[bytes]], None]) -> _Read:
    """Stream ``archive``: check every member's name, type, hash and size against the
    manifest, handing each data member's chunks to ``sink(name, chunks)``."""
    seen: dict[str, dict[str, Any]] = {}
    blob_shas: set[str] = set()
    run_id: str | None = None
    manifest_raw: bytes | None = None
    try:
        with archive.open("rb") as raw:
            f = _Hashing(raw)
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
                    if m.size > _cap(name):
                        raise PackCorrupt(f"a member is larger than its cap ({_cap(name)} bytes)")
                    member_file = tar.extractfile(m)
                    if member_file is None:
                        raise PackCorrupt("a member has no data")
                    if name == _MANIFEST:
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

                    data = chunks()
                    if bm and bm["ext"] == "zst":
                        data = _checked_blob(bm["sha"], data)
                    elif bm:
                        data = _checked_tombstone(bm["sha"], data)
                    if bm:
                        blob_shas.add(bm["sha"])
                    sink(name, data)
                    for _ in data:  # whatever the sink left unread, checked all the same
                        pass
                    seen[name] = {"sha256": h.hexdigest(), "size": size}
            z.drain()
            archive_sha = f.sha.hexdigest()
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
    if not blob_shas <= set(blobs):
        raise PackCorrupt(f"the archive holds a blob file {_MANIFEST} does not name (a blob of no event of the run)")
    return _Read(run_id, manifest, archive_sha)


def _checked_blob(sha: str, chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Pass a blob member's bytes on while decompressing them (``_ZstdIn``: one whole frame,
    at most ``MAX_BLOB`` bytes out, a few blocks at a time); ``PackCorrupt`` at the end
    unless what they decompress to hashes to ``sha``, the name they are stored under."""
    taken: list[bytes] = []

    class _Tee:
        def read(self, _size: int = -1) -> bytes:
            chunk = next(chunks, b"")
            if chunk:
                taken.append(chunk)
            return chunk

    z = _ZstdIn(_Tee(), what=f"blob {sha}", max_output=MAX_BLOB)
    digest = hashlib.sha256()
    buf = bytearray(_CHUNK)
    while n := z.readinto(buf):
        digest.update(memoryview(buf)[:n])
        while taken:
            yield taken.pop(0)
    yield from taken
    if digest.hexdigest() != sha:
        raise PackCorrupt(f"blob {sha}: its content hashes to {digest.hexdigest()}, not to its name")


def _checked_tombstone(sha: str, chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Pass a tombstone member on if it is the record ``BlobStore.tombstone`` writes for
    ``sha`` (a JSON object with this hash, a non-empty reason and a time), as the blob store
    and ``verify_run`` read one; else ``PackCorrupt``. Its size was capped by ``MAX_TOMBSTONE``."""
    body = b"".join(chunks)
    try:
        record = json.loads(body)
    except (ValueError, RecursionError):
        record = None
    if not _is_record(record, sha):
        raise PackCorrupt(f"blob {sha}: the tombstone member is not the tombstone record of this hash")
    yield body


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
                    members[name] = _add_file(tar, name, path)
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


def _add_file(tar: tarfile.TarFile, name: str, path: Path) -> dict[str, Any]:
    """Stream ``path`` into ``tar`` as member ``name``; its SHA-256 and size. A file over
    its cap is ``PackRefused`` (unpack would refuse the member)."""
    with path.open("rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size > _cap(name):
            raise PackRefused(f"{name} is larger than its cap ({_cap(name)} bytes); an archive cannot hold it")
        hashed = _Hashing(f)
        tar.addfile(_tarinfo(name, size), hashed)  # reads exactly ``size`` bytes, or raises
    return {"sha256": hashed.sha.hexdigest(), "size": size}


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


def _refuse_another_pack(read: _Read, root: Path) -> None:
    """``UnpackConflict`` when ``root`` recorded a pack of this run (``pack.json``, or a prune
    by ``prune.json``) and ``read`` is not that archive: its SHA-256 or its latest seal differ.

    An older archive over a run pruned from a newer one would put back an earlier state
    beside the newer Parquet files and drop ``prune.json``, which leaves the run unsealed,
    the newer archive unrestorable and the import guard gone.
    """
    run_dir = root / "runs" / read.run_id
    pack_path, prune_path = run_dir / _PACK_RECORD, run_dir / _PRUNE_RECORD
    if not (pack_path.is_file() or prune_path.is_file()):
        return
    try:
        record = json.loads(pack_path.read_bytes())
        recorded = (record["archive_sha256"], record["last_seal"]["n"], record["last_seal"]["hash"])
        archive = str(record["archive"])
    except (OSError, ValueError, RecursionError, KeyError, TypeError):
        raise UnpackConflict(
            f"runs/{read.run_id}: this store recorded a pack of the run but its {_PACK_RECORD} is "
            "missing or unreadable, so this archive cannot be checked against it"
        ) from None
    seal = read.manifest.get("last_seal")
    given = (read.archive_sha256, *(seal.get(k) for k in ("n", "hash"))) if isinstance(seal, dict) else None
    if given != recorded:
        state = "pruned from" if prune_path.is_file() else "packed as"
        raise UnpackConflict(
            f"runs/{read.run_id}: this store holds the run {state} {archive} (seal {recorded[1]}); "
            f"this archive is a different pack (seal {seal.get('n') if isinstance(seal, dict) else '?'}); "
            "unpack the recorded archive instead"
        )


def _place(read: _Read, staging: Path, root: Path) -> str:
    """Verify the staged run and place it under ``root``; the caller holds the maintenance lock."""
    run_id = read.run_id
    _refuse_another_pack(read, root)  # under the lock: before anything is read from or put in root
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
    named = set(_named_blobs(s_log, run_id))
    if named != set(read.manifest["blobs"]):
        raise PackCorrupt(f"the blob list of {_MANIFEST} is not the blobs the run's events name")

    run_rel = [rel for rel in read.manifest["members"] if rel.startswith("runs/")]
    blob_rel = []  # only blobs the verified run names reach the target (checked above, kept here)
    for rel in read.manifest["members"]:
        m = _BLOB_MEMBER.fullmatch(rel)
        if m and m["sha"] in named:
            blob_rel.append(rel)
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
