"""Content-addressed blob store with tombstones (ADR-0003)."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
from pathlib import Path

import pytest
import zstandard
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


# --- fix pass 2: durability, races and damage the bots found (PR #2) ---------------------

import hashlib  # noqa: E402

from mailroom_record import blobs as blobs_module  # noqa: E402
from record_testkit import tmp_files  # noqa: E402


def _record_dir_fsyncs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []
    real = blobs_module._fsync_dir

    def rec(path: Path) -> None:
        calls.append(Path(path))
        real(path)

    monkeypatch.setattr(blobs_module, "_fsync_dir", rec)
    return calls


def test_put_hashes_and_stores_one_snapshot_of_mutable_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Another thread changes a bytearray after put() hashed it and before it compressed it.
    s = BlobStore(tmp_path)
    data = bytearray(b"original bytes " * 1000)
    real_sha = hashlib.sha256

    def sha_then_mutate(view: object) -> object:
        h = real_sha(view)  # type: ignore[arg-type]
        data[:8] = b"MUTATED!"
        return h

    monkeypatch.setattr(blobs_module.hashlib, "sha256", sha_then_mutate)
    ref = s.put(data, "original")
    monkeypatch.undo()
    assert s.get(ref.sha256) == b"original bytes " * 1000  # hash and bytes agree


def test_every_directory_up_to_the_root_is_synced_even_when_it_existed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A writer that crashed after mkdir and before its parent fsync leaves an entry that is
    # visible but not durable; the next writer must not take "exists" as "durable".
    s = BlobStore(tmp_path)
    data = b"shard test"
    shard = s.content_path(hashlib.sha256(data).hexdigest()).parent
    shard.mkdir(parents=True)  # created, never synced (the crashed writer)
    calls = _record_dir_fsyncs(monkeypatch)
    s.put(data, "original")
    assert {shard, shard.parent, s.blob_root, tmp_path} <= set(calls)


def test_put_of_present_content_syncs_its_directory_before_returning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The first put renamed the file and then failed its directory fsync: the retry sees the
    # content "present" and must still make the rename durable before an event names it.
    s = BlobStore(tmp_path)
    ref = s.put(b"retry me", "original")
    calls = _record_dir_fsyncs(monkeypatch)
    assert s.put(b"retry me", "original") == ref
    assert s.content_path(ref.sha256).parent in calls


def test_a_put_racing_a_deletion_never_puts_the_content_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Writer B checks "missing" and compresses. Meanwhile writer A stores the same bytes and
    # a deletion tombstones them. B then publishes, and crashes right after its rename. The
    # deleted content must not be on disk afterwards.
    s = BlobStore(tmp_path)
    data = b"private document text " * 200
    sha = hashlib.sha256(data).hexdigest()
    real_fsync, real_replace = os.fsync, os.replace
    fired: list[bool] = []

    def crash_after_rename(src: object, dst: object) -> None:
        real_replace(src, dst)  # type: ignore[arg-type]
        raise OSError("crash right after the rename")

    def fsync_hook(fd: int) -> None:
        real_fsync(fd)
        if not fired and stat.S_ISREG(os.fstat(fd).st_mode):  # B's temp file, not a directory
            fired.append(True)
            other = BlobStore(tmp_path)  # writer A and the deletion, while B is mid-write
            other.put(data, "original")
            other.tombstone(sha, "erasure request")
            monkeypatch.setattr(os, "replace", crash_after_rename)

    monkeypatch.setattr(os, "fsync", fsync_hook)
    with pytest.raises(BlobTombstoned):
        s.put(data, "original")
    monkeypatch.undo()
    assert fired
    assert not s.content_path(sha).exists() and s.exists(sha) == "tombstoned"
    assert tmp_files(tmp_path) == []


def test_a_retried_tombstone_syncs_even_when_the_content_is_already_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"to delete", "original")
    s.tombstone(ref.sha256, "erasure request")  # suppose its last directory fsync failed
    calls = _record_dir_fsyncs(monkeypatch)
    s.tombstone(ref.sha256, "erasure request")
    assert s.content_path(ref.sha256).parent in calls


def test_an_existing_tombstone_is_synced_before_the_content_is_unlinked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The first caller linked the record and stopped before syncing and unlinking.
    s = BlobStore(tmp_path)
    ref = s.put(b"half deleted", "original")
    record = {"sha256": ref.sha256, "reason": "erasure request", "tombstoned_at": "2026-11-18T12:00:00Z"}
    s.tombstone_path(ref.sha256).write_text(json.dumps(record))
    shard = s.content_path(ref.sha256).parent
    order: list[tuple[str, Path]] = []
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == s.content_path(ref.sha256):
            order.append(("unlink", self))
        real_unlink(self, missing_ok=missing_ok)

    def fsync_dir(path: Path) -> None:
        order.append(("fsync", Path(path)))
        os_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(os_fd)
        finally:
            os.close(os_fd)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(blobs_module, "_fsync_dir", fsync_dir)
    s.tombstone(ref.sha256, "erasure request")
    unlinked = order.index(("unlink", s.content_path(ref.sha256)))
    assert ("fsync", shard) in order[:unlinked]  # the shard itself, not only its ancestors
    assert order[-1] == ("fsync", shard)
    assert json.loads(s.tombstone_path(ref.sha256).read_text()) == record  # the first record stays


@pytest.mark.parametrize("damage", ["empty", "truncated", "other_hash", "not_json"])
@pytest.mark.parametrize("content_left", [False, True])
def test_an_unreadable_tombstone_is_rewritten_and_a_readable_one_kept(
    tmp_path: Path, damage: str, content_left: bool
) -> None:
    from mailroom_record.verify import verify_run

    from mailroom_record.log import EventLog
    from mailroom_record.models import Run
    from record_testkit import run_fields

    s = BlobStore(tmp_path)
    log = EventLog(tmp_path, s)
    log.create_run(Run(**run_fields()))
    ref = s.put(b"client document", "original")
    with log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="n", kind="output", inputs=[ref])
    s.tombstone(ref.sha256, "erasure request")
    tomb = s.tombstone_path(ref.sha256)
    good = tomb.read_bytes()
    tomb.write_bytes({"empty": b"", "truncated": good[:10], "not_json": b"\xff\x00",
                      "other_hash": good.replace(ref.sha256.encode(), b"c" * 64)}[damage])
    if content_left:
        s.content_path(ref.sha256).write_bytes(b"left behind")
    found = [p.kind for p in verify_run(log, s, "r1").problems]
    assert found == (["tombstone_with_content"] if content_left else ["corrupt_tombstone"])

    s.tombstone(ref.sha256, "erasure request (repair)")  # the supported repair
    info = s.tombstone_info(ref.sha256)
    assert info["sha256"] == ref.sha256 and info["reason"] == "erasure request (repair)"
    assert not s.content_path(ref.sha256).exists() and tmp_files(tmp_path) == []
    report = verify_run(log, s, "r1")
    assert report.ok and report.tombstoned_blobs == 1
    s.tombstone(ref.sha256, "a later request")  # a readable record is never replaced
    assert s.tombstone_info(ref.sha256)["reason"] == "erasure request (repair)"


@pytest.mark.parametrize(
    "data", [b"", random_bytes := os.urandom(5 * 1024 * 1024)], ids=["empty", "5MiB_incompressible"]
)
@pytest.mark.parametrize("cut", [1, 2, 3, "all", "trailing", "second_frame", "skippable_frame"])
def test_a_blob_file_that_is_not_one_whole_frame_is_corrupt(tmp_path: Path, data: bytes, cut: object) -> None:
    # A frame cut inside its last (empty) block still decodes to every byte, so the digest
    # matches; only a frame check sees it. Verify reports the same file.
    from mailroom_record.log import EventLog
    from mailroom_record.models import Run
    from mailroom_record.verify import verify_run
    from record_testkit import run_fields

    s = BlobStore(tmp_path)
    log = EventLog(tmp_path, s)
    log.create_run(Run(**run_fields()))
    ref = s.put(data, "original")
    with log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="n", kind="output", inputs=[ref])
    assert s.get(ref.sha256) == data and verify_run(log, s, "r1").ok
    p = s.content_path(ref.sha256)
    whole = p.read_bytes()
    if cut == "all":
        p.write_bytes(b"")
    elif cut == "trailing":
        p.write_bytes(whole + b"\x00")
    elif cut == "second_frame":  # a decoder stops after the first frame: the digest still matches
        p.write_bytes(whole + zstandard.ZstdCompressor().compress(b"more"))
    elif cut == "skippable_frame":  # skippable frame: magic 0x184D2A50, a length, that many bytes
        p.write_bytes(whole + (0x184D2A50).to_bytes(4, "little") + (4).to_bytes(4, "little") + b"meta")
    else:
        p.write_bytes(whole[: -int(cut)])  # type: ignore[call-overload]
    with pytest.raises(BlobCorrupt):
        s.get(ref.sha256)
    assert [x.kind for x in verify_run(log, s, "r1").problems] == ["corrupt_blob"]


@pytest.mark.parametrize("data", [b"", b"synthetic checksummed content " * 1000], ids=["empty", "30KB"])
def test_a_frame_with_a_content_checksum_is_whole_only_with_its_checksum(tmp_path: Path, data: bytes) -> None:
    # The store writes frames without a checksum, but a frame that has one (flag bit 2) is
    # whole only with its four checksum bytes: accepted with them, corrupt without any.
    from mailroom_record.log import EventLog
    from mailroom_record.models import Run
    from mailroom_record.verify import verify_run
    from record_testkit import run_fields

    s = BlobStore(tmp_path)
    log = EventLog(tmp_path, s)
    log.create_run(Run(**run_fields()))
    ref = s.put(data, "original")
    with log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="n", kind="output", inputs=[ref])
    framed = zstandard.ZstdCompressor(write_checksum=True).compress(data)
    assert (framed[4] >> 2) & 1  # the checksum flag is set
    p = s.content_path(ref.sha256)
    p.write_bytes(framed)
    assert s.get(ref.sha256) == data and verify_run(log, s, "r1").ok
    for cut in (1, 4):
        p.write_bytes(framed[:-cut])
        with pytest.raises(BlobCorrupt):
            s.get(ref.sha256)
        assert [x.kind for x in verify_run(log, s, "r1").problems] == ["corrupt_blob"]


def test_get_never_returns_content_whose_deletion_was_recorded_while_it_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"read during deletion", "original")
    real_exists = BlobStore.exists
    done: list[bool] = []

    def exists_then_tombstone_record(self: BlobStore, sha256: str) -> str:
        state = real_exists(self, sha256)
        if not done:  # the deletion's record lands right after get() looked
            done.append(True)
            record = {"sha256": sha256, "reason": "erasure request", "tombstoned_at": "2026-11-18T12:00:00Z"}
            self.tombstone_path(sha256).write_text(json.dumps(record))
        return state

    monkeypatch.setattr(BlobStore, "exists", exists_then_tombstone_record)
    with pytest.raises(BlobTombstoned):
        s.get(ref.sha256)


# --- fix pass 3: tombstone damage and durability (PR #2 re-review) ----------------------


def _record_body(sha: str, reason: str = "erasure request") -> str:
    return json.dumps({"sha256": sha, "reason": reason, "tombstoned_at": "2026-11-18T12:00:00Z"})


@pytest.mark.parametrize("target", ["dangling", "elsewhere"])
def test_a_symlink_at_the_tombstone_path_is_replaced_by_a_real_record(tmp_path: Path, target: str) -> None:
    # A symlink is never a record tombstone() writes. A dangling one used to make tombstone()
    # unlink the content with no record kept, so the deletion was not recorded at all.
    s = BlobStore(tmp_path)
    ref = s.put(b"linked away", "original")
    tomb = s.tombstone_path(ref.sha256)
    elsewhere = tmp_path / "elsewhere.json"
    if target == "elsewhere":
        elsewhere.write_text(_record_body(ref.sha256, "planted"))
    tomb.symlink_to(elsewhere)
    s.tombstone(ref.sha256, "erasure request")
    assert not tomb.is_symlink() and tomb.is_file()
    info = s.tombstone_info(ref.sha256)
    assert info["sha256"] == ref.sha256 and info["reason"] == "erasure request"
    assert s.exists(ref.sha256) == "tombstoned" and not s.content_path(ref.sha256).exists()
    assert tmp_files(tmp_path) == []


def test_content_is_kept_when_the_record_is_not_there_after_writing_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-validate before unlinking: if the exclusive write lost to something that is not a
    # record, the content must stay and the call must fail.
    s = BlobStore(tmp_path)
    ref = s.put(b"keep me", "original")
    monkeypatch.setattr(blobs_module, "write_exclusive", lambda final, body, root=None: False)
    with pytest.raises(OSError, match="not a readable tombstone record"):
        s.tombstone(ref.sha256, "erasure request")
    assert s.content_path(ref.sha256).exists() and s.get(ref.sha256) == b"keep me"


def test_a_transient_read_error_never_replaces_a_valid_tombstone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"deleted once", "original")
    s.tombstone(ref.sha256, "first request")
    tomb = s.tombstone_path(ref.sha256)
    first = tomb.read_bytes()
    s.content_path(ref.sha256).write_bytes(b"restored by a backup")  # a retry has work to do
    real_read_bytes, real_open = Path.read_bytes, os.open

    def read_bytes(self: Path) -> bytes:
        if self == tomb:
            raise PermissionError(13, "transient", str(self))
        return real_read_bytes(self)

    def os_open(path: object, flags: int, *args: object, **kw: object) -> int:
        if Path(path) == tomb:  # type: ignore[arg-type]
            raise PermissionError(13, "transient", str(path))
        return real_open(path, flags, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(os, "open", os_open)
    with pytest.raises(PermissionError):
        s.tombstone(ref.sha256, "second request")
    monkeypatch.undo()
    assert tomb.read_bytes() == first  # never overwritten
    assert s.content_path(ref.sha256).exists()  # and nothing unlinked on an unknown state
    s.tombstone(ref.sha256, "second request")  # once the error clears, the retry finishes
    assert tomb.read_bytes() == first and not s.content_path(ref.sha256).exists()


def test_creating_the_store_root_syncs_its_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = BlobStore(tmp_path / "store")  # does not exist yet: put creates it
    calls = _record_dir_fsyncs(monkeypatch)
    ref = s.put(b"first blob", "original")
    shard = s.content_path(ref.sha256).parent
    assert tmp_path in calls  # the root's own entry
    assert {shard, shard.parent, s.blob_root, s.root} <= set(calls)


def test_tombstone_sweeps_temp_copies_of_the_content(tmp_path: Path) -> None:
    # A put that crashed before its rename leaves a temp copy of the private content.
    s = BlobStore(tmp_path)
    ref = s.put(b"private content", "original")
    shard = s.content_path(ref.sha256).parent
    stray = shard / f".{ref.sha256}.zst.crashed1.tmp"
    stray.write_bytes(b"compressed private content")
    other = shard / f".{'e' * 64}.zst.crashed2.tmp"  # another hash's: not ours to remove
    other.write_bytes(b"someone else")
    s.tombstone(ref.sha256, "erasure request")
    assert not stray.exists() and other.exists()


def test_an_empty_directory_at_the_tombstone_path_is_repaired(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"dir in the way", "original")
    s.tombstone_path(ref.sha256).mkdir()
    s.tombstone(ref.sha256, "erasure request")  # the repair verify advises
    assert s.tombstone_info(ref.sha256)["reason"] == "erasure request"
    assert not s.content_path(ref.sha256).exists() and s.exists(ref.sha256) == "tombstoned"


def test_a_non_empty_directory_at_the_tombstone_path_is_refused_and_named(tmp_path: Path) -> None:
    s = BlobStore(tmp_path)
    ref = s.put(b"dir in the way", "original")
    tomb = s.tombstone_path(ref.sha256)
    tomb.mkdir()
    (tomb / "something").write_bytes(b"x")
    with pytest.raises(IsADirectoryError, match="remove it, then call tombstone again"):
        s.tombstone(ref.sha256, "erasure request")
    assert s.content_path(ref.sha256).exists() and (tomb / "something").exists()  # nothing lost
