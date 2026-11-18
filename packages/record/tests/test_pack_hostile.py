"""Hostile archives against ``unpack_run`` (adversarial round 3, findings 1, 4 and 5).

Each test builds a synthetic archive the way an attacker would and checks that the
target store is refused and left unchanged, or that the cost of reading it stays bounded.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
import zstandard
from record_testkit import (
    archive_members,
    blob_member,
    build_sealed_store,
    flip_byte,
    forge_archive,
    peak_bytes,
    run_cli,
    store_tree,
    zero_bomb,
)

import mailroom_record.pack as pack_module
from mailroom_record.blobs import BlobStore
from mailroom_record.hashing import canonical_json
from mailroom_record.log import EventLog
from mailroom_record.pack import PackCorrupt, PackRefused, UnpackVerifyFailed, pack_run, unpack_run
from mailroom_record.verify import Problem, VerifyReport

MiB = 1 << 20


# --- finding 4: decompression bombs -------------------------------------------------------


def test_an_over_cap_member_is_refused_before_it_is_staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 64 MiB segment member against a 1 MiB cap: refused from its tar header, at once,
    without writing it to the staging store."""
    bomb = zero_bomb(tmp_path / "bomb.tar.zst", "runs/r1/segments/w0.jsonl", 64 * MiB)
    assert bomb.stat().st_size < 64 * 1024
    monkeypatch.setattr(pack_module, "MAX_RUN_FILE", 1 * MiB)
    root = tmp_path / "target"
    started = time.monotonic()
    with pytest.raises(PackCorrupt, match="larger than"):
        unpack_run(bomb, root)
    assert time.monotonic() - started < 2.0
    assert store_tree(root) == {} and not list(root.glob(".unpack-*"))


def test_reading_a_bomb_holds_a_bounded_amount_of_memory(tmp_path: Path) -> None:
    """A 64 MiB member under the default cap is read through: it is staged on disk in
    chunks, and no step holds more than a few MiB of decompressed bytes."""
    bomb = zero_bomb(tmp_path / "bomb.tar.zst", "runs/r1/segments/w0.jsonl", 64 * MiB)

    def unpack() -> None:
        with pytest.raises(PackCorrupt, match="no manifest"):
            unpack_run(bomb, tmp_path / "target")

    assert peak_bytes(unpack) < 16 * MiB


def test_pack_refuses_a_member_unpack_would_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The caps hold on both sides: pack never writes an archive unpack would refuse."""
    s = build_sealed_store(tmp_path / "store")
    monkeypatch.setattr(pack_module, "MAX_RUN_FILE", 64)
    with pytest.raises(PackRefused, match="larger than"):
        pack_run(s.root, "r1", tmp_path / "r1.tar.zst")
    assert not (tmp_path / "r1.tar.zst").exists()


# --- finding 1: blob members must be the run's own, and be what their names say ----------------


def _packed(tmp_path: Path) -> tuple[Path, list[tuple[str, bytes]]]:
    s = build_sealed_store(tmp_path / "src")
    archive = pack_run(s.root, "r1", tmp_path / "r1.tar.zst").path
    return archive, archive_members(archive)


def _target(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    """A target store that already holds a run of its own (r5, same synthetic originals)."""
    t = build_sealed_store(tmp_path / "target", run_id="r5")
    return t.root, store_tree(t.root)


def _with_manifest(members: list[tuple[str, bytes]], extra: list[tuple[str, bytes]], **changes: object) -> list[tuple[str, bytes]]:
    manifest = json.loads(members[-1][1])
    manifest.update(changes)
    return [*members[:-1], *extra, ("manifest.json", canonical_json(manifest))]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_an_unnamed_blob_member_is_refused(tmp_path: Path) -> None:
    """A blob the run does not name, even one whose content is what its name says, never
    reaches the target: it could preempt a later put of the same bytes."""
    _, members = _packed(tmp_path)
    future = b"bytes some later document will hold"
    extra = [(blob_member(_sha(future)), zstandard.ZstdCompressor().compress(future))]
    root, before = _target(tmp_path)
    forged = forge_archive(tmp_path / "x.tar.zst", _with_manifest(members, extra))
    with pytest.raises(PackCorrupt, match="does not name"):
        unpack_run(forged, root)
    assert store_tree(root) == before


def test_an_unnamed_blob_listed_in_the_manifest_is_refused(tmp_path: Path) -> None:
    """Listing the extra blob in the manifest's ``blobs`` does not help: the list must be the
    blobs the verified run names, no more."""
    _, members = _packed(tmp_path)
    future = b"bytes some later document will hold"
    sha = _sha(future)
    manifest = json.loads(members[-1][1])
    extra = [(blob_member(sha), zstandard.ZstdCompressor().compress(future))]
    root, before = _target(tmp_path)
    forged = forge_archive(
        tmp_path / "x.tar.zst", _with_manifest(members, extra, blobs=sorted([*manifest["blobs"], sha]))
    )
    with pytest.raises(PackCorrupt, match="is not the blobs"):
        unpack_run(forged, root)
    assert store_tree(root) == before


@pytest.mark.parametrize("named", [True, False], ids=["named-blob", "unnamed-blob"])
def test_a_blob_whose_content_is_not_its_name_is_refused(tmp_path: Path, named: bool) -> None:
    """The reviewer's poison: a .zst member whose bytes decompress to something else. It
    would make the target's blob corrupt for every later run that names it."""
    _, members = _packed(tmp_path)
    evil = zstandard.ZstdCompressor().compress(b"EVIL CONTENT")
    if named:
        victim = next(n for n, _ in members if n.endswith(".zst"))
        forged_members = [(n, evil if n == victim else d) for n, d in members]
    else:
        forged_members = _with_manifest(members, [(blob_member(_sha(b"a future document")), evil)])
    root, before = _target(tmp_path)
    with pytest.raises(PackCorrupt, match="hashes to"):
        unpack_run(forge_archive(tmp_path / "x.tar.zst", forged_members), root)
    assert store_tree(root) == before


@pytest.mark.parametrize(
    "record",
    [
        {"reason": "x", "tombstoned_at": "2026-01-01T00:00:00Z"},  # no hash
        {"sha256": "0" * 64, "reason": "x", "tombstoned_at": "2026-01-01T00:00:00Z"},  # another hash
        {"sha256": None, "reason": " ", "tombstoned_at": "2026-01-01T00:00:00Z"},  # no reason
        "not an object",
    ],
    ids=["no-hash", "other-hash", "blank-reason", "not-object"],
)
def test_a_forged_tombstone_is_refused(tmp_path: Path, record: object) -> None:
    """A tombstone member (here in place of the blob's content) must be the record
    BlobStore.tombstone writes for that hash."""
    _, members = _packed(tmp_path)
    victim = next(n for n, _ in members if n.endswith(".zst"))
    sha = victim.rsplit("/", 1)[1].split(".")[0]
    if isinstance(record, dict) and record.get("sha256") is None and "sha256" in record:
        record = {**record, "sha256": sha}
    body = json.dumps(record).encode()
    forged = [(blob_member(sha, "tombstone"), body) if n == victim else (n, d) for n, d in members]
    root, before = _target(tmp_path)
    with pytest.raises(PackCorrupt, match="not the tombstone record"):
        unpack_run(forge_archive(tmp_path / "x.tar.zst", forged), root)
    assert store_tree(root) == before


def test_a_tombstone_for_a_blob_the_run_does_not_name_is_refused(tmp_path: Path) -> None:
    """The reviewer's case B: a valid-looking tombstone for a future document's hash would
    make every later put of those bytes raise BlobTombstoned."""
    _, members = _packed(tmp_path)
    sha = _sha(b"another future doc")
    body = canonical_json({"sha256": sha, "reason": "x", "tombstoned_at": "2026-01-01T00:00:00Z"})
    root, before = _target(tmp_path)
    forged = forge_archive(tmp_path / "x.tar.zst", _with_manifest(members, [(blob_member(sha, "tombstone"), body)]))
    with pytest.raises(PackCorrupt, match="does not name"):
        unpack_run(forged, root)
    assert store_tree(root) == before


def test_a_blob_bomb_is_refused_at_its_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A small .zst member that decompresses past MAX_BLOB is refused while it is read."""
    _, members = _packed(tmp_path)
    victim = next(n for n, _ in members if n.endswith(".zst"))
    bomb = zstandard.ZstdCompressor(level=19).compress(bytes(8 << 20))
    monkeypatch.setattr(pack_module, "MAX_BLOB", 1 << 20)
    root, before = _target(tmp_path)
    forged = [(n, bomb if n == victim else d) for n, d in members]
    with pytest.raises(PackCorrupt, match="more than"):
        unpack_run(forge_archive(tmp_path / "x.tar.zst", forged), root)
    assert store_tree(root) == before


# --- finding 5: "it verifies" must be true of the target ---------------------------------------


def test_a_damaged_target_blob_is_refused_before_anything_is_placed(tmp_path: Path) -> None:
    """The reviewer's claim.py: the target already holds one of the archive's blobs, bit-rotted.
    Unpack used to skip it as present and print "it verifies" over a run that does not."""
    archive, _ = _packed(tmp_path)
    t = build_sealed_store(tmp_path / "target", run_id="r5")  # the same synthetic originals as r1
    rotten = sorted((t.root / "blobs").rglob("*.zst"))[0]
    flip_byte(rotten, rotten.stat().st_size // 2)
    before = store_tree(t.root)
    with pytest.raises(UnpackVerifyFailed, match="damaged") as info:
        unpack_run(archive, t.root)
    assert rotten.name.split(".")[0] in str(info.value)
    assert store_tree(t.root) == before
    res = run_cli("unpack", str(archive), root=t.root)
    assert res.exit_code == 1 and "UnpackVerifyFailed" in res.stderr and "it verifies" not in res.stdout
    assert store_tree(t.root) == before


def test_the_placed_run_is_verified_in_the_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fault injection: whatever the target makes of the placed run, a failing verify there is
    UnpackVerifyFailed naming the problems, and the CLI never says "it verifies"."""
    archive, _ = _packed(tmp_path)
    root = tmp_path / "target"
    real = pack_module._verify_contents
    calls: list[Path] = []

    def judged_in_target(log: EventLog, blobs: BlobStore, run_id: str) -> VerifyReport:
        calls.append(Path(log.root))
        report = real(log, blobs, run_id)
        injected = Problem("missing_blob", "d1", 0, "injected for the test")
        return replace(report, ok=False, problems=[*report.problems, injected])

    monkeypatch.setattr(pack_module, "_verify_contents", judged_in_target)
    with pytest.raises(UnpackVerifyFailed, match="missing_blob"):
        unpack_run(archive, root)
    assert calls == [root]  # the target itself was verified (the staging store by verify_run)
    res = run_cli("unpack", str(archive), root=root)
    assert res.exit_code == 1 and "it verifies" not in res.stdout
