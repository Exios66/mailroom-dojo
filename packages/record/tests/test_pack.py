"""Task 11: pack, unpack and prune, on real on-disk stores, with corrupt and hostile archives."""

from __future__ import annotations

import io
import json
import os
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import zstandard
from hypothesis import given
from hypothesis import strategies as st
from record_testkit import (
    SENTINEL,
    Faults,
    blob_bytes,
    build_sealed_store,
    flip_byte,
    run_cli,
    run_fields,
)

from mailroom_record.blobs import BlobStore
from mailroom_record.log import EventLog, RunPruned
from mailroom_record.models import Run, Seal
from mailroom_record.pack import (
    PackCorrupt,
    PruneRefused,
    RunNotSealed,
    StoreBusy,
    UnpackConflict,
    pack_run,
    prune_run,
    unpack_run,
)
from mailroom_record.seal import seal_run
from mailroom_record.verify import verify_run


def open_store(root: Path) -> tuple[EventLog, BlobStore]:
    blobs = BlobStore(root)
    return EventLog(root, blobs), blobs


@pytest.fixture
def sealed_store(tmp_path: Path) -> SimpleNamespace:
    return build_sealed_store(tmp_path / "store")


def build_two_run_sealed_store(root: Path) -> SimpleNamespace:
    """Runs r1 and r2 under one root, both sealed. Both name the ``shared`` original blob;
    each also names blobs of its own (``only`` maps run -> its own shas)."""
    log, blobs = open_store(root)
    shared = blobs.put(blob_bytes("shared original"), "original")
    only: dict[str, list[str]] = {}
    for run_id in ("r1", "r2"):
        log.create_run(Run(**run_fields(run_id=run_id, document_ids=["d1", "d2"])))
        own = [blobs.put(blob_bytes(f"{run_id} {i}"), "extraction") for i in range(2)]
        only[run_id] = [r.sha256 for r in own]
        with log.open_writer(run_id, "w0") as w:
            w.append(doc_id="d1", node="intake", kind="handoff", outputs=[shared])
            w.append(doc_id="d1", node="extract", kind="output", inputs=[shared], outputs=[own[0]])
            w.append(doc_id="d2", node="extract", kind="output", outputs=[own[1]])
        seal_run(log, run_id)
    return SimpleNamespace(root=root, log=log, blobs=blobs, parts=(log, blobs), shared=shared.sha256, only=only)


@pytest.fixture
def two_run_sealed_store(tmp_path: Path) -> SimpleNamespace:
    return build_two_run_sealed_store(tmp_path / "store")


def run_files(root: Path, run_id: str) -> dict[str, bytes]:
    base = root / "runs" / run_id
    keep = ("run.json", "segments/", "seals/", "columns/")
    return {
        str(p.relative_to(base)): p.read_bytes()
        for p in sorted(base.rglob("*"))
        if p.is_file() and str(p.relative_to(base)).startswith(keep) and not p.name.startswith(".")
    }


# --- the plan's tests ---------------------------------------------------------------------


def test_pack_unpack_roundtrip_verifies(sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    manifest = pack_run(sealed_store.root, "r1", tmp_path / "r1.tar.zst")
    rid = unpack_run(tmp_path / "r1.tar.zst", tmp_path / "other")
    assert rid == "r1"
    assert verify_run(*open_store(tmp_path / "other"), rid).ok
    assert run_files(tmp_path / "other", "r1") == run_files(sealed_store.root, "r1")
    assert manifest.last_seal_hash == sealed_store.seals[-1].hash
    assert manifest.run_id == "r1" and manifest.path == tmp_path / "r1.tar.zst"


def test_unsealed_run_cannot_be_packed(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    with pytest.raises(RunNotSealed):
        pack_run(store_with_events.root, "r1", tmp_path / "x.tar.zst")
    assert not (tmp_path / "x.tar.zst").exists()


def test_prune_keeps_blobs_shared_with_another_run(two_run_sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    s = two_run_sealed_store
    pack_run(s.root, "r1", tmp_path / "r1.tar.zst")
    report = prune_run(s.root, "r1")
    assert verify_run(*s.parts, "r2").ok
    assert s.blobs.exists(s.shared) == "present"
    assert {s.blobs.exists(h) for h in s.only["r1"]} == {"missing"}
    assert sorted(report.blobs_removed) == sorted(s.only["r1"]) and report.blobs_kept == 1


def test_corrupt_archive_is_refused(sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    p = tmp_path / "r1.tar.zst"
    pack_run(sealed_store.root, "r1", p)
    flip_byte(p, p.stat().st_size // 2)
    with pytest.raises(PackCorrupt):
        unpack_run(p, tmp_path / "other")
    assert not (tmp_path / "other" / "runs" / "r1").exists()


# --- packing ------------------------------------------------------------------------------


def test_events_after_the_latest_seal_refuse_the_pack(sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    with sealed_store.log.open_writer("r1", "w9") as w:
        w.append(doc_id="d1", node="late", kind="handoff")
    with pytest.raises(RunNotSealed, match="not covered"):
        pack_run(sealed_store.root, "r1", tmp_path / "x.tar.zst")
    seal_run(sealed_store.log, "r1")
    pack_run(sealed_store.root, "r1", tmp_path / "x.tar.zst")


def test_the_manifest_lists_every_member_and_no_content(sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    p = tmp_path / "r1.tar.zst"
    manifest = pack_run(sealed_store.root, "r1", p)
    names, raw = _members(p)
    assert names[-1] == "manifest.json" and manifest.include_blobs
    stored = json.loads(raw["manifest.json"])
    assert sorted(stored["members"]) == sorted(names[:-1]) == sorted(manifest.members)
    assert SENTINEL.encode() not in raw["manifest.json"]
    blob_members = [n for n in names if n.startswith("blobs/")]
    assert len(blob_members) == len(set(sealed_store.originals.values())) + len(sealed_store.outputs)
    # positive control: a blob member does hold the content, compressed
    one = raw[blob_members[0]]
    assert SENTINEL.encode() in zstandard.ZstdDecompressor().decompress(one)


def test_pack_without_blobs(sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    p = tmp_path / "r1.tar.zst"
    pack_run(sealed_store.root, "r1", p, include_blobs=False)
    names, _ = _members(p)
    assert not [n for n in names if n.startswith("blobs/")]
    with pytest.raises(PackCorrupt, match="missing_blob"):  # nowhere to find the blobs
        unpack_run(p, tmp_path / "other")
    rid = unpack_run(p, _store_with_blobs_of(sealed_store, tmp_path / "with-blobs"))
    assert verify_run(*open_store(tmp_path / "with-blobs"), rid).ok


def _store_with_blobs_of(store: SimpleNamespace, root: Path) -> Path:
    import shutil

    shutil.copytree(store.root / "blobs", root / "blobs")
    return root


def test_an_existing_output_is_never_overwritten(sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    p = tmp_path / "r1.tar.zst"
    p.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        pack_run(sealed_store.root, "r1", p)
    assert p.read_bytes() == b"keep me"


@pytest.mark.parametrize(("call", "action"), [("write", "ENOSPC"), ("write", "short+ENOSPC"), ("fsync", "EIO"), ("link", "EIO")])
def test_a_failed_write_leaves_no_archive(
    sealed_store: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str, action: str
) -> None:
    out_dir = tmp_path / "packs"
    out_dir.mkdir()
    faults = Faults(monkeypatch, out_dir)
    faults.fail(call, 1, action)
    with pytest.raises(OSError):
        pack_run(sealed_store.root, "r1", out_dir / "r1.tar.zst")
    monkeypatch.undo()
    assert faults.fired and list(out_dir.iterdir()) == []


# --- unpacking ----------------------------------------------------------------------------


def test_any_flipped_bit_is_refused_or_harmless(sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    p = tmp_path / "r1.tar.zst"
    pack_run(sealed_store.root, "r1", p)
    data = p.read_bytes()
    want = run_files(sealed_store.root, "r1")
    counter = iter(range(10**6))

    @given(st.integers(min_value=0, max_value=len(data) - 1), st.integers(min_value=0, max_value=7))
    def check(where: int, bit: int) -> None:
        case = tmp_path / f"case-{next(counter)}"
        case.mkdir()
        q = case / "r1.tar.zst"
        q.write_bytes(data)
        flip_byte(q, where, 1 << bit)
        try:
            rid = unpack_run(q, case / "other")
        except PackCorrupt:
            assert not (case / "other" / "runs" / "r1").exists()
            return
        # a flip the format cannot see (it changes no stored byte) must restore the run exactly
        assert verify_run(*open_store(case / "other"), rid).ok
        assert run_files(case / "other", "r1") == want

    check()


def test_a_truncated_archive_is_refused(sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    p = tmp_path / "r1.tar.zst"
    pack_run(sealed_store.root, "r1", p)
    data = p.read_bytes()
    for cut in (len(data) - 1, len(data) // 2, 10):
        p.write_bytes(data[:cut])
        with pytest.raises(PackCorrupt):
            unpack_run(p, tmp_path / f"other-{cut}")


def _members(archive: Path) -> tuple[list[str], dict[str, bytes]]:
    with archive.open("rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as z, tarfile.open(
        fileobj=z, mode="r|"
    ) as tar:
        names, raw = [], {}
        for m in tar:
            names.append(m.name)
            member_file = tar.extractfile(m)
            raw[m.name] = member_file.read() if member_file else b""
    return names, raw


def _forge(path: Path, members: list[tuple[str, bytes, bytes]]) -> Path:
    """A ``.tar.zst`` holding ``(name, type, data)`` members (type ``tarfile.REGTYPE`` etc.)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                info.linkname = data.decode()
                tar.addfile(info)
    path.write_bytes(zstandard.ZstdCompressor(write_checksum=True).compress(buf.getvalue()))
    return path


@pytest.mark.parametrize(
    "member",
    [
        ("../escape.txt", tarfile.REGTYPE, b"x"),
        ("/abs/escape.txt", tarfile.REGTYPE, b"x"),
        ("runs/r1/../../escape.txt", tarfile.REGTYPE, b"x"),
        ("runs/r1/segments/link.jsonl", tarfile.SYMTYPE, b"/etc/passwd"),
        ("runs/r1/segments/hard.jsonl", tarfile.LNKTYPE, b"runs/r1/run.json"),
        ("runs/r1/notes.txt", tarfile.REGTYPE, b"x"),
        ("blobs/aa/bb/" + "c" * 64 + ".zst", tarfile.REGTYPE, b"not zstd"),
    ],
    ids=["dotdot", "absolute", "inner-dotdot", "symlink", "hardlink", "unknown-file", "bad-blob"],
)
def test_hostile_members_are_refused(tmp_path: Path, member: tuple[str, int, bytes]) -> None:
    root = tmp_path / "root"
    p = _forge(tmp_path / "evil.tar.zst", [member])
    with pytest.raises(PackCorrupt):
        unpack_run(p, root)
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "abs").exists()
    assert not (root / "runs").exists()


def test_a_member_not_in_the_manifest_is_refused(sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    p = tmp_path / "r1.tar.zst"
    pack_run(sealed_store.root, "r1", p)
    names, raw = _members(p)
    forged = [(n, tarfile.REGTYPE, raw[n]) for n in names[:-1]]
    forged.append(("runs/r1/seals/9.json", tarfile.REGTYPE, b"{}"))
    forged.append(("manifest.json", tarfile.REGTYPE, raw["manifest.json"]))
    with pytest.raises(PackCorrupt, match="manifest"):
        unpack_run(_forge(tmp_path / "extra.tar.zst", forged), tmp_path / "other")


def test_a_crash_mid_unpack_is_finished_by_the_next(
    sealed_store: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "r1.tar.zst"
    pack_run(sealed_store.root, "r1", p)
    root = tmp_path / "other"
    root.mkdir()
    faults = Faults(monkeypatch, root)
    n_blobs = len(set(sealed_store.originals.values())) + len(sealed_store.outputs)
    faults.fail("link", n_blobs + 2, "EIO")  # every blob and one run file are in place, the rest not
    with pytest.raises(OSError):
        unpack_run(p, root)
    monkeypatch.undo()
    assert faults.fired
    assert len(list((root / "runs" / "r1").rglob("*.jsonl"))) == 1 and not (root / "runs" / "r1" / "run.json").exists()
    assert unpack_run(p, root) == "r1"
    assert verify_run(*open_store(root), "r1").ok
    assert run_files(root, "r1") == run_files(sealed_store.root, "r1")
    assert not [q for q in root.iterdir() if q.name.startswith(".unpack")]


def test_unpack_over_a_different_run_is_refused(sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    p = tmp_path / "r1.tar.zst"
    pack_run(sealed_store.root, "r1", p)
    other = build_sealed_store(tmp_path / "other", kind="full_pipeline")  # another run named r1
    before = run_files(other.root, "r1")
    with pytest.raises(UnpackConflict):
        unpack_run(p, other.root)
    assert run_files(other.root, "r1") == before


def test_unpack_twice_is_a_no_op(sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    p = tmp_path / "r1.tar.zst"
    pack_run(sealed_store.root, "r1", p)
    assert unpack_run(p, tmp_path / "other") == unpack_run(p, tmp_path / "other") == "r1"
    assert verify_run(*open_store(tmp_path / "other"), "r1").ok


# --- pruning ------------------------------------------------------------------------------


def test_prune_needs_a_recorded_current_pack(two_run_sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    s = two_run_sealed_store
    with pytest.raises(PruneRefused, match="no pack"):
        prune_run(s.root, "r1")
    p = tmp_path / "r1.tar.zst"
    pack_run(s.root, "r1", p)
    with s.log.open_writer("r1", "w1") as w:
        w.append(doc_id="d1", node="late", kind="handoff")
    with pytest.raises(RunNotSealed):
        prune_run(s.root, "r1")
    seal_run(s.log, "r1")
    with pytest.raises(PruneRefused, match="seal"):  # the pack predates the latest seal
        prune_run(s.root, "r1")
    p.unlink()
    pack_run(s.root, "r1", p)
    p.write_bytes(p.read_bytes()[:-1])
    with pytest.raises(PruneRefused, match="archive"):  # the recorded archive changed
        prune_run(s.root, "r1")
    assert verify_run(*s.parts, "r1").ok  # every refusal left the run whole


def test_prune_removes_segments_and_seals_and_keeps_columns(
    two_run_sealed_store: SimpleNamespace, tmp_path: Path
) -> None:
    s = two_run_sealed_store
    pack_run(s.root, "r1", tmp_path / "r1.tar.zst")
    report = prune_run(s.root, "r1")
    run_dir = s.root / "runs" / "r1"
    assert not list(run_dir.glob("segments/*.jsonl")) and not list(run_dir.glob("seals/*.json"))
    assert list(run_dir.glob("columns/*.parquet")) and (run_dir / "run.json").is_file()
    assert report.segments_removed == 1 and report.seals_removed == 1 and report.columns_removed == 0
    again = prune_run(s.root, "r1")
    assert again.blobs_removed == [] and again.segments_removed == 0
    rid = unpack_run(tmp_path / "r1.tar.zst", s.root)  # the pack restores it in place
    assert verify_run(*s.parts, rid).ok and verify_run(*s.parts, "r2").ok


def test_prune_without_columns(two_run_sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    s = two_run_sealed_store
    pack_run(s.root, "r1", tmp_path / "r1.tar.zst")
    report = prune_run(s.root, "r1", keep_columns=False)
    assert report.columns_removed == 1 and not list((s.root / "runs" / "r1").glob("columns/*.parquet"))


def test_prune_of_a_pack_without_blobs_deletes_no_blob(
    two_run_sealed_store: SimpleNamespace, tmp_path: Path
) -> None:
    s = two_run_sealed_store
    pack_run(s.root, "r1", tmp_path / "r1.tar.zst", include_blobs=False)
    report = prune_run(s.root, "r1")
    assert report.blobs_removed == [] and {s.blobs.exists(h) for h in s.only["r1"]} == {"present"}


def test_a_crash_mid_prune_is_finished_by_the_next(
    two_run_sealed_store: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = two_run_sealed_store
    pack_run(s.root, "r1", tmp_path / "r1.tar.zst")
    real_unlink = Path.unlink
    calls = {"n": 0}

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.suffix == ".zst":
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError, match="injected"):
        prune_run(s.root, "r1")
    monkeypatch.undo()
    report = prune_run(s.root, "r1")
    assert {s.blobs.exists(h) for h in s.only["r1"]} == {"missing"}
    assert s.blobs.exists(s.shared) == "present" and report.segments_removed == 1
    assert verify_run(*s.parts, "r2").ok


def test_prune_waits_for_a_live_writer_and_sees_its_events(
    two_run_sealed_store: SimpleNamespace, tmp_path: Path
) -> None:
    """Review Focus: prune vs a concurrent append. A writer of r2 is open (shared lock) when
    prune of r1 starts; prune waits; the writer then names one of r1's own blobs and closes;
    prune must see that and keep the blob."""
    s = two_run_sealed_store
    pack_run(s.root, "r1", tmp_path / "r1.tar.zst")
    writer = s.log.open_writer("r2", "w-late")
    done: dict[str, object] = {}

    def prune() -> None:
        try:
            done["report"] = prune_run(s.root, "r1")
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            done["error"] = exc

    t = threading.Thread(target=prune)
    t.start()
    time.sleep(0.3)
    assert t.is_alive() and not done  # waiting for the maintenance lock
    keep = s.only["r1"][0]
    ref = {"sha256": keep, "role": "extraction", "size": len(s.blobs.get(keep))}
    writer.append(doc_id="d3", node="late", kind="output", outputs=[ref])
    writer.close()
    t.join(30)
    assert "error" not in done, done.get("error")
    assert s.blobs.exists(keep) == "present" and s.blobs.exists(s.only["r1"][1]) == "missing"
    seal_run(s.log, "r2")
    assert verify_run(*s.parts, "r2").ok


@pytest.mark.parametrize("include_blobs", [True, False], ids=["with-blobs", "without-blobs"])
def test_prune_cannot_run_while_an_unpack_places_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, include_blobs: bool
) -> None:
    """An unpack of r2 skips the shared blob the target already holds (r1 names it). A prune
    of r1 run between the blob step and r2's run files would see no other run naming it and
    delete it. The unpack holds the maintenance lock shared, so that prune gives up."""
    import mailroom_record.pack as pack_module

    origin = build_two_run_sealed_store(tmp_path / "origin")
    archive = tmp_path / "r2.tar.zst"
    pack_run(origin.root, "r2", archive, include_blobs=include_blobs)
    target = build_two_run_sealed_store(tmp_path / "target")
    for sha in target.only["r2"]:  # r2's own blobs, as a store holding only r1 would lack them
        target.blobs.content_path(sha).unlink()
    if not include_blobs:  # the archive lacks blobs: the target must hold r2's own
        for sha in origin.only["r2"]:
            target.blobs.put(origin.blobs.get(sha), "extraction")
    (tmp_path / "target" / "runs" / "r2").rename(tmp_path / "r2-gone")
    pack_run(target.root, "r1", tmp_path / "r1.tar.zst")

    real = pack_module._makedirs_durable
    raced: list[BaseException | object] = []

    def makedirs(path: Path, root: Path) -> None:
        if "runs" in Path(path).relative_to(root).parts and not raced:
            try:
                raced.append(prune_run(target.root, "r1", lock_timeout=0.2))
            except StoreBusy as exc:
                raced.append(exc)
        real(path, root)

    monkeypatch.setattr(pack_module, "_makedirs_durable", makedirs)
    assert unpack_run(archive, target.root) == "r2"
    monkeypatch.undo()
    assert len(raced) == 1 and isinstance(raced[0], StoreBusy), raced
    assert target.blobs.exists(target.shared) == "present"
    assert verify_run(*target.parts, "r2").ok
    prune_run(target.root, "r1")  # after the unpack, r2 names the shared blob: it is kept
    assert target.blobs.exists(target.shared) == "present" and verify_run(*target.parts, "r2").ok


def test_prune_gives_up_after_its_timeout(two_run_sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    s = two_run_sealed_store
    pack_run(s.root, "r1", tmp_path / "r1.tar.zst")
    with s.log.open_writer("r2", "w-hold"), pytest.raises(StoreBusy):
        prune_run(s.root, "r1", lock_timeout=0.2)
    assert verify_run(*s.parts, "r1").ok


def test_prune_keeps_a_blob_another_run_names_in_a_damaged_line(
    two_run_sealed_store: SimpleNamespace, tmp_path: Path
) -> None:
    s = two_run_sealed_store
    seg = next((s.root / "runs" / "r2" / "segments").glob("*.jsonl"))
    with seg.open("ab") as f:
        f.write(b'#garbled# {"outputs": [{"sha256": "' + s.only["r1"][0].encode() + b'"')  # a torn, corrupt tail
    pack_run(s.root, "r1", tmp_path / "r1.tar.zst")
    prune_run(s.root, "r1")
    assert s.blobs.exists(s.only["r1"][0]) == "present"


def test_cli_pack_unpack_prune(two_run_sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    s = two_run_sealed_store
    p = tmp_path / "r1.tar.zst"
    res = run_cli("prune", "r1", root=s.root)
    assert res.exit_code == 1 and "no pack" in res.stderr
    res = run_cli("pack", "r1", str(p), root=s.root)
    assert res.exit_code == 0 and "r1" in res.stdout, res.stderr
    res = run_cli("prune", "r1", "--json", root=s.root)
    assert res.exit_code == 0 and json.loads(res.stdout)["segments_removed"] == 1
    res = run_cli("unpack", str(p), root=tmp_path / "other")
    assert res.exit_code == 0 and "r1" in res.stdout
    flip_byte(p, p.stat().st_size // 2)
    res = run_cli("unpack", str(p), root=tmp_path / "third")
    assert res.exit_code == 1 and "PackCorrupt" in res.stderr and "Traceback" not in res.stderr
    for out in (res.stdout, res.stderr):
        assert SENTINEL not in out


def test_the_maintenance_lock_is_shared_by_writers(two_run_sealed_store: SimpleNamespace) -> None:
    s = two_run_sealed_store
    with s.log.open_writer("r1", "a"), s.log.open_writer("r2", "b"):  # two writers at once are fine
        assert (s.root / ".maintenance.lock").exists()
    assert os.path.getsize(s.root / ".maintenance.lock") == 0


# --- a pruned run takes no writes (adversarial round 2, findings 1 to 3) --------------------


def store_tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` (lock files and staging leftovers aside), by relative path."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.endswith(".lock") and not str(p.relative_to(root)).startswith(".unpack-")
    }


def _pack_and_prune(s: SimpleNamespace, archive: Path, *, keep_columns: bool = True) -> None:
    pack_run(s.root, "r1", archive)
    prune_run(s.root, "r1", keep_columns=keep_columns)


def test_a_writer_refuses_a_pruned_run(two_run_sealed_store: SimpleNamespace, tmp_path: Path) -> None:
    """A writer on a pruned run would start every chain again at seq 0, a fork once the
    archive is unpacked. It refuses with RunPruned before touching the segments."""
    s = two_run_sealed_store
    archive = tmp_path / "r1.tar.zst"
    _pack_and_prune(s, archive)
    before = store_tree(s.root)
    with pytest.raises(RunPruned, match="unpack") as info:
        s.log.open_writer("r1", "late")
    assert str(archive) in str(info.value)
    assert store_tree(s.root) == before
    with s.log.open_writer("r2", "late") as w:  # the lock was released, other runs still take events
        w.append(doc_id="d1", node="late", kind="handoff")
    unpack_run(archive, s.root)
    with s.log.open_writer("r1", "late") as w:  # whole again: the chain continues
        assert w.append(doc_id="d1", node="late", kind="handoff").seq == 2
    assert verify_run(*s.parts, "r1").ok


@pytest.mark.parametrize("keep_columns", [True, False], ids=["keep-columns", "drop-columns"])
def test_seal_refuses_a_pruned_run(two_run_sealed_store: SimpleNamespace, tmp_path: Path, keep_columns: bool) -> None:
    """Sealing a pruned run would write a new seal 0 over an empty log; the unpack would
    then conflict and verify would pass on an empty run. seal_run refuses, writes nothing,
    and the archive still restores the run."""
    s = two_run_sealed_store
    archive = tmp_path / "r1.tar.zst"
    _pack_and_prune(s, archive, keep_columns=keep_columns)
    before = store_tree(s.root)
    with pytest.raises(RunPruned, match="sealed"):
        seal_run(s.log, "r1")
    assert store_tree(s.root) == before
    res = run_cli("seal", "r1", root=s.root)
    assert res.exit_code == 1 and "RunPruned" in res.stderr and "Traceback" not in res.stderr
    assert store_tree(s.root) == before
    assert unpack_run(archive, s.root) == "r1"
    assert verify_run(*s.parts, "r1").ok
    assert seal_run(s.log, "r1").n == 0  # nothing new: the restored seal, unchanged


def test_a_prune_waits_for_a_live_seal(
    two_run_sealed_store: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seal_run holds the maintenance lock shared, so a prune cannot delete the seals it is
    building on: a prune started while a seal runs gives up after its timeout."""
    import mailroom_record.seal as seal_module

    s = two_run_sealed_store
    pack_run(s.root, "r1", tmp_path / "r1.tar.zst")
    real = seal_module._seal_unlocked
    seen: list[str] = []

    def contended(log: EventLog, run_id: str, sealed_at: str) -> Seal:
        with pytest.raises(StoreBusy):
            prune_run(s.root, "r1", lock_timeout=0.2)
        seen.append(run_id)
        return real(log, run_id, sealed_at)

    monkeypatch.setattr(seal_module, "_seal_unlocked", contended)
    seal_run(s.log, "r1")
    assert seen == ["r1"]
    assert verify_run(*s.parts, "r1").ok and (s.root / "runs" / "r1" / "seals" / "0.json").is_file()
