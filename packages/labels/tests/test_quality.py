"""The quality gate: one failure-state test per finding kind.

Each case adds one bad thing to the clean synthetic set and asserts that exactly the
expected findings appear (kind, document, field), that the valid snapshots around it still
load, and that their labels still resolve."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from labels_testkit import (
    hub_row,
    interim_row,
    make_snapshot,
    near_gold_row,
    profile,
    rehash_manifest,
    review_row,
    standard_set,
)

from mailroom_labels.findings import SEVERITY
from mailroom_labels.layers import corpus_index, read_layers
from mailroom_labels.quality import check, check_dirs, value_matches_type, verify
from mailroom_labels.snapshot import load_snapshot_set
from mailroom_labels.resolve import resolve

BASE_IDS = {"hub-test", "interim-test", "near_gold-test", "review-test"}


def ng(root: Path, rows, sid: str = "ng-extra") -> Path:
    return make_snapshot(root, "near_gold", rows, snapshot_id=sid)


def no_manifest(root: Path) -> list[Path]:
    d = root / "bare"
    d.mkdir()
    (d / "rows.jsonl").write_text("{}\n")
    return [d]


def tampered(root: Path) -> list[Path]:
    d = ng(root, [near_gold_row("D4", {"recipient": "Eve"})])
    (d / "rows.jsonl").write_bytes((d / "rows.jsonl").read_bytes().replace(b"Eve", b"Evy"))
    return [d]


def newer_manifest(root: Path) -> list[Path]:
    d = ng(root, [near_gold_row("D4", {"recipient": "Eve"})])
    rehash_manifest(d, lambda m: m.update(manifest_schema="mailroom-labels/manifest/v9"))
    return [d]


def newer_layer(root: Path) -> list[Path]:
    d = ng(root, [near_gold_row("D4", {"recipient": "Eve"})])
    rehash_manifest(d, lambda m: m.update(layer_schema="mailroom-interim-labels/v9"))
    return [d]


def duplicate_snapshot(root: Path) -> list[Path]:
    copy = root / "copy-of-interim"
    shutil.copytree(root / "interim-test", copy)
    return [copy]


def corrupt(root: Path) -> list[Path]:
    d = make_snapshot(root, "review", [review_row("D1", "sender", "Ann")], snapshot_id="rv-extra")
    with (d / "rows.jsonl").open("ab") as fh:
        fh.write(b'{"doc_id": "D1", \n')
    rehash_manifest(d)
    return [d]


def text_unverified(root: Path) -> list[Path]:
    return [make_snapshot(root, "hub", [hub_row("D5", fields={"sender": "Fay"})], snapshot_id="hub-extra",
                          source={"text_unverified_documents": ["D5"]})]


def truth_flag(root: Path) -> list[Path]:
    row = interim_row("D4", {"recipient": ("gap_fill", "Eve")})
    row["labels"]["recipient"]["use_as_truth"] = False
    return [make_snapshot(root, "interim", [row], snapshot_id="in-extra")]


Case = tuple[str, Callable[[Path], list[Path]], set[tuple]]

CASES: list[Case] = [
    ("manifest_missing", no_manifest, {("manifest_missing", None, None)}),
    ("manifest_mismatch", tampered, {("manifest_mismatch", None, None)}),
    ("unknown_schema_manifest", newer_manifest, {("unknown_schema", None, None)}),
    ("unknown_schema_layer", newer_layer, {("unknown_schema", None, None)}),
    ("duplicate_snapshot", duplicate_snapshot, {("duplicate_snapshot", None, None)}),
    ("corrupt_line", corrupt, {("corrupt_line", None, None)}),
    (
        "malformed_record",
        lambda r: [make_snapshot(r, "review", [{**review_row("D1", "sender", "Ann"), "reviewer": "someone"}],
                                 snapshot_id="rv-extra")],
        {("malformed_record", "D1", "sender")},
    ),
    ("unknown_document", lambda r: [ng(r, [near_gold_row("GHOST", {"sender": "Boo"})])],
     {("unknown_document", "GHOST", None)}),
    ("duplicate_document", lambda r: [make_snapshot(r, "hub", [hub_row("D1")], snapshot_id="hub-extra")],
     {("duplicate_document", "D1", None)}),
    ("class_mismatch", lambda r: [ng(r, [near_gold_row("D4", {"sender": "X"}, "filing", None)])],
     {("class_mismatch", "D4", None), ("duplicate_label", "D4", "sender")}),
    ("subclass_mismatch", lambda r: [ng(r, [near_gold_row("D4", {"recipient": "X"}, "letter", "memo")])],
     {("subclass_mismatch", "D4", None)}),
    ("stale_content_hash", lambda r: [ng(r, [near_gold_row("D4", {"recipient": "X"}, sha="0" * 16)])],
     {("stale_content_hash", "D4", None)}),
    ("text_unverified", text_unverified, {("text_unverified", "D5", None)}),
    ("duplicate_label", lambda r: [ng(r, [near_gold_row("D1", {"sent_on": "2020-01-02"})])],
     {("duplicate_label", "D1", "sent_on")}),
    (
        "status_not_allowed",
        lambda r: [make_snapshot(r, "interim", [interim_row("D4", {"recipient": ("guess", "X")})], snapshot_id="in-x")],
        {("status_not_allowed", "D4", "recipient")},
    ),
    ("inconsistent_truth_flag", truth_flag, {("inconsistent_truth_flag", "D4", "recipient")}),
    (
        "orphan_decision",
        lambda r: [make_snapshot(r, "review", [review_row("D4", "recipient", "Eve")], snapshot_id="rv-extra")],
        {("orphan_decision", "D4", "recipient")},
    ),
    (
        "conflicting_decision",
        lambda r: [make_snapshot(r, "review", [review_row("D1", "sent_on", "1999-01-01")], snapshot_id="rv-extra")],
        {("conflicting_decision", "D1", "sent_on")},
    ),
    (
        "conflicting_human_decisions",
        lambda r: [make_snapshot(r, "review", [review_row("D1", "sent_on", "1999-01-01", human=True)],
                                 snapshot_id="rv-extra")],
        {("conflicting_decision", "D1", "sent_on"), ("duplicate_label", "D1", "sent_on")},
    ),
    (
        "unknown_class",
        lambda r: [make_snapshot(r, "hub", [hub_row("D5", "invoice", None, {"total": 1})], snapshot_id="hub-extra")],
        {("unknown_class", "D5", None)},
    ),
    (
        "unknown_subclass",
        lambda r: [make_snapshot(r, "hub", [hub_row("D5", "letter", "fax")], snapshot_id="hub-extra")],
        {("unknown_subclass", "D5", None)},
    ),
    ("unknown_field", lambda r: [ng(r, [near_gold_row("D4", {"colour": "red"})])],
     {("unknown_field", "D4", "colour")}),
    ("type_mismatch_date", lambda r: [ng(r, [near_gold_row("D4", {"sent_on": 20200101})])],
     {("type_mismatch", "D4", "sent_on")}),
    ("type_mismatch_list", lambda r: [ng(r, [near_gold_row("D4", {"action_items": "call"})])],
     {("type_mismatch", "D4", "action_items")}),
    ("type_mismatch_money", lambda r: [ng(r, [near_gold_row("D4", {"amount": True})])],
     {("type_mismatch", "D4", "amount")}),
    ("vocabulary_violation", lambda r: [ng(r, [near_gold_row("D4", {"urgency": "panic"})])],
     {("vocabulary_violation", "D4", "urgency")}),
    ("vocabulary_violation_hub",
     lambda r: [make_snapshot(r, "hub", [hub_row("D5", fields={"urgency": "panic"})], snapshot_id="hub-extra")],
     {("vocabulary_violation", "D5", "urgency")}),
    ("unknown_document_interim",
     lambda r: [make_snapshot(r, "interim", [interim_row("GHOST", {"sender": ("gap_fill", "Boo")})],
                              snapshot_id="in-extra")],
     {("unknown_document", "GHOST", None)}),
    ("duplicate_label_across_interim_ranks",
     lambda r: [make_snapshot(r, "interim", [interim_row("D4", {"recipient": ("gap_fill", "A")}),
                                            interim_row("D4", {"recipient": ("correction", "B")})],
                              snapshot_id="in-extra")],
     {("duplicate_label", "D4", "recipient")}),
]


def test_clean_set_has_no_findings(tmp_path: Path) -> None:
    report, loaded = check_dirs(standard_set(tmp_path), profile())
    assert report.findings == ()
    assert verify(report) == 0
    assert {s.snapshot_id for s in loaded} == BASE_IDS


@pytest.mark.parametrize(("name", "build", "expected"), CASES, ids=[c[0] for c in CASES])
def test_failure_state_is_reported_and_neighbours_still_load(tmp_path: Path, name, build, expected) -> None:
    prof = profile()
    base = standard_set(tmp_path)
    report, loaded = check_dirs([*base, *build(tmp_path)], prof)
    assert {(f.kind, f.doc, f.field) for f in report.findings} == expected
    assert all(f.severity == SEVERITY[f.kind] for f in report.findings)
    assert all(f.detail or f.location for f in report.findings)
    has_error = any(SEVERITY[k] == "error" for k, _, _ in expected)
    assert verify(report) == (1 if has_error else 0)
    # the valid neighbours were loaded and still resolve
    assert BASE_IDS <= {s.snapshot_id for s in loaded}
    read = read_layers(loaded)
    docs = {d.document_id: d for d in resolve(read.records, prof, corpus_index(read.corpus))}
    assert docs["D3"].fields["entity"].value == "Acme"
    assert docs["D2"].fields["sender"].value == "Cy"


def test_every_finding_kind_has_a_failure_state_test() -> None:
    covered = {k for _, _, expected in CASES for k, _, _ in expected} | {"missing_hub"}
    assert covered == set(SEVERITY)


def test_missing_hub_is_an_error(tmp_path: Path) -> None:
    base = standard_set(tmp_path)
    report, loaded = check_dirs([d for d in base if d.name != "hub-test"], profile())
    assert [f.kind for f in report.findings] == ["missing_hub"]
    assert verify(report) == 1
    assert len(loaded) == 3


def test_corrupt_line_location_names_file_and_line(tmp_path: Path) -> None:
    report, _ = check_dirs([*standard_set(tmp_path), *corrupt(tmp_path)], profile())
    (f,) = report.findings
    assert f.location == "rv-extra/rows.jsonl:2"


def test_many_problems_are_all_reported_in_one_run(tmp_path: Path) -> None:
    extra = ng(tmp_path, [
        near_gold_row("D4", {"urgency": "panic", "colour": "red", "sent_on": 5}),
        near_gold_row("GHOST", {"sender": "Boo"}),
    ])
    report, _ = check_dirs([*standard_set(tmp_path), extra], profile())
    assert report.kinds() == {"vocabulary_violation": 1, "unknown_field": 1, "type_mismatch": 1, "unknown_document": 1}
    assert [f.severity for f in report.findings] == ["error"] * 4


def test_known_warnings_are_marked_and_never_change_the_verdict(tmp_path: Path) -> None:
    known = ({"kind": "orphan_decision", "doc": "D4", "field": "recipient"},)
    extra = make_snapshot(tmp_path, "review", [review_row("D4", "recipient", "Eve"), review_row("D3", "filing_id", "F1")],
                          snapshot_id="rv-extra", known_warnings=known)
    report, _ = check_dirs([*standard_set(tmp_path), extra], profile())
    assert sorted((f.doc, f.known) for f in report.findings) == [("D3", False), ("D4", True)]
    assert verify(report) == 0


def test_errors_sort_before_warnings(tmp_path: Path) -> None:
    extra = ng(tmp_path, [near_gold_row("D4", {"recipient": "X", "urgency": "panic"}, sha="0" * 16)])
    report, _ = check_dirs([*standard_set(tmp_path), extra], profile())
    assert [f.severity for f in report.findings] == ["error", "warning"]
    assert json.loads(report.findings[0].model_dump_json())["kind"] == "vocabulary_violation"


@pytest.mark.parametrize(
    ("ftype", "value", "ok"),
    [
        ("name", "Ann", True), ("name", 3, False), ("name", None, True), ("label", ["a"], False),
        ("date", "2020-01-01", True), ("date", 20200101, False),
        ("money", 12.5, True), ("money", 3, True), ("money", "$3", True), ("money", True, False),
        ("money", float("nan"), False), ("money", float("inf"), False), ("money", [1], False),
        ("entity_list", ["a", "b"], True), ("entity_list:name", [], True), ("entity_list:name", ["a", 1], False),
        ("entity_list:free_text", "a", False), ("cms_doc_ref", "NPI 1", True), ("containment", {"a": 1}, False),
        ("no_such_type", "x", False),
    ],
)
def test_value_matches_type(ftype: str, value: object, ok: bool) -> None:
    assert value_matches_type(ftype, value) is ok


def test_a_file_changed_after_load_is_reported_not_raised(tmp_path: Path) -> None:
    dirs = standard_set(tmp_path)
    loaded = load_snapshot_set(dirs)
    path = dirs[2] / "rows.jsonl"
    path.write_bytes(path.read_bytes().replace(b"Cyrus", b"Cyril"))
    report = check(loaded, profile())
    assert ("manifest_mismatch", "near_gold") in {(f.kind, f.layer) for f in report.findings}
    assert verify(report) == 1


def test_nan_and_deep_nesting_are_reported_not_raised(tmp_path: Path) -> None:
    base = standard_set(tmp_path)
    d = make_snapshot(tmp_path, "near_gold", [near_gold_row("D4", {"recipient": "X"})], snapshot_id="ng-nan")
    with (d / "rows.jsonl").open("ab") as fh:
        fh.write(b'{"doc_id": "D4", "value": NaN}\n')
        fh.write(b'{"doc_id": ' + b"[" * 100000 + b"]" * 100000 + b"}\n")
    rehash_manifest(d)
    report, loaded = check_dirs([*base, d], profile())
    assert [f.kind for f in report.findings].count("corrupt_line") == 2
    read = read_layers(loaded)
    docs = {x.document_id: x for x in resolve(read.records, profile(), corpus_index(read.corpus))}
    assert docs["D4"].fields["recipient"].value == "X"  # the valid neighbour line still resolves
