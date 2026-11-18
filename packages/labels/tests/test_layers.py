"""Layer readers: each native shape becomes the right normalised records; bad rows become
findings, never exceptions; old layer schemas upgrade at read time (ADR-0005)."""

from __future__ import annotations

from pathlib import Path

import pytest
from labels_testkit import (
    hub_row,
    profile,
    interim_row,
    make_snapshot,
    near_gold_row,
    rehash_manifest,
    review_row,
    text_hash,
)

from mailroom_labels import layers
from mailroom_labels.errors import UnknownSnapshotSchema
from mailroom_labels.layers import corpus_index, hash_matches, read_layer, read_layers
from mailroom_labels.resolve import resolve
from mailroom_labels.snapshot import load_snapshot, load_snapshot_set


def read(tmp_path: Path, kind: str, rows: list[dict], **kw):
    return read_layer(load_snapshot(make_snapshot(tmp_path, kind, rows, **kw)))


def test_hub_rows_become_corpus_entries_and_labels(tmp_path: Path) -> None:
    out = read(tmp_path, "hub", [
        hub_row("D1", fields={
            "sender": "Ann",
            "recipient": None,
            "amount": (None, "not_applicable"),
            "urgency": ("", "unlabeled"),
        }),
    ], source={"text_unverified_documents": ["D1"]})
    assert out.findings == []
    (entry,) = out.corpus
    assert (entry.document_id, entry.doc_class, entry.subclass, entry.text_verified) == ("D1", "letter", "email", False)
    got = {(r.field, r.presence, r.rank, r.value) for r in out.records}
    assert got == {
        ("sender", "populated", "hub", "Ann"),
        ("recipient", "schema_documented_absence", "hub:absent", None),
        ("amount", "not_applicable", "hub:absent", None),
    }
    assert all(r.content_sha256 == text_hash("D1") and r.source == "hub:hub-test" for r in out.records)


def test_interim_statuses_map_to_ranks_and_disputed_is_kept(tmp_path: Path) -> None:
    out = read(tmp_path, "interim", [interim_row("D1", {
        "sender": ("gap_fill", "Ann"),
        "recipient": ("correction", "Bob"),
        "urgency": ("derived", "routine"),
        "sent_on": ("disputed", "2020-01-01"),
        "amount": ("gap_fill", None),
    })])
    assert out.findings == []
    by_field = {r.field: r for r in out.records}
    assert {f: r.rank for f, r in by_field.items()} == {
        "sender": "interim:gap_fill",
        "recipient": "interim:correction",
        "urgency": "interim:derived",
        "sent_on": "interim:disputed",
        "amount": "interim:gap_fill",
    }
    assert by_field["amount"].presence == "schema_documented_absence"
    assert by_field["sender"].source == "interim:gate-synthetic"
    assert by_field["sender"].content_sha256 == text_hash("D1")[:16]


def test_near_gold_labels_are_near_gold_whatever_the_native_status(tmp_path: Path) -> None:
    out = read(tmp_path, "near_gold", [near_gold_row("D1", {"sender": "Ann", "sent_on": None},
                                                     basis="near_gold:opus_reviewed_correct")])
    assert out.findings == []
    assert {(r.field, r.rank, r.native_status, r.presence, r.source) for r in out.records} == {
        ("sender", "near_gold", "gap_fill", "populated", "near_gold:opus_reviewed_correct"),
        ("sent_on", "near_gold", "gap_fill", "schema_documented_absence", "near_gold:opus_reviewed_correct"),
    }


def test_review_human_is_gold_and_claude_is_near_gold(tmp_path: Path) -> None:
    out = read(tmp_path, "review", [
        review_row("D1", "sender", "Ann", human=True),
        review_row("D1", "action_items", [], absent="nothing asked"),
        review_row("D2", "sent_on", None, verdict="accept"),
    ])
    assert out.findings == []
    got = {(r.document_id, r.field): r for r in out.records}
    human = got[("D1", "sender")]
    assert (human.rank, human.reviewer, human.source) == ("review:gold", "human:desk", "review:human-desk")
    audit = got[("D1", "action_items")]
    assert (audit.rank, audit.presence, audit.source) == (
        "review:near_gold", "schema_documented_absence", "review:synthetic-auditor-audit")
    assert got[("D2", "sent_on")].presence == "schema_documented_absence"
    assert all(r.content_sha256 is None and r.doc_class is None for r in out.records)


def _bad_rows():
    good = hub_row("OK")
    missing_key = hub_row("X1")
    del missing_key["split"]
    bad_sha = hub_row("X2", sha="ABC")
    bad_presence = hub_row("X3", fields={"sender": ("Ann", "maybe")})
    bad_field = hub_row("X4")
    bad_field["fields"] = {"sender": {"value": "Ann"}}
    return [good, missing_key, bad_sha, bad_presence, bad_field]


def test_malformed_hub_rows_are_findings_and_neighbours_still_read(tmp_path: Path) -> None:
    out = read(tmp_path, "hub", _bad_rows())
    kinds = sorted((f.kind, f.doc) for f in out.findings)
    assert kinds == [("malformed_record", "X1"), ("malformed_record", "X2"),
                     ("malformed_record", "X3"), ("malformed_record", "X4")]
    assert sorted(e.document_id for e in out.corpus) == ["OK", "X3", "X4"]


@pytest.mark.parametrize(
    ("kind", "row", "expected"),
    [
        ("interim", interim_row("D1", {"sender": ("guess", "Ann")}), "status_not_allowed"),
        ("interim", {**interim_row("D1", {}), "doc_sha256": "xyz"}, "malformed_record"),
        ("interim", {**interim_row("D1", {}), "labels": []}, "malformed_record"),
        ("near_gold", near_gold_row("D1", {"sender": "Ann"}, basis="candidate"), "status_not_allowed"),
        ("review", review_row("D1", "sender", "Ann", verdict="reject"), "status_not_allowed"),
        ("review", {**review_row("D1", "sender", "Ann"), "reviewer": {"model": "human"}}, "malformed_record"),
        ("review", {**review_row("D1", "sender", "Ann"), "entry": {"evidence": []}}, "malformed_record"),
        ("review", {**review_row("D1", "sender", "Ann"), "extra": 1}, "malformed_record"),
    ],
)
def test_bad_native_rows_are_typed_findings(tmp_path: Path, kind: str, row: dict, expected: str) -> None:
    good = {"interim": interim_row("D9", {"sender": ("gap_fill", "Zed")}),
            "near_gold": near_gold_row("D9", {"sender": "Zed"}),
            "review": review_row("D9", "sender", "Zed")}[kind]
    out = read(tmp_path, kind, [row, good])
    assert [f.kind for f in out.findings] == [expected]
    assert [r.document_id for r in out.records] == ["D9"]


def test_truth_flag_must_follow_the_status(tmp_path: Path) -> None:
    row = interim_row("D1", {"sender": ("disputed", "Ann")})
    row["labels"]["sender"]["use_as_truth"] = True
    out = read(tmp_path, "interim", [row])
    assert [f.kind for f in out.findings] == ["inconsistent_truth_flag"]
    assert out.records[0].rank == "interim:flag_mismatch"  # the flag never promotes a dispute


def test_a_truth_status_with_a_false_flag_is_not_truth(tmp_path: Path) -> None:
    """A gap_fill that says use_as_truth=false contradicts itself: reported, never applied."""
    hub = make_snapshot(tmp_path, "hub", [hub_row("D1", fields={"sender": ("", "unlabeled")})])
    row = interim_row("D1", {"sender": ("gap_fill", "Ann")})
    row["labels"]["sender"]["use_as_truth"] = False
    interim = make_snapshot(tmp_path, "interim", [row])
    read_ = read_layers(load_snapshot_set([hub, interim]))
    assert [f.kind for f in read_.findings] == ["inconsistent_truth_flag"]
    (doc,) = resolve(read_.records, profile(), corpus_index(read_.corpus))
    assert "sender" not in doc.fields and [f.rank for f in doc.untrusted["sender"]] == ["interim:flag_mismatch"]


def test_uppercase_or_short_label_hash_is_malformed_and_never_matches(tmp_path: Path) -> None:
    full = text_hash("D1")
    out = read(tmp_path, "interim", [interim_row("D1", {"sender": ("gap_fill", "A")}, sha=full[:16].upper()),
                                     interim_row("D2", {"sender": ("gap_fill", "B")}, sha=full[:8])])
    assert sorted((f.kind, f.doc) for f in out.findings) == [("malformed_record", "D1"), ("malformed_record", "D2")]
    assert out.records == []
    assert not hash_matches(full[:16].upper(), full.upper())
    assert not hash_matches("", full)
    assert not hash_matches(full[:8], full)


def test_corrupt_line_is_a_finding_with_location(tmp_path: Path) -> None:
    d = make_snapshot(tmp_path, "review", [review_row("D1", "sender", "Ann"), review_row("D2", "sender", "Bo")])
    path = d / "rows.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0] + b"{oops\n" + lines[1])
    rehash_manifest(d)
    out = read_layer(load_snapshot(d))
    assert [(f.kind, f.location) for f in out.findings] == [("corrupt_line", "review-test/rows.jsonl:2")]
    assert [r.document_id for r in out.records] == ["D1", "D2"]


def test_unknown_layer_schema_is_refused(tmp_path: Path) -> None:
    d = make_snapshot(tmp_path, "hub", [hub_row("D1")])
    rehash_manifest(d, lambda m: m.update(layer_schema="mailroom-labels/hub/v2"))
    with pytest.raises(UnknownSnapshotSchema):
        read_layer(load_snapshot(d))


def test_old_layer_schema_is_upgraded_at_read_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A v0 hub row called its id ``doc_id``; the upgrade renames it. The stored bytes are
    not rewritten (the manifest still verifies)."""
    def v0_to_v1(row: dict) -> dict:
        row = dict(row)
        row["document_id"] = row.pop("doc_id")
        return row

    monkeypatch.setitem(layers.UPGRADES, ("hub", "mailroom-labels/hub/v0"), ("mailroom-labels/hub/v1", v0_to_v1))
    old = hub_row("D1", fields={"sender": "Ann"})
    old["doc_id"] = old.pop("document_id")
    d = make_snapshot(tmp_path, "hub", [old, {"broken": True}])
    rehash_manifest(d, lambda m: m.update(layer_schema="mailroom-labels/hub/v0"))
    before = (d / "rows.jsonl").read_bytes()
    out = read_layer(load_snapshot(d))
    assert [r.document_id for r in out.records] == ["D1"]
    assert [f.kind for f in out.findings] == ["malformed_record"]  # an upgrade failure is reported
    assert (d / "rows.jsonl").read_bytes() == before


def test_hash_matches_prefix_rule() -> None:
    full = text_hash("D1")
    assert hash_matches(None, full)
    assert hash_matches(full, full)
    assert hash_matches(full[:16], full)
    assert not hash_matches(text_hash("D2")[:16], full)
    assert not hash_matches(full[:16], None)
    assert not hash_matches(full[8:24], full), "a substring that is not a prefix does not name the text"


def test_corpus_index_keeps_the_first_entry(tmp_path: Path) -> None:
    out = read(tmp_path, "hub", [hub_row("D1", "letter"), hub_row("D1", "filing", "annual")])
    assert corpus_index(out.corpus)["D1"].doc_class == "filing"  # rows are sorted by canonical JSON


def test_review_provenance_keys_are_read_and_checked(tmp_path: Path) -> None:
    """``origin`` and ``reviewed_at`` are provenance: accepted when well formed, a
    malformed_record when not, and they never change the decision."""
    good = {**review_row("D1", "sender", "Ann", human=True),
            "origin": ["desk.jsonl:3", "owner.jsonl:1"], "reviewed_at": "2026-11-17T18:13:52Z"}
    bad_origin = {**review_row("D2", "sender", "Bo"), "origin": "desk.jsonl:4"}
    bad_time = {**review_row("D3", "sender", "Cy"), "reviewed_at": 17}
    read = read_layer(load_snapshot(make_snapshot(tmp_path, "review", [good, bad_origin, bad_time])))
    assert [(r.document_id, r.rank, r.value) for r in read.records] == [("D1", "review:gold", "Ann")]
    assert sorted((f.kind, f.doc) for f in read.findings) == [("malformed_record", "D2"), ("malformed_record", "D3")]


def test_hash_matches_compares_every_digit_of_a_longer_label_hash() -> None:
    full = text_hash("D1")
    for n in (17, 32, 63, 64):
        changed = full[: n - 1] + ("0" if full[n - 1] != "0" else "1")
        assert not hash_matches(changed, full), f"a {n}-digit hash differing in its last digit names other text"
        assert hash_matches(full[:n], full)
