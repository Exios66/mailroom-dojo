"""Content-addressed blob store with tombstones (ADR-0003).

Layout under the store root::

    blobs/<aa>/<bb>/<sha256>.zst          content, zstd level 9 (one frame)
    blobs/<aa>/<bb>/<sha256>.tombstone    deletion record: JSON {sha256, reason, tombstoned_at}

Rules:

- ``put`` writes to a temp file in the target directory, fsyncs it, renames it into place
  and fsyncs the directory, so a blob is either whole or absent. It is idempotent: if the
  content is already present nothing is rewritten. Two writers racing on the same bytes
  both rename an identical file into place, which is harmless.
- A tombstone wins over content. Once ``<sha>.tombstone`` exists the blob is
  ``tombstoned``: ``get`` raises ``BlobTombstoned`` and ``put`` of the same bytes raises
  ``BlobTombstoned`` too, so a deletion request is never silently undone.
- Tombstones are created exclusively (temp file, fsync, ``os.link``): under concurrent
  ``tombstone`` calls the first record written is kept and never replaced.
- Files are created 0600 (owner only), because blobs hold private document content.
- ``tombstone`` of a blob that was never stored (or was lost) raises ``BlobMissing``: a
  tombstone passes verification, so it must not be used to paper over a lost blob.
- ``get`` re-hashes the decompressed bytes and raises ``BlobCorrupt`` on any mismatch or
  decoding error.

Hashes are validated (64 lowercase hex) before they touch a path.
"""

from __future__ import annotations

import contextlib
import hashlib
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


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _makedirs_durable(path: Path) -> None:
    """Create ``path`` and any missing parents, fsyncing each parent that gained an entry."""
    missing: list[Path] = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        with contextlib.suppress(FileExistsError):
            d.mkdir()
        _fsync_dir(d.parent)


def write_exclusive(final: Path, body: bytes) -> bool:
    """Write ``body`` to ``final`` only if ``final`` does not exist yet; first writer wins.

    The bytes go to a fsynced temp file that is hard-linked to ``final``. ``os.link``
    fails if ``final`` exists, so a concurrent second writer never replaces the first
    record, and a reader never sees a half-written file. Returns whether we created it.
    """
    directory = final.parent
    _makedirs_durable(directory)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{final.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
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

    def _write_atomic(self, final: Path, write: Any) -> None:
        """Write via ``write(fileobj)`` to a temp file, fsync, rename to ``final``, fsync dir."""
        directory = final.parent
        _makedirs_durable(directory)
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{final.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                write(f)
                f.flush()
                os.fsync(f.fileno())
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
        view = memoryview(data).cast("B")
        sha = hashlib.sha256(view).hexdigest()
        ref = BlobRef(sha256=sha, role=role, size=view.nbytes)  # validates the role first
        state = self.exists(sha)
        if state == "tombstoned":
            raise BlobTombstoned(sha)
        if state == "present":
            return ref

        def write(f: Any) -> None:
            cctx = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
            with cctx.stream_writer(f, size=view.nbytes, closefd=False) as zf:
                for start in range(0, view.nbytes, _CHUNK):
                    zf.write(view[start : start + _CHUNK])

        self._write_atomic(self.content_path(sha), write)
        # A tombstone written while we were writing must win: undo our content.
        if self.tombstone_path(sha).exists():
            with contextlib.suppress(FileNotFoundError):
                self.content_path(sha).unlink()
            raise BlobTombstoned(sha)
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
        if state == "present":
            record = {"sha256": sha256, "reason": reason, "tombstoned_at": utc_now()}
            body = canonical_json(record)
            write_exclusive(self.tombstone_path(sha256), body)
        content = self.content_path(sha256)
        if content.exists():
            with contextlib.suppress(FileNotFoundError):
                content.unlink()
            _fsync_dir(content.parent)

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
        try:
            with f, zstandard.ZstdDecompressor().stream_reader(f) as reader:
                while chunk := reader.read(_CHUNK):
                    digest.update(chunk)
                    parts.append(chunk)
        except zstandard.ZstdError as exc:
            raise BlobCorrupt(f"{sha256}: {exc}") from exc
        if digest.hexdigest() != sha256:
            raise BlobCorrupt(f"{sha256}: content hashes to {digest.hexdigest()}")
        return b"".join(parts)
