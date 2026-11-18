"""Content-addressed blob store with tombstones (ADR-0003).

Layout under the store root::

    blobs/<aa>/<bb>/<sha256>.zst          content, zstd level 9 (one frame)
    blobs/<aa>/<bb>/<sha256>.tombstone    deletion record: JSON {sha256, reason, tombstoned_at}

Rules:

- ``put`` snapshots its input (a ``bytearray`` or ``memoryview`` a caller could change
  meanwhile is copied once; ``bytes`` is not), writes to a temp file in the target
  directory, fsyncs it, renames it into place and fsyncs the directory, so a blob is either
  whole or absent. Every directory from the shard up to the store root is fsynced on every
  write, created or found, so a shard left unsynced by a writer that crashed after
  ``mkdir`` is made durable by the next one. It is idempotent: if the content is already
  present nothing is rewritten, but the shard directory is fsynced again before the ref is
  returned (an earlier ``put`` may have renamed the file and then failed its directory
  fsync). Two writers racing on the same bytes both rename an identical file into place,
  which is harmless.
- A tombstone wins over content. Once ``<sha>.tombstone`` exists the blob is
  ``tombstoned``: ``get`` raises ``BlobTombstoned`` and ``put`` of the same bytes raises
  ``BlobTombstoned`` too, so a deletion request is never silently undone. ``put``'s final
  rename and ``tombstone``'s record-and-unlink hold one ``flock`` on the shard directory
  (across threads and processes), and ``put`` checks for a tombstone under it, so a ``put``
  that started before a deletion can never put the deleted content back. ``get`` checks
  for a tombstone again after reading, so a read that ends after a deletion was recorded
  never returns the content.
- Tombstones are created exclusively (temp file, fsync, ``os.link``): under concurrent
  ``tombstone`` calls the first record written is kept and never replaced. Before the
  content is unlinked the directory is fsynced (the record is durable first), and after
  it is fsynced again, also when a retry finds the content already gone. A tombstone file
  that is not a readable record (empty, truncated, not this hash's) is replaced atomically
  by a new record on the next ``tombstone`` call; a readable record is never replaced.
- Files are created 0600 (owner only), because blobs hold private document content.
- ``tombstone`` of a blob that was never stored (or was lost) raises ``BlobMissing``: a
  tombstone passes verification, so it must not be used to paper over a lost blob.
- ``get`` re-hashes the decompressed bytes and raises ``BlobCorrupt`` on any mismatch or
  decoding error, or when the file is not exactly one whole zstd frame: a frame cut inside
  its last (empty) block still decodes to every byte, so the digest alone cannot see it.

Hashes are validated (64 lowercase hex) before they touch a path.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

import zstandard

from mailroom_record.hashing import canonical_json
from mailroom_record.models import BlobRef, utc_now

__all__ = ["BlobCorrupt", "BlobMissing", "BlobStore", "BlobTombstoned"]

ZSTD_LEVEL = 9
_CHUNK = 4 * 1024 * 1024
_SHA_RE = re.compile(r"[0-9a-f]{64}")  # used with fullmatch: no trailing newline


class BlobMissing(LookupError):
    """No content and no tombstone exist for this hash."""


class BlobTombstoned(LookupError):
    """The content was deliberately deleted; only its tombstone remains."""


class BlobCorrupt(ValueError):
    """The stored file does not decode to bytes with the expected SHA-256."""


def _check_sha(sha256: str) -> str:
    if not isinstance(sha256, str) or not _SHA_RE.fullmatch(sha256):
        raise ValueError(f"not a lowercase hex SHA-256: {sha256!r}")
    return sha256


class _FdWriter(io.RawIOBase):
    """A raw file over ``fd`` that writes with ``os.write`` and fails on a write of 0 bytes.

    ``os.fdopen(fd, "wb")`` puts a ``BufferedWriter`` over ``FileIO``, and
    ``BufferedWriter`` retries a raw write that returns 0 forever. Like the log writer,
    every write path here raises ``OSError`` instead of spinning.
    """

    def __init__(self, fd: int, name: str) -> None:
        self._fd = fd
        self._name = name

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd

    def write(self, b: Any) -> int:
        if not memoryview(b).nbytes:
            return 0
        n = os.write(self._fd, b)
        if n <= 0:
            raise OSError(errno.EIO, f"os.write wrote 0 bytes to {self._name}")
        return n

    def seek(self, pos: int, whence: int = 0) -> int:
        return os.lseek(self._fd, pos, whence)

    def tell(self) -> int:
        return os.lseek(self._fd, 0, os.SEEK_CUR)

    def close(self) -> None:
        if not self.closed:
            try:
                os.close(self._fd)
            finally:
                super().close()


def _open_fd_for_write(fd: int, name: str) -> io.BufferedWriter:
    """A buffered binary writer over ``fd`` (closing it closes ``fd``); see ``_FdWriter``."""
    return io.BufferedWriter(_FdWriter(fd, name))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _makedirs_durable(path: Path, root: Path | None = None) -> None:
    """Create ``path`` and any missing parents, then make every entry on the way durable.

    Without ``root``: each parent that gained an entry is fsynced. With ``root`` (a known
    durable store root that ``path`` is under): every directory from ``path``'s parent up
    to ``root`` is fsynced, whether this call created the entry below it or found it, so an
    entry another process created and crashed before syncing becomes durable now too.
    """
    missing: list[Path] = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        with contextlib.suppress(FileExistsError):
            d.mkdir()
        if root is None:
            _fsync_dir(d.parent)
    if root is None:
        return
    root = Path(root)
    if root != path and root not in path.parents:
        raise ValueError(f"{path} is not under the store root {root}")
    d = path
    while d != root:
        _fsync_dir(d.parent)
        d = d.parent


@contextlib.contextmanager
def _dir_lock(directory: Path) -> Any:
    """An exclusive ``flock`` on ``directory`` itself (threads and processes); no lock file."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the flock


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _zstd_frame_whole(f: Any) -> bool:
    """Whether the open binary file ``f`` holds exactly one whole zstd frame, nothing after it.

    Walks the frame and block headers (RFC 8878) without decompressing: every block must be
    there in full up to the one marked last, then the checksum if the frame has one, then
    the end of the file. A decoder fed a frame cut inside its last block returns every byte
    and no error, so this is the only way to see such a cut. Leaves ``f`` at an unspecified
    position.
    """
    size = os.fstat(f.fileno()).st_size
    f.seek(0)
    head = f.read(5)
    if len(head) < 5 or head[:4] != _ZSTD_MAGIC:
        return False
    fhd = head[4]
    if fhd & 0x08:  # reserved bit
        return False
    single_segment = (fhd >> 5) & 1
    pos = 5 + (0 if single_segment else 1) + (0, 1, 2, 4)[fhd & 3] + ((1 if single_segment else 0), 2, 4, 8)[fhd >> 6]
    while True:
        f.seek(pos)
        block = f.read(3)
        if len(block) < 3:
            return False
        v = int.from_bytes(block, "little")
        kind, length = (v >> 1) & 3, v >> 3
        if kind == 3:  # reserved block type
            return False
        pos += 3 + (1 if kind == 1 else length)  # an RLE block stores one byte
        if pos > size:
            return False
        if v & 1:  # last block
            break
    if (fhd >> 2) & 1:
        pos += 4
    return pos == size


def _tombstone_readable(path: Path, sha256: str) -> bool:
    """Whether ``path`` holds the record ``tombstone`` writes: this hash, a reason, a time."""
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):  # unreadable, empty, not JSON (UnicodeDecodeError too)
        return False
    return (
        isinstance(raw, dict)
        and raw.get("sha256") == sha256
        and isinstance(raw.get("reason"), str)
        and bool(raw["reason"].strip())
        and isinstance(raw.get("tombstoned_at"), str)
        and bool(raw["tombstoned_at"])
    )


def write_exclusive(final: Path, body: bytes, root: Path | None = None) -> bool:
    """Write ``body`` to ``final`` only if ``final`` does not exist yet; first writer wins.

    The bytes go to a fsynced temp file that is hard-linked to ``final``. ``os.link``
    fails if ``final`` exists, so a concurrent second writer never replaces the first
    record, and a reader never sees a half-written file. Returns whether we created it.
    ``root`` is passed to ``_makedirs_durable`` for the directory.
    """
    directory = final.parent
    _makedirs_durable(directory, root)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{final.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with _open_fd_for_write(fd, tmp.name) as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, final)
            created = True
        except FileExistsError:
            created = False
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    _fsync_dir(directory)
    return created


class BlobStore:
    """Blobs under ``<root>/blobs``. Safe to share between threads and processes.

    Content and tombstone files are created with mode 0600 (``mkstemp``) on purpose:
    blobs hold private document content, so only the owning user can read them.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.blob_root = self.root / "blobs"

    # --- paths ----------------------------------------------------------------------

    def _dir(self, sha256: str) -> Path:
        return self.blob_root / sha256[:2] / sha256[2:4]

    def content_path(self, sha256: str) -> Path:
        _check_sha(sha256)
        return self._dir(sha256) / f"{sha256}.zst"

    def tombstone_path(self, sha256: str) -> Path:
        _check_sha(sha256)
        return self._dir(sha256) / f"{sha256}.tombstone"

    # --- state ----------------------------------------------------------------------

    def exists(self, sha256: str) -> Literal["present", "tombstoned", "missing"]:
        if self.tombstone_path(sha256).exists():
            return "tombstoned"
        if self.content_path(sha256).exists():
            return "present"
        # A concurrent tombstone() may have written its record and unlinked the content
        # between the two checks above; look again so that is not reported as "missing".
        if self.tombstone_path(sha256).exists():
            return "tombstoned"
        return "missing"

    def size_on_disk(self) -> int:
        """Bytes used by stored content and tombstone files (temp files excluded)."""
        if not self.blob_root.exists():
            return 0
        total = 0
        for pattern in ("*.zst", "*.tombstone"):
            for p in self.blob_root.glob(f"*/*/{pattern}"):
                with contextlib.suppress(FileNotFoundError):
                    total += p.stat().st_size
        return total

    # --- writes ---------------------------------------------------------------------

    def _write_atomic(self, final: Path, write: Any, unless: Path | None = None) -> None:
        """Write via ``write(fileobj)`` to a temp file, fsync, rename to ``final``, fsync dir.

        The rename holds the shard directory's lock; with ``unless``, it is skipped (and
        ``BlobTombstoned`` raised) when ``unless`` exists by then.
        """
        directory = final.parent
        _makedirs_durable(directory, self.root)
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{final.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with _open_fd_for_write(fd, tmp.name) as f:
                write(f)
                f.flush()
                os.fsync(f.fileno())
            with _dir_lock(directory):
                if unless is not None and unless.exists():
                    raise BlobTombstoned(final.name.split(".", 1)[0])
                os.replace(tmp, final)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise
        _fsync_dir(directory)

    def put(self, data: bytes, role: str) -> BlobRef:
        """Store ``data`` once under its SHA-256 and return a ref with the given role.

        Raises ``BlobTombstoned`` if these bytes were deleted, ``ValueError`` for a bad role.
        """
        if isinstance(data, str) or not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"blob data must be bytes-like, got {type(data).__name__}")
        if not isinstance(data, bytes):  # mutable or a view of mutable memory: hash and store one snapshot
            data = memoryview(data).tobytes()
        view = memoryview(data).cast("B")
        sha = hashlib.sha256(view).hexdigest()
        ref = BlobRef(sha256=sha, role=role, size=view.nbytes)  # validates the role first
        state = self.exists(sha)
        if state == "tombstoned":
            raise BlobTombstoned(sha)
        if state == "present":
            # Visible is not durable: an earlier put may have renamed it and then failed
            # its directory fsync. Make sure before the ref lets an event name it.
            _makedirs_durable(self._dir(sha), self.root)
            _fsync_dir(self._dir(sha))
            return ref

        def write(f: Any) -> None:
            cctx = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
            with cctx.stream_writer(f, size=view.nbytes, closefd=False) as zf:
                for start in range(0, view.nbytes, _CHUNK):
                    zf.write(view[start : start + _CHUNK])

        # A tombstone recorded while we were writing wins: the rename checks under the lock
        # tombstone() holds, so deleted content is never put back, even for a moment.
        self._write_atomic(self.content_path(sha), write, unless=self.tombstone_path(sha))
        return ref

    def tombstone(self, sha256: str, reason: str) -> None:
        """Delete the content, keeping ``<sha>.tombstone`` with the reason and UTC time.

        Idempotent: a second call keeps the first record and finishes any half-done
        deletion. Concurrent calls (threads or processes) are safe: the tombstone file is
        created exclusively, so the first record written is the one kept. Raises
        ``BlobMissing`` if the blob was never stored.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a tombstone needs a non-empty reason")
        state = self.exists(sha256)
        if state == "missing":
            raise BlobMissing(sha256)
        directory = self._dir(sha256)
        tomb, content = self.tombstone_path(sha256), self.content_path(sha256)
        record = canonical_json({"sha256": sha256, "reason": reason, "tombstoned_at": utc_now()})
        _makedirs_durable(directory, self.root)
        with _dir_lock(directory):
            if not tomb.exists():
                write_exclusive(tomb, record, self.root)
            elif not _tombstone_readable(tomb, sha256):
                # Damaged (empty, truncated, another hash's): replace it whole with a new
                # record. A readable record is the first one written and is never replaced.
                self._write_atomic_locked(tomb, record)
            _fsync_dir(directory)  # the record is durable before the content goes
            with contextlib.suppress(FileNotFoundError):
                content.unlink()
            _fsync_dir(directory)  # also when a retry finds the content already gone

    def _write_atomic_locked(self, final: Path, body: bytes) -> None:
        """Replace ``final`` with ``body`` (temp file, fsync, rename); the caller holds the lock."""
        fd, tmp_name = tempfile.mkstemp(dir=final.parent, prefix=f".{final.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with _open_fd_for_write(fd, tmp.name) as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise

    # --- reads ----------------------------------------------------------------------

    def tombstone_info(self, sha256: str) -> dict[str, Any]:
        """The tombstone record. Raises ``BlobMissing`` if this hash has no tombstone."""
        try:
            return json.loads(self.tombstone_path(sha256).read_bytes())
        except FileNotFoundError:
            raise BlobMissing(sha256) from None

    def get(self, sha256: str) -> bytes:
        """The original bytes. Raises ``BlobTombstoned``, ``BlobMissing`` or ``BlobCorrupt``."""
        state = self.exists(sha256)
        if state == "tombstoned":
            raise BlobTombstoned(sha256)
        try:
            f = self.content_path(sha256).open("rb")
        except FileNotFoundError:
            if self.tombstone_path(sha256).exists():
                raise BlobTombstoned(sha256) from None
            raise BlobMissing(sha256) from None
        digest = hashlib.sha256()
        parts: list[bytes] = []
        with f:
            if not _zstd_frame_whole(f):
                raise BlobCorrupt(f"{sha256}: the file is not one whole zstd frame")
            f.seek(0)
            try:
                with zstandard.ZstdDecompressor().stream_reader(f, closefd=False) as reader:
                    while chunk := reader.read(_CHUNK):
                        digest.update(chunk)
                        parts.append(chunk)
            except zstandard.ZstdError as exc:
                raise BlobCorrupt(f"{sha256}: {exc}") from exc
        if digest.hexdigest() != sha256:
            raise BlobCorrupt(f"{sha256}: content hashes to {digest.hexdigest()}")
        if self.tombstone_path(sha256).exists():  # deleted while we read: never hand it out
            raise BlobTombstoned(sha256)
        return b"".join(parts)
