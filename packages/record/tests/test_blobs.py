"""Content-addressed blob store with tombstones (ADR-0003)."""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
from pathlib import Path

import pytest
from record_testkit import SENTINEL, put_in_child

from mailroom_record.blobs import BlobCorrupt, BlobMissing, BlobStore, BlobTombstoned
from mailroom_record.hashing import sha256_hex
from mailroom_record.models import BlobRef

# --- the plan's tests -------------------------------------------------------------


def test_same_bytes_stored_once(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    a, b = s.put(b"x" * 1000, "original"), s.put(b"x" * 1000, "original")
    assert a.sha256 == b.sha256
    assert len(list(tmp_path.rglob("*.zst"))) == 1


def test_roundtrip_arbitrary_bytes(tmp_path: Path) -> None:  # Review Focus 5
    data = os.urandom(1024) + b"\xff\xfe" + bytes(50 * 1024 * 1024)
    s = BlobStore(tmp_path)
    assert s.get(s.put(data, "original").sha256) == data


def test_tombstone_keeps_identity_drops_content(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"secret", "original")
    s.tombstone(ref.sha256, "client deletion request")
    assert s.exists(ref.sha256) == "tombstoned"
    with pytest.raises(BlobTombstoned):
        s.get(ref.sha256)


def test_missing_is_not_tombstoned(tmp_path: Path) -> None:
    assert BlobStore(tmp_path).exists("0" * 64) == "missing"


# --- layout, refs and round trips -------------------------------------------------


def test_layout_and_ref(tmp_path: Path) -> None:
    data = SENTINEL.encode()
    ref = BlobStore(tmp_path).put(data, "transcript")
    sha = sha256_hex(data)
    assert ref == BlobRef(sha256=sha, role="transcript", size=len(data))
    path = tmp_path / "blobs" / sha[:2] / sha[2:4] / f"{sha}.zst"
    assert path.is_file()
    # A zstd frame. Compression is not encryption (deferred to deploy, ADR-0003): short
    # text may be stored verbatim inside the frame, so this is not a leak guarantee.
    assert path.read_bytes()[:4] == b"\x28\xb5\x2f\xfd"


def test_compressible_bytes_shrink(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(SENTINEL.encode() * 10_000, "original")
    assert s.content_path(ref.sha256).stat().st_size < ref.size // 100


def test_random_50mb_roundtrip(tmp_path: Path) -> None:  # incompressible bytes
    data = os.urandom(50 * 1024 * 1024)
    s = BlobStore(tmp_path)
    assert s.get(s.put(data, "original").sha256) == data


@pytest.mark.parametrize("data", [b"", b"\x00", "é ✓".encode("utf-16"), bytes(range(256))])
def test_small_and_odd_bytes_round_trip(tmp_path: Path, data: bytes) -> None:
    s = BlobStore(tmp_path)
    assert s.get(s.put(data, "window").sha256) == data


def test_bytes_like_input_is_accepted(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(memoryview(b"abc"), "report")
    assert ref.sha256 == sha256_hex(b"abc") and s.get(ref.sha256) == b"abc"


def test_str_input_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        BlobStore(tmp_path).put("text", "original")  # type: ignore[arg-type]


def test_bad_role_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BlobStore(tmp_path).put(b"x", "notes")
    assert not list(tmp_path.rglob("*.zst"))


def test_same_bytes_different_role_share_one_file(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    a, b = s.put(b"same", "extraction"), s.put(b"same", "report")
    assert (a.role, b.role) == ("extraction", "report") and a.sha256 == b.sha256
    assert len(list(tmp_path.rglob("*.zst"))) == 1


def test_idempotent_put_does_not_rewrite(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"x" * 100, "original")
    path = s.content_path(ref.sha256)
    before = path.stat()
    s.put(b"x" * 100, "original")
    after = path.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


@pytest.mark.parametrize(
    "bad", ["A" * 64, "a" * 63, "../" + "a" * 61, "", "g" * 64, "a" * 64 + "\n", "a" * 64 + "\r"]
)
def test_malformed_hash_is_refused_everywhere(tmp_path: Path, bad: str) -> None:
    s = BlobStore(tmp_path)
    for call in (s.get, s.exists, s.content_path, lambda h: s.tombstone(h, "r")):
        with pytest.raises(ValueError):
            call(bad)


# --- missing and corrupt ----------------------------------------------------------


def test_get_missing_raises_blob_missing(tmp_path: Path) -> None:
    with pytest.raises(BlobMissing):
        BlobStore(tmp_path).get("1" * 64)


def test_missing_and_tombstoned_are_distinct_errors() -> None:
    assert not issubclass(BlobMissing, BlobTombstoned)
    assert not issubclass(BlobTombstoned, BlobMissing)


def test_flipped_byte_is_blob_corrupt(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(os.urandom(4096), "original")
    path = s.content_path(ref.sha256)
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(BlobCorrupt):
        s.get(ref.sha256)


def test_wrong_content_under_a_hash_is_blob_corrupt(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    a, b = s.put(b"aaa", "original"), s.put(b"bbb", "original")
    s.content_path(a.sha256).write_bytes(s.content_path(b.sha256).read_bytes())
    with pytest.raises(BlobCorrupt):
        s.get(a.sha256)


def test_truncated_file_is_blob_corrupt(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(os.urandom(4096), "original")
    path = s.content_path(ref.sha256)
    path.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(BlobCorrupt):
        s.get(ref.sha256)


# --- tombstones -------------------------------------------------------------------


def test_tombstone_file_holds_reason_and_utc_time(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"secret", "original")
    s.tombstone(ref.sha256, "client deletion request")
    assert not s.content_path(ref.sha256).exists()
    info = json.loads(s.tombstone_path(ref.sha256).read_text())
    assert info["sha256"] == ref.sha256 and info["reason"] == "client deletion request"
    assert info["tombstoned_at"].endswith("Z")
    assert s.tombstone_info(ref.sha256) == info
    assert b"secret" not in s.tombstone_path(ref.sha256).read_bytes()


def test_put_after_tombstone_is_refused_and_keeps_the_tombstone(tmp_path: Path) -> None:
    # A deletion request must never be silently undone by a later write.
    s = BlobStore(tmp_path)
    ref = s.put(b"secret", "original")
    s.tombstone(ref.sha256, "client deletion request")
    with pytest.raises(BlobTombstoned):
        s.put(b"secret", "original")
    assert s.exists(ref.sha256) == "tombstoned"
    assert not s.content_path(ref.sha256).exists()


def test_tombstone_of_a_missing_blob_is_refused(tmp_path: Path) -> None:
    # A tombstone passes verification, so it must never paper over a lost blob.
    s = BlobStore(tmp_path)
    with pytest.raises(BlobMissing):
        s.tombstone("2" * 64, "cover-up")
    assert s.exists("2" * 64) == "missing"
    assert not list(tmp_path.rglob("*.tombstone"))


def test_tombstone_twice_keeps_the_first_record(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"secret", "original")
    s.tombstone(ref.sha256, "first")
    s.tombstone(ref.sha256, "second")
    assert s.tombstone_info(ref.sha256)["reason"] == "first"


def test_tombstone_finishes_a_half_done_deletion(tmp_path: Path) -> None:
    # Crash after the tombstone was written but before the content was removed.
    s = BlobStore(tmp_path)
    ref = s.put(b"secret", "original")
    content = s.content_path(ref.sha256).read_bytes()
    s.tombstone(ref.sha256, "deletion")
    s.content_path(ref.sha256).write_bytes(content)
    assert s.exists(ref.sha256) == "tombstoned"  # the tombstone wins
    with pytest.raises(BlobTombstoned):
        s.get(ref.sha256)
    s.tombstone(ref.sha256, "retry")
    assert not s.content_path(ref.sha256).exists()


@pytest.mark.parametrize("reason", ["", "   "])
def test_tombstone_needs_a_reason(tmp_path: Path, reason: str) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"x", "original")
    with pytest.raises(ValueError):
        s.tombstone(ref.sha256, reason)
    assert s.exists(ref.sha256) == "present"


def test_tombstone_info_of_untombstoned_blob_raises(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"x", "original")
    with pytest.raises(BlobMissing):
        s.tombstone_info(ref.sha256)


# --- size on disk -----------------------------------------------------------------


def test_size_on_disk_sums_stored_files(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    assert s.size_on_disk() == 0
    a = s.put(b"a" * 5000, "original")
    b = s.put(os.urandom(3000), "original")
    files = [s.content_path(a.sha256), s.content_path(b.sha256)]
    assert s.size_on_disk() == sum(p.stat().st_size for p in files)
    s.tombstone(a.sha256, "deletion")
    assert s.size_on_disk() == (
        s.content_path(b.sha256).stat().st_size + s.tombstone_path(a.sha256).stat().st_size
    )


# --- atomic writes and concurrency ------------------------------------------------


def _leftovers(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and p.suffix not in (".zst", ".tombstone")]


def test_failed_write_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = BlobStore(tmp_path)

    def boom(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("mailroom_record.blobs.os.replace", boom)
    with pytest.raises(OSError):
        s.put(b"payload", "original")
    assert s.exists(sha256_hex(b"payload")) == "missing"
    assert _leftovers(tmp_path) == []


def test_threads_putting_the_same_bytes(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    data = os.urandom(256 * 1024)
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(5):
                s.put(data, "original")
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(list(tmp_path.rglob("*.zst"))) == 1
    assert _leftovers(tmp_path) == []
    assert s.get(sha256_hex(data)) == data


def test_processes_putting_the_same_bytes(tmp_path: Path) -> None:
    data = os.urandom(256 * 1024)
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(4) as pool:
        shas = pool.starmap(put_in_child, [(str(tmp_path), data, "original", 5)] * 4)
    assert set(shas) == {sha256_hex(data)}
    assert len(list(tmp_path.rglob("*.zst"))) == 1
    assert _leftovers(tmp_path) == []
    assert BlobStore(tmp_path).get(sha256_hex(data)) == data


def test_tombstone_after_a_stale_check_keeps_the_first_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two callers both saw "present" before either wrote: the first record must win.
    s = BlobStore(tmp_path)
    ref = s.put(b"secret", "original")
    s.tombstone(ref.sha256, "first")
    monkeypatch.setattr(s, "exists", lambda sha: "present")
    s.tombstone(ref.sha256, "second")
    assert s.tombstone_info(ref.sha256)["reason"] == "first"
    assert not list(tmp_path.rglob("*.tmp"))


def test_concurrent_tombstones_never_replace_the_record(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"secret", "original")
    n = 16
    barrier = threading.Barrier(n)
    seen: list[str] = []
    errors: list[BaseException] = []

    def go(i: int) -> None:
        try:
            barrier.wait()
            s.tombstone(ref.sha256, f"reason-{i}")
            seen.append(s.tombstone_info(ref.sha256)["reason"])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    final = s.tombstone_info(ref.sha256)["reason"]
    assert set(seen) == {final}  # every caller saw the one record that stayed
    assert s.exists(ref.sha256) == "tombstoned"
    assert not list(tmp_path.rglob("*.tmp"))


def test_blob_and_tombstone_files_are_owner_only(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    keep, gone = s.put(b"keep", "original"), s.put(b"gone", "original")
    s.tombstone(gone.sha256, "deletion request")
    assert s.content_path(keep.sha256).stat().st_mode & 0o777 == 0o600
    assert s.tombstone_path(gone.sha256).stat().st_mode & 0o777 == 0o600


def test_exists_sees_a_tombstone_written_between_its_two_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: exists() checked the tombstone, then the content. A tombstone written in
    # between (content already unlinked) made it answer "missing", so tombstone() raised.
    s = BlobStore(tmp_path)
    ref = s.put(b"secret", "original")
    real = s.content_path
    armed = [True]

    def racing(sha: str) -> Path:
        if armed[0]:
            armed[0] = False
            BlobStore(tmp_path).tombstone(sha, "other caller")  # lands between the checks
        return real(sha)

    monkeypatch.setattr(s, "content_path", racing)
    assert s.exists(ref.sha256) == "tombstoned"
