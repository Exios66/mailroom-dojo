"""Hostile archives against ``unpack_run`` (adversarial round 3, findings 1, 4 and 5).

Each test builds a synthetic archive the way an attacker would and checks that the
target store is refused and left unchanged, or that the cost of reading it stays bounded.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from record_testkit import (
    build_sealed_store,
    peak_bytes,
    store_tree,
    zero_bomb,
)

import mailroom_record.pack as pack_module
from mailroom_record.pack import PackCorrupt, PackRefused, pack_run, unpack_run

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
