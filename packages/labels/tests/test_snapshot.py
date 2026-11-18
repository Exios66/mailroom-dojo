"""Snapshot manifests: the tamper matrix, build determinism and streaming failures.

Every mutation asserts the exact error and kind; the untouched control loads."""

from __future__ import annotations

import json
import random
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from labels_testkit import hub_row, make_snapshot, rehash_manifest

from mailroom_labels.errors import (
    CorruptLine,
    DuplicateSnapshot,
    ManifestMismatch,
    ManifestMissing,
    UnknownSnapshotSchema,
)
from mailroom_labels.layers import CURRENT_LAYER_SCHEMA
from mailroom_labels.snapshot import (
    MANIFEST_NAME,
    build_manifest,
    load_snapshot,
    load_snapshot_set,
    snapshot_refs,
    write_snapshot,
)

ROWS_A = [hub_row(f"A{i}", fields={"sender": f"S{i}"}) for i in range(4)]
ROWS_B = [hub_row(f"B{i}", "filing", "annual", {"entity": f"E{i}"}) for i in range(3)]


def build(directory: Path, rows_a=ROWS_A, rows_b=ROWS_B):
    return write_snapshot(
        directory,
        {"a.jsonl": rows_a, "part/b.jsonl": rows_b},
        snapshot_id="hub-t",
        kind="hub",
        layer_schema=CURRENT_LAYER_SCHEMA["hub"],
        source={"repository": "synthetic", "revision": "r1"},
        builder="test",
    )


@pytest.fixture
def snap(tmp_path: Path) -> Path:
    directory = tmp_path / "snap"
    build(directory)
    return directory


# --- mutations --------------------------------------------------------------------


def flip_byte(d: Path) -> None:
    path = d / "a.jsonl"
    data = bytearray(path.read_bytes())
    i = data.index(b"S2")
    data[i + 1] = ord("9")
    path.write_bytes(bytes(data))


def truncate(d: Path) -> None:
    path = d / "part/b.jsonl"
    path.write_bytes(path.read_bytes()[:-7])


def append_line(d: Path) -> None:
    with (d / "a.jsonl").open("ab") as fh:
        fh.write(b'{"extra":1}\n')


def delete_file(d: Path) -> None:
    (d / "part/b.jsonl").unlink()


def add_unlisted_jsonl(d: Path) -> None:
    (d / "c.jsonl").write_bytes(b'{"x":1}\n')


def add_unlisted_readme(d: Path) -> None:
    (d / "part" / "README.md").write_text("notes\n")


def swap_rows(d: Path) -> None:
    path = d / "a.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    lines[0], lines[1] = lines[1], lines[0]
    path.write_bytes(b"".join(lines))


def edit_manifest(edit: Callable[[dict], None], *, rehash: bool) -> Callable[[Path], None]:
    def mutate(d: Path) -> None:
        if rehash:
            rehash_manifest(d, edit)
        else:
            path = d / MANIFEST_NAME
            manifest = json.loads(path.read_text())
            edit(manifest)
            path.write_text(json.dumps(manifest))
    return mutate


def set_rows(m: dict) -> None:
    m["files"][0]["rows"] += 1


def reverse_files(m: dict) -> None:
    m["files"].reverse()


def duplicate_file(m: dict) -> None:
    m["files"].append(dict(m["files"][-1]))


def escape_path(m: dict) -> None:
    m["files"][0]["path"] = "../a.jsonl"


def non_jsonl_path(m: dict) -> None:
    m["files"][0]["path"] = "a.txt"


def drop_builder(m: dict) -> None:
    del m["builder"]


def change_source(m: dict) -> None:
    m["source"]["revision"] = "r2"


def bad_kind(m: dict) -> None:
    m["kind"] = "gossip"


TAMPER = [
    ("flip_byte", flip_byte, ManifestMismatch, "changed"),
    ("truncate", truncate, ManifestMismatch, "truncated"),
    ("append_line", append_line, ManifestMismatch, "grown"),
    ("delete_file", delete_file, ManifestMismatch, "missing"),
    ("add_unlisted_jsonl", add_unlisted_jsonl, ManifestMismatch, "extra"),
    ("add_unlisted_readme", add_unlisted_readme, ManifestMismatch, "extra"),
    ("swap_rows", swap_rows, ManifestMismatch, "changed"),
    ("edit_count", edit_manifest(set_rows, rehash=False), ManifestMismatch, "manifest_hash"),
    ("edit_count_and_rehash", edit_manifest(set_rows, rehash=True), ManifestMismatch, "row_count"),
    ("edit_source", edit_manifest(change_source, rehash=False), ManifestMismatch, "manifest_hash"),
    ("reorder_files", edit_manifest(reverse_files, rehash=False), ManifestMismatch, "manifest_hash"),
    ("reorder_files_and_rehash", edit_manifest(reverse_files, rehash=True), ManifestMismatch, "file_order"),
    ("duplicate_file_and_rehash", edit_manifest(duplicate_file, rehash=True), ManifestMismatch, "file_order"),
    ("escape_path_and_rehash", edit_manifest(escape_path, rehash=True), ManifestMismatch, "unsafe_path"),
    ("non_jsonl_and_rehash", edit_manifest(non_jsonl_path, rehash=True), ManifestMismatch, "unsafe_path"),
    ("drop_key_and_rehash", edit_manifest(drop_builder, rehash=True), ManifestMismatch, "manifest_invalid"),
    ("bad_kind_and_rehash", edit_manifest(bad_kind, rehash=True), ManifestMismatch, "manifest_invalid"),
    ("manifest_not_json", lambda d: (d / MANIFEST_NAME).write_text("{not json"), ManifestMismatch, "manifest_invalid"),
    ("manifest_a_list", lambda d: (d / MANIFEST_NAME).write_text("[]"), ManifestMismatch, "manifest_invalid"),
    ("delete_manifest", lambda d: (d / MANIFEST_NAME).unlink(), ManifestMissing, None),
    (
        "newer_manifest_schema",
        edit_manifest(lambda m: m.update(manifest_schema="mailroom-labels/manifest/v9"), rehash=True),
        UnknownSnapshotSchema,
        None,
    ),
]


def test_untouched_snapshot_loads(snap: Path) -> None:
    loaded = load_snapshot(snap)
    assert [f.path for f in loaded.manifest.files] == ["a.jsonl", "part/b.jsonl"]
    assert [f.rows for f in loaded.manifest.files] == [4, 3]
    assert len(list(loaded.documents())) == 7
    assert loaded.manifest.source == {"repository": "synthetic", "revision": "r1"}


@pytest.mark.parametrize(("name", "mutate", "error", "kind"), TAMPER, ids=[t[0] for t in TAMPER])
def test_tamper_is_refused_with_the_exact_kind(snap: Path, name, mutate, error, kind) -> None:
    mutate(snap)
    with pytest.raises(error) as info:
        load_snapshot(snap)
    if kind is not None:
        assert info.value.kind == kind


def test_every_mismatch_kind_is_in_the_tamper_matrix() -> None:
    from mailroom_labels.errors import MISMATCH_KINDS

    assert {t[3] for t in TAMPER if t[2] is ManifestMismatch} == set(MISMATCH_KINDS)


def test_mismatch_names_the_file(snap: Path) -> None:
    truncate(snap)
    with pytest.raises(ManifestMismatch) as info:
        load_snapshot(snap)
    assert info.value.path == snap / "part/b.jsonl"


# --- corrupt lines -----------------------------------------------------------------


def test_build_refuses_a_corrupt_line(tmp_path: Path) -> None:
    d = tmp_path / "s"
    d.mkdir()
    (d / "a.jsonl").write_bytes(b'{"ok":1}\n{broken\n')
    with pytest.raises(CorruptLine) as info:
        build_manifest(d, snapshot_id="s", kind="hub", layer_schema="x", source={}, builder="t")
    assert (info.value.path.name, info.value.line) == ("a.jsonl", 2)
    assert not (d / MANIFEST_NAME).exists()


def test_build_refuses_a_non_jsonl_file(tmp_path: Path) -> None:
    d = tmp_path / "s"
    d.mkdir()
    (d / "notes.md").write_text("x")
    with pytest.raises(ManifestMismatch) as info:
        build_manifest(d, snapshot_id="s", kind="hub", layer_schema="x", source={}, builder="t")
    assert info.value.kind == "unsafe_path"


@pytest.mark.parametrize(
    ("content", "line", "detail"),
    [
        (b'{"a":1}\n{broken\n{"c":3}\n', 2, "not JSON"),
        (b'{"a":1}\n[1,2]\n{"c":3}\n', 2, "must be a JSON object"),
        (b'{"a":1}\n\n{"c":3}\n', 2, "blank line"),
        (b'{"a":1}\n{"b":2}\n{"c":3}', 3, "no newline"),
        (b'{"a":1}\n\xff\xfe\n{"c":3}\n', 2, "not JSON"),
    ],
)
def test_corrupt_line_is_reported_with_file_and_line(snap: Path, content: bytes, line: int, detail: str) -> None:
    (snap / "a.jsonl").write_bytes(content)
    rehash_manifest(snap)  # a tamperer who also fixes the hashes: the manifest now agrees
    loaded = load_snapshot(snap)
    with pytest.raises(CorruptLine) as info:
        list(loaded.rows())
    assert (info.value.path, info.value.line) == (snap / "a.jsonl", line)
    assert detail in info.value.detail
    # With a collector the valid neighbours still stream, in order, from both files.
    seen: list[CorruptLine] = []
    rows = list(loaded.rows(on_corrupt=seen.append))
    assert [r.line for r in rows if r.file == "a.jsonl"] == [n for n in (1, 2, 3) if n != line]
    assert len([r for r in rows if r.file == "part/b.jsonl"]) == 3
    assert [e.line for e in seen] == [line]


def test_file_changed_after_load_is_caught_while_streaming(snap: Path) -> None:
    loaded = load_snapshot(snap)
    flip_byte(snap)
    with pytest.raises(ManifestMismatch) as info:
        list(loaded.rows())
    assert info.value.kind == "changed"


# --- determinism -------------------------------------------------------------------


def _all_bytes(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_build_is_deterministic_whatever_the_input_order(tmp_path: Path) -> None:
    one = build(tmp_path / "one")
    shuffled_a, shuffled_b = list(ROWS_A), list(ROWS_B)
    random.Random(7).shuffle(shuffled_a)
    random.Random(8).shuffle(shuffled_b)
    two = build(tmp_path / "two", shuffled_a, shuffled_b)
    assert one.manifest_hash == two.manifest_hash
    assert _all_bytes(tmp_path / "one") == _all_bytes(tmp_path / "two")


def test_one_label_change_changes_exactly_one_file_hash(tmp_path: Path) -> None:
    before = build(tmp_path / "before")
    rows = [dict(r) for r in ROWS_A]
    rows[2] = hub_row("A2", fields={"sender": "someone else"})
    after = build(tmp_path / "after", rows)
    changed = [a.path for a, b in zip(before.files, after.files, strict=True) if a.sha256 != b.sha256]
    assert changed == ["a.jsonl"]
    assert before.manifest_hash != after.manifest_hash


def test_rebuilding_in_place_is_idempotent(snap: Path) -> None:
    first = (snap / MANIFEST_NAME).read_bytes()
    build(snap)
    assert (snap / MANIFEST_NAME).read_bytes() == first


# --- sets and refs -----------------------------------------------------------------


def test_snapshot_set_sorted_and_refuses_duplicates(tmp_path: Path) -> None:
    b = make_snapshot(tmp_path, "review", [], snapshot_id="b-review")
    a = make_snapshot(tmp_path, "hub", [hub_row("D1")], snapshot_id="a-hub")
    snaps = load_snapshot_set([b, a])
    assert [s.snapshot_id for s in snaps] == ["a-hub", "b-review"]
    refs = snapshot_refs(reversed(snaps))
    assert refs == (
        {"snapshot_id": "a-hub", "kind": "hub", "manifest_hash": snaps[0].manifest_hash},
        {"snapshot_id": "b-review", "kind": "review", "manifest_hash": snaps[1].manifest_hash},
    )
    copy = tmp_path / "copy"
    shutil.copytree(a, copy)
    with pytest.raises(DuplicateSnapshot):
        load_snapshot_set([a, copy])


def test_empty_snapshot_is_valid(tmp_path: Path) -> None:
    d = make_snapshot(tmp_path, "review", [])
    loaded = load_snapshot(d)
    assert [(f.rows, f.bytes) for f in loaded.manifest.files] == [(0, 0)]
    assert list(loaded.rows()) == []
