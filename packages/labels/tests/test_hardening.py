"""Regression tests for the second review round of the labels package: each one fails on the
code as it was before its fix. Synthetic data only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import mailroom_labels.importers.hub as hub_module
import mailroom_labels.labelset as labelset_module
import mailroom_labels.snapshot as snapshot_module
from labels_testkit import (
    PROFILE_PATH,
    hub_row,
    interim_row,
    make_snapshot,
    near_gold_row,
    profile,
    rehash_manifest,
    review_row,
    standard_set,
)
from mailroom_labels.errors import ManifestMismatch, ProfileError
from mailroom_labels.importers import ImportRefused
from mailroom_labels.importers.common import known_warnings_for
from mailroom_labels.importers.hub import load_hub_pins
from mailroom_labels.labelset import QualityGateFailed, load_label_set
from mailroom_labels.layers import CURRENT_LAYER_SCHEMA, hash_matches
from mailroom_labels.profile import load_profile
from mailroom_labels.profile import load_unique_yaml as load_yaml_unique
from mailroom_labels.quality import check_dirs, verify
from mailroom_labels.snapshot import build_manifest, load_snapshot, write_snapshot
from test_importers import (
    hub_build,
    pins_for,
    write_dataset,
)

ARGS = dict(
    snapshot_id="hub-a",
    kind="hub",
    layer_schema=CURRENT_LAYER_SCHEMA["hub"],
    source={"repository": "synthetic"},
    builder="test",
)


# --- unique-key YAML everywhere ----------------------------------------------------------------


def test_load_yaml_unique_refuses_repeated_and_unhashable_keys() -> None:
    assert load_yaml_unique("a: 1\nb: [1, 2]\n") == {"a": 1, "b": [1, 2]}  # positive control
    with pytest.raises(yaml.YAMLError, match="duplicate key 'a'"):
        load_yaml_unique("a: 1\na: 2\n")
    with pytest.raises(yaml.YAMLError, match="unhashable"):
        load_yaml_unique("? [a, b]\n: 1\n")


def test_known_warnings_file_with_a_repeated_snapshot_block_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "known.yaml"
    entry = "- {kind: orphan_decision, doc: D1, field: sender, reason: r}\n"
    path.write_text(f"snap-a:\n{entry}", encoding="utf-8")
    assert len(known_warnings_for(path, "snap-a")) == 1
    path.write_text(f"snap-a:\n{entry}snap-a:\n{entry}", encoding="utf-8")
    with pytest.raises(ImportRefused, match="duplicate key 'snap-a'"):
        known_warnings_for(path, "snap-a")


def test_hub_pins_file_with_a_repeated_shard_path_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "pins.yaml"
    body = f"'{'0' * 40}':\n  manifest.txt: {'a' * 64}\n"
    path.write_text(body, encoding="utf-8")
    assert load_hub_pins(path)["0" * 40]["manifest.txt"] == "a" * 64
    path.write_text(body + f"  manifest.txt: {'b' * 64}\n", encoding="utf-8")
    with pytest.raises(ImportRefused, match="duplicate key 'manifest.txt'"):
        load_hub_pins(path)


def _profile_text(replace: tuple[str, str] | None = None) -> str:
    text = PROFILE_PATH.read_text(encoding="utf-8")
    return text.replace(*replace) if replace else text


def test_unknown_keys_of_different_types_are_all_reported(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text(_profile_text() + "1: x\ntypo: y\n", encoding="utf-8")
    with pytest.raises(ProfileError) as info:
        load_profile(path)
    assert any("1" in p for p in info.value.problems) and any("typo" in p for p in info.value.problems)


def test_an_unquoted_yaml_date_as_dataset_revision_is_a_profile_error(tmp_path: Path) -> None:
    text = _profile_text()
    assert "dataset:" in text
    path = tmp_path / "p.yaml"
    path.write_text(text + "\n", encoding="utf-8")
    good = load_profile(path)
    assert good.dataset is not None  # positive control
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        text.replace("dataset:", "dataset: {revision: 2026-11-18}\nignored_dataset:", 1), encoding="utf-8"
    )
    with pytest.raises(ProfileError):
        load_profile(bad)


def test_a_profile_that_is_not_utf8_is_a_profile_error(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_bytes(b"client: \xff\xfe\n")
    with pytest.raises(ProfileError, match="cannot read"):
        load_profile(path)


# --- hashes must match in full -----------------------------------------------------------------


def test_a_hash_with_a_trailing_newline_never_matches() -> None:
    full = "a" * 64
    assert hash_matches("a" * 16, full) and hash_matches(full, full)  # positive controls
    assert not hash_matches("a" * 16 + "\n", full)
    assert not hash_matches("a" * 63 + "\n", full[:63] + "\n")


def test_a_hub_content_hash_of_63_digits_and_a_newline_is_a_malformed_record(tmp_path: Path) -> None:
    bad = make_snapshot(tmp_path, "hub", [hub_row("D1", sha="a" * 63 + "\n")])
    report, _ = check_dirs([bad], profile())
    assert [f.kind for f in report.errors] == ["malformed_record"]


# --- JSON that the interpreter converts badly is a finding, not a crash -----------------------------


@pytest.mark.parametrize(
    "bad_line", [b'{"doc_id": "D4", "x": 1e400}\n', b'{"doc_id": "D4", "x": ' + b"9" * 5000 + b"}\n"]
)
def test_a_row_with_an_infinite_float_or_an_over_long_integer_is_a_corrupt_line(
    tmp_path: Path, bad_line: bytes
) -> None:
    d = make_snapshot(tmp_path, "review", [review_row("D1", "sender", "Ann")], snapshot_id="rv-extra")
    with (d / "rows.jsonl").open("ab") as fh:
        fh.write(bad_line)
    rehash_manifest(d)
    report, loaded = check_dirs([*standard_set(tmp_path), d], profile())
    assert "corrupt_line" in {f.kind for f in report.errors}
    assert "rv-extra" in {s.snapshot_id for s in loaded}  # the snapshot and its valid rows still load


def test_a_manifest_with_an_infinite_float_is_manifest_invalid(tmp_path: Path) -> None:
    d = make_snapshot(tmp_path, "near_gold", [near_gold_row("D1", {"sender": "Ann"})], snapshot_id="ng-a")
    text = (d / "manifest.json").read_text(encoding="utf-8")
    (d / "manifest.json").write_text(
        text.replace('"builder"', '"x": 1e400,\n  "builder"', 1), encoding="utf-8"
    )
    with pytest.raises(ManifestMismatch) as info:
        load_snapshot(d)
    assert info.value.kind == "manifest_invalid"
    report, _ = check_dirs(
        [*standard_set(tmp_path / "ok"), d], profile()
    )  # other snapshots are still checked
    assert report.errors and verify(report) == 1


def test_a_file_that_cannot_be_read_is_reported_and_the_rest_are_still_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirs = standard_set(tmp_path)
    real = snapshot_module._file_facts

    def deny(path: Path, *, parse: bool):
        if path.parent.name == "interim-test":
            raise PermissionError("denied")
        return real(path, parse=parse)

    monkeypatch.setattr(snapshot_module, "_file_facts", deny)
    report, loaded = check_dirs(dirs, profile())
    assert "manifest_mismatch" in {f.kind for f in report.errors}
    assert {s.snapshot_id for s in loaded} == {"hub-test", "near_gold-test", "review-test"}


# --- write_snapshot ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "/abs.jsonl",
        "../out.jsonl",
        "a//b.jsonl",
        "./a.jsonl",
        "a/../b.jsonl",
        "notes.txt",
        "manifest.json",
        "a\\b.jsonl",
        "",
    ],
)
def test_write_snapshot_refuses_unsafe_names_before_touching_anything(tmp_path: Path, name: str) -> None:
    d = tmp_path / "hub-a"
    with pytest.raises(ValueError, match="output path"):
        write_snapshot(d, {"a.jsonl": [{"x": 1}], name: [{"x": 2}]}, **ARGS)
    assert not d.exists() and sorted(p.name for p in tmp_path.iterdir()) == []


def test_write_snapshot_refuses_two_names_for_one_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="output path"):
        write_snapshot(tmp_path / "hub-a", {"a/b.jsonl": [{"x": 1}], "a//b.jsonl": [{"x": 2}]}, **ARGS)


def test_nested_jsonl_names_stay_valid(tmp_path: Path) -> None:
    manifest = write_snapshot(tmp_path / "hub-a", {"a/b.jsonl": [{"x": 1}]}, **ARGS)
    assert [f.path for f in manifest.files] == ["a/b.jsonl"]
    load_snapshot(tmp_path / "hub-a")


def test_rows_with_equal_sort_keys_are_written_in_one_order_whatever_the_input_order(tmp_path: Path) -> None:
    rows = [{"id": 1, "v": "b"}, {"id": 1, "v": "a"}, {"id": 0, "v": "z"}]
    one = write_snapshot(tmp_path / "x" / "hub-a", {"a.jsonl": rows}, sort_key=lambda r: r["id"], **ARGS)
    two = write_snapshot(
        tmp_path / "y" / "hub-a", {"a.jsonl": rows[::-1]}, sort_key=lambda r: r["id"], **ARGS
    )
    assert one.manifest_hash == two.manifest_hash
    assert (tmp_path / "x/hub-a/a.jsonl").read_bytes() == (tmp_path / "y/hub-a/a.jsonl").read_bytes()
    assert [json.loads(line)["id"] for line in (tmp_path / "x/hub-a/a.jsonl").read_text().splitlines()] == [
        0,
        1,
        1,
    ]


def test_a_failed_rebuild_leaves_the_earlier_snapshot_in_place(tmp_path: Path) -> None:
    d = tmp_path / "hub-a"
    write_snapshot(d, {"a.jsonl": [{"x": 1}], "b.jsonl": [{"x": 2}]}, **ARGS)
    before = {p.name: p.read_bytes() for p in d.iterdir()}

    def boom(row: dict) -> int:
        if row["x"] == 99:
            raise RuntimeError("write failed part way")
        return row["x"]

    with pytest.raises(RuntimeError):
        write_snapshot(d, {"a.jsonl": [{"x": 5}], "b.jsonl": [{"x": 99}]}, sort_key=boom, **ARGS)
    assert {p.name: p.read_bytes() for p in d.iterdir()} == before
    assert load_snapshot(d).manifest.snapshot_id == "hub-a"  # still a loadable snapshot
    assert [p.name for p in tmp_path.iterdir()] == ["hub-a"]  # no staging leftovers


def test_a_successful_rebuild_replaces_the_snapshot_whole(tmp_path: Path) -> None:
    d = tmp_path / "hub-a"
    write_snapshot(d, {"a.jsonl": [{"x": 1}], "old.jsonl": [{"x": 2}]}, **ARGS)
    write_snapshot(d, {"a.jsonl": [{"x": 3}]}, **ARGS)
    assert sorted(p.name for p in d.iterdir()) == ["a.jsonl", "manifest.json"]
    assert [p.name for p in tmp_path.iterdir()] == ["hub-a"]


def test_build_and_load_agree_about_stray_directories(tmp_path: Path) -> None:
    d = tmp_path / "hub-a"
    d.mkdir()
    (d / "a.jsonl").write_bytes(b'{"x": 1}\n')
    (d / "empty").mkdir()
    with pytest.raises(ManifestMismatch) as info:
        build_manifest(d, **ARGS)
    assert info.value.kind == "extra"
    assert not (d / "manifest.json").exists()  # no manifest that load_snapshot would then reject
    (d / "empty").rmdir()
    (d / "nested").mkdir()
    (d / "nested" / "b.jsonl").write_bytes(b'{"x": 2}\n')
    build_manifest(d, **ARGS)  # a parent of a listed data file is fine
    load_snapshot(d)


# --- the manifest identity a run records is the manifest actually used --------------------------------


def test_a_manifest_replaced_after_the_gate_fails_load_label_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirs = standard_set(tmp_path)
    real_check_dirs = labelset_module.check_dirs

    def gate_then_replace_manifest(directories, prof):
        result = real_check_dirs(directories, prof)
        rehash_manifest(tmp_path / "near_gold-test", lambda m: m["source"].update(note="replaced"))
        return result

    monkeypatch.setattr(labelset_module, "check_dirs", gate_then_replace_manifest)
    with pytest.raises(QualityGateFailed) as info:
        load_label_set(dirs, profile())
    assert info.value.report.errors[0].kind == "manifest_mismatch"


# --- resolved values cannot be edited in place -------------------------------------------------------


def test_editing_a_returned_list_value_does_not_change_later_reads(tmp_path: Path) -> None:
    label_set = load_label_set(standard_set(tmp_path), profile())
    first = label_set.document("D3")
    assert first.fields["signers"].value == ["P. Q."]
    first.fields["signers"].value.append("Mallory")
    first.fields["signers"].layers_seen[0].value.append("Mallory")
    assert label_set.document("D3").fields["signers"].value == ["P. Q."]
    assert next(d for d in label_set.documents() if d.document_id == "D3").fields["signers"].value == [
        "P. Q."
    ]
    assert label_set.document("D3") == label_set.document("D3")  # a copy equals its source
    streamed = next(d for d in label_set.documents() if d.document_id == "D3")
    streamed.fields["signers"].value.append("Mallory")  # a document handed out by documents() is a copy too
    assert next(d for d in label_set.documents() if d.document_id == "D3").fields["signers"].value == [
        "P. Q."
    ]


# --- near-gold unlabeled rows and populated-without-value --------------------------------------------


def test_an_unlabeled_near_gold_row_is_an_error_and_never_a_label(tmp_path: Path) -> None:
    row = near_gold_row("D4", {"recipient": "Eve"})
    row["labels"]["recipient"]["presence"] = "unlabeled"
    extra = make_snapshot(tmp_path, "near_gold", [row], snapshot_id="ng-extra")
    report, loaded = check_dirs([*standard_set(tmp_path), extra], profile())
    assert ("malformed_record", "D4", "recipient") in {(f.kind, f.doc, f.field) for f in report.errors}
    with pytest.raises(QualityGateFailed):
        load_label_set([*standard_set(tmp_path / "again"), extra], profile())


def test_hub_import_parses_the_shard_bytes_it_hashed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = write_dataset(tmp_path / "data")
    pins = pins_for(data)
    shard = data / "parquet/ground_truth/train/train-00000-of-00001.parquet"
    real = hub_module._read_parquet

    def swap_then_parse(raw: bytes):
        shard.write_bytes(b"not a parquet file any more")
        return real(raw)

    monkeypatch.setattr(hub_module, "_read_parquet", swap_then_parse)
    hub_build(tmp_path / "hub-x", data, pins=pins, default=False)  # would raise if it re-read the file
    ids = {r.data["document_id"] for r in load_snapshot(tmp_path / "hub-x").rows()}
    assert {"D1", "D2"} <= ids


def test_the_declared_pydantic_floor_provides_jsonvalue() -> None:
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pydantic>=2.5' in text  # JsonValue first ships in pydantic 2.5


# --- review round 3 -------------------------------------------------------------------------------


def test_build_manifest_refuses_a_manifest_link_that_points_outside(tmp_path: Path) -> None:
    d = tmp_path / "hub-a"
    d.mkdir()
    (d / "a.jsonl").write_text('{"x": 1}\n', encoding="utf-8")
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("keep me", encoding="utf-8")
    (d / "manifest.json").symlink_to(outside)
    with pytest.raises(ManifestMismatch) as info:
        build_manifest(
            d, snapshot_id="hub-a", kind="hub", layer_schema=ARGS["layer_schema"], source={}, builder="test"
        )
    assert info.value.kind == "unsafe_path"
    assert outside.read_text(encoding="utf-8") == "keep me"


def test_source_lines_with_an_over_long_integer_are_rejected_and_later_rows_still_read() -> None:
    from mailroom_labels.importers.common import parse_jsonl_lenient

    data = b'{"x": ' + b"9" * 5000 + b"}\n" + b'{"x": 1}\n'
    rows, rejected = parse_jsonl_lenient(data, "src.jsonl")
    assert [r.data for r in rows] == [{"x": 1}]
    assert [(r["line"], r["file"]) for r in rejected] == [(1, "src.jsonl")]


def test_a_label_that_looks_like_a_token_is_not_trusted_as_one() -> None:
    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts" / "labels_report.py"
    spec = importlib.util.spec_from_file_location("labels_report_hardening_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    fake = {"$token": "Jane Q. Person, 12 Elm Street"}
    shown = module._token(fake)
    assert "Jane" not in shown and shown.startswith("object of 1 keys, sha256:")
    real = module._token("Jane Q. Person")
    assert module._token({"$token": real}) == real  # a token it generated is kept as it is


def test_findings_about_a_bad_status_or_verdict_never_repeat_the_supplied_text(tmp_path: Path) -> None:
    hub = make_snapshot(tmp_path, "hub", [hub_row("D1", fields={"sender": "Ann"})])
    interim = make_snapshot(
        tmp_path, "interim", [interim_row("D1", {"sender": ("SECRET-status", "X")})], snapshot_id="in-a"
    )
    review = make_snapshot(
        tmp_path, "review", [review_row("D1", "sender", "Ann", verdict="SECRET-verdict")], snapshot_id="rv-a"
    )
    report, _ = check_dirs([hub, interim, review], profile())
    flagged = [f for f in report.findings if f.kind == "status_not_allowed"]
    assert len(flagged) == 2  # positive control: both bad values were reported
    assert all("SECRET" not in f.detail for f in report.findings)


def test_an_absent_reason_that_is_not_text_is_a_malformed_review_entry(tmp_path: Path) -> None:
    row = review_row("D1", "sender", "Ann")
    row["entry"]["absent"] = {"unexpected": True}
    hub = make_snapshot(tmp_path, "hub", [hub_row("D1", fields={"sender": "Ann"})])
    review = make_snapshot(tmp_path, "review", [row], snapshot_id="rv-a")
    report, _ = check_dirs([hub, review], profile())
    assert [(f.kind, f.doc, f.field) for f in report.errors] == [("malformed_record", "D1", "sender")]


def test_a_manifest_replaced_between_reading_and_checking_is_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirs = standard_set(tmp_path)
    snaps = [load_snapshot(d) for d in dirs]
    from mailroom_labels import quality

    real_layer = quality.read_layer

    def read_then_replace(snap: object):  # the data is read, then the manifest changes underneath
        result = real_layer(snap)  # type: ignore[arg-type]
        manifest = snap.directory / "manifest.json"  # type: ignore[attr-defined]
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace('"builder"', '"builder_x"', 1), encoding="utf-8"
        )
        return result

    monkeypatch.setattr(quality, "read_layer", read_then_replace)
    report = quality.check(snaps, profile())
    assert any(f.kind == "manifest_mismatch" for f in report.findings)
