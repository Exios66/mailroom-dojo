"""Pack, unpack and prune around a pruned run (adversarial round 3, findings 2, 7 and 8).

Synthetic stores only; each race is made deterministic by injecting the competing call at
the exact point the reviewer's script reached.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from record_testkit import build_two_run_sealed_store, run_cli, store_tree

import mailroom_record.pack as pack_module
from mailroom_record.log import RunPruned
from mailroom_record.pack import (
    PruneRefused,
    StoreBusy,
    pack_run,
    prune_run,
    unpack_run,
)
from mailroom_record.verify import verify_run


@pytest.fixture
def s(tmp_path: Path) -> SimpleNamespace:
    return build_two_run_sealed_store(tmp_path / "store")


def _record(s: SimpleNamespace, name: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((s.root / "runs" / "r1" / name).read_bytes())
    return loaded


# --- finding 2: a pack and a prune of one run never interleave -------------------------------


def test_a_prune_waits_for_a_pack_in_progress(s: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reviewer's packrace.py, made deterministic: archive A, then pack B starts; at the
    point B has written and checked its archive, a prune of the run tries to run. It used to
    prune from A while B then recorded itself in pack.json, leaving prune.json naming A and
    pack.json naming B: neither archive restored the run. Now pack holds the maintenance lock
    shared from its first check to pack.json, so the prune waits (StoreBusy here)."""
    a, b = tmp_path / "A.tar.zst", tmp_path / "B.tar.zst"
    pack_run(s.root, "r1", a)
    real = pack_module._read_archive
    tried: list[str] = []

    def readback_then_prune(archive: Path, sink: object) -> object:
        result = real(archive, sink)  # type: ignore[arg-type]
        if archive.name.startswith(".B.tar.zst"):
            try:
                prune_run(s.root, "r1", lock_timeout=0.2)
                tried.append("pruned")
            except StoreBusy:
                tried.append("busy")
        return result

    monkeypatch.setattr(pack_module, "_read_archive", readback_then_prune)
    pack_run(s.root, "r1", b, include_blobs=False)
    monkeypatch.undo()
    assert tried == ["busy"]
    assert not (s.root / "runs" / "r1" / "prune.json").exists()
    assert Path(str(_record(s, "pack.json")["archive"])).name == "B.tar.zst"

    prune_run(s.root, "r1")  # after the pack: from B, the recorded pack
    assert unpack_run(b, s.root) == "r1"
    assert verify_run(*s.parts, "r1").ok


@pytest.mark.parametrize("keep_columns", [True, False], ids=["keep-columns", "drop-columns"])
def test_packing_a_pruned_run_is_refused_as_pruned(s: SimpleNamespace, tmp_path: Path, keep_columns: bool) -> None:
    pack_run(s.root, "r1", tmp_path / "A.tar.zst")
    prune_run(s.root, "r1", keep_columns=keep_columns)
    before = store_tree(s.root)
    with pytest.raises(RunPruned, match="cannot be packed") as info:
        pack_run(s.root, "r1", tmp_path / "B.tar.zst")
    assert str(tmp_path / "A.tar.zst") in str(info.value) and "seal it first" not in str(info.value)
    assert not (tmp_path / "B.tar.zst").exists() and store_tree(s.root) == before
    res = run_cli("pack", "r1", str(tmp_path / "B.tar.zst"), root=s.root)
    assert res.exit_code == 1 and "RunPruned" in res.stderr


def test_unpack_accepts_the_archive_prune_json_records(s: SimpleNamespace, tmp_path: Path) -> None:
    """A store left by the old race: prune.json names A, pack.json names B. A is the
    archive the run was pruned from, so it restores the run."""
    a, b = tmp_path / "A.tar.zst", tmp_path / "B.tar.zst"
    pack_run(s.root, "r1", a)
    prune_run(s.root, "r1")
    a_record = (s.root / "runs" / "r1" / "pack.json").read_bytes()
    unpack_run(a, s.root)
    pack_run(s.root, "r1", b, include_blobs=False)  # pack.json now names B
    prune = s.root / "runs" / "r1" / "prune.json"
    prune.write_bytes(json.dumps({"state": "done", "archive_sha256": json.loads(a_record)["archive_sha256"]}).encode())
    assert unpack_run(a, s.root) == "r1"
    assert not prune.exists() and verify_run(*s.parts, "r1").ok


# --- finding 7: two unpacks of one pruned run ----------------------------------------------


def test_an_unpack_that_finds_prune_json_gone_finishes(s: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two unpacks of the same archive both hold the lock shared; the other one removes
    prune.json first. This one must not fail on the missing file."""
    a = tmp_path / "A.tar.zst"
    pack_run(s.root, "r1", a)
    prune_run(s.root, "r1")
    prune = s.root / "runs" / "r1" / "prune.json"
    real_exists = Path.exists

    def other_unpack_removes_it(self: Path, *args: object, **kwargs: object) -> bool:
        seen = real_exists(self, *args, **kwargs)  # type: ignore[arg-type]
        if self == prune and seen:  # it was there when looked at, and gone right after
            os.unlink(self)
        return seen

    monkeypatch.setattr(Path, "exists", other_unpack_removes_it)
    assert unpack_run(a, s.root) == "r1"
    monkeypatch.undo()
    assert not prune.exists() and verify_run(*s.parts, "r1").ok



# --- finding 8: damaged pack and prune records are named refusals ---------------------------

_DAMAGED = {
    "empty": b"",
    "list": b"[]",
    "garbage": b"{not json",
    "no-archive": b'{"archive_sha256": "x"}',
    "members-not-a-dict": b'{"archive": "a", "archive_sha256": "x", "last_seal": {"n": 0, "hash": "h"}, "members": []}',
}


@pytest.mark.parametrize("pruned", [False, True], ids=["packed", "pruned"])
@pytest.mark.parametrize("body", list(_DAMAGED.values()), ids=list(_DAMAGED))
def test_a_damaged_pack_record_is_a_named_refusal(s: SimpleNamespace, tmp_path: Path, body: bytes, pruned: bool) -> None:
    pack_run(s.root, "r1", tmp_path / "A.tar.zst")
    if pruned:
        prune_run(s.root, "r1")
    (s.root / "runs" / "r1" / "pack.json").write_bytes(body)
    before = store_tree(s.root)
    with pytest.raises(PruneRefused, match="pack.json"):
        prune_run(s.root, "r1")
    assert store_tree(s.root) == before
    res = run_cli("prune", "r1", root=s.root)
    assert res.exit_code == 1 and "PruneRefused" in res.stderr and "pack.json" in res.stderr


@pytest.mark.parametrize("body", [*_DAMAGED.values(), b'{"archive_sha256": "' + b"0" * 64 + b'"}'], ids=[*_DAMAGED, "other-archive"])
def test_a_damaged_prune_record_still_means_pruned(s: SimpleNamespace, tmp_path: Path, body: bytes) -> None:
    """A prune.json that is garbage, or names another archive, is never taken for "not
    pruned": prune refuses by name, writers and pack refuse the run, and unpacking the
    recorded archive restores it."""
    archive = tmp_path / "A.tar.zst"
    pack_run(s.root, "r1", archive)
    prune_run(s.root, "r1")
    (s.root / "runs" / "r1" / "prune.json").write_bytes(body)
    before = store_tree(s.root)
    with pytest.raises(PruneRefused, match="prune.json"):
        prune_run(s.root, "r1")
    with pytest.raises(RunPruned):
        s.log.open_writer("r1", "w9")
    with pytest.raises(RunPruned):
        pack_run(s.root, "r1", tmp_path / "B.tar.zst")
    assert store_tree(s.root) == before
    assert unpack_run(archive, s.root) == "r1"  # the recovery: pack.json still names it
    assert verify_run(*s.parts, "r1").ok and not (s.root / "runs" / "r1" / "prune.json").exists()


@pytest.mark.parametrize("body", [b"[]", b'{"include_blobs": false, "blobs": 7}'], ids=["list", "blobs-not-a-list"])
def test_another_runs_damaged_pack_record_is_a_named_refusal(s: SimpleNamespace, tmp_path: Path, body: bytes) -> None:
    pack_run(s.root, "r1", tmp_path / "A.tar.zst")
    pack_run(s.root, "r2", tmp_path / "B.tar.zst")
    (s.root / "runs" / "r2" / "pack.json").write_bytes(body)
    before = store_tree(s.root)
    with pytest.raises(PruneRefused, match="r2.*pack.json"):
        prune_run(s.root, "r1")
    assert store_tree(s.root) == before
