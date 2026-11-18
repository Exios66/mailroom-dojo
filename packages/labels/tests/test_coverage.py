"""Coverage: correct on the clean synthetic set (answer computed by hand from
``labels_testkit.standard_set``) and byte-stable whatever the input order."""

from __future__ import annotations

import json
from pathlib import Path

from labels_testkit import make_snapshot, near_gold_row, profile

from mailroom_labels.coverage import coverage, render_markdown
from mailroom_labels.layers import corpus_index, read_layers
from mailroom_labels.models import RANKS, STATUS_WORDS
from mailroom_labels.resolve import resolve
from mailroom_labels.snapshot import load_snapshot_set, snapshot_refs


def docs_and_refs(dirs):
    snaps = load_snapshot_set(dirs)
    read = read_layers(snaps)
    return resolve(read.records, profile(), corpus_index(read.corpus)), snapshot_refs(snaps)


def cell(scorable: int = 0, status: dict | None = None, rank: dict | None = None) -> dict:
    return {
        "scorable": scorable,
        "by_status": {s: (status or {}).get(s, 0) for s in STATUS_WORDS},
        "by_rank": {r: (rank or {}).get(r, 0) for r in RANKS},
    }


# Worked out by hand from standard_set (see the worked example in test_resolve.py).
EXPECTED_CLASSES = {
    "filing": {
        "in_profile": True, "documents": 1, "verified_documents": 1, "scorable_documents": 1,
        "fields": {
            "entity": cell(1, {"hub": 1}, {"hub": 1}),
            "filing_id": cell(0),
            "kind": cell(1, {"hub": 1}, {"hub": 1}),
            "signers": cell(1, {"interim": 1}, {"interim:gap_fill": 1}),
        },
    },
    "letter": {
        "in_profile": True, "documents": 3, "verified_documents": 3, "scorable_documents": 3,
        "fields": {
            "action_items": cell(1, {"near_gold": 1}, {"review:near_gold": 1}),
            "amount": cell(1, {"hub": 1}, {"hub": 1}),
            "recipient": cell(1, {"interim": 1}, {"interim:correction": 1}),
            "sender": cell(3, {"hub": 1, "interim": 1, "near_gold": 1},
                           {"hub": 1, "interim:gap_fill": 1, "near_gold": 1}),
            "sent_on": cell(1, {"gold": 1}, {"review:gold": 1}),
            "urgency": cell(1, {"hub": 1}, {"hub": 1}),
        },
    },
}


def test_coverage_matches_the_hand_computed_answer(snapshot_dirs) -> None:
    docs, refs = docs_and_refs(snapshot_dirs)
    report = coverage(docs, profile(), refs)
    assert report["totals"] == {"documents": 4, "verified_documents": 4, "scorable_documents": 4, "scorable_labels": 11}
    assert report["classes"] == EXPECTED_CLASSES
    assert [s["snapshot_id"] for s in report["snapshots"]] == ["hub-test", "interim-test", "near_gold-test", "review-test"]


def test_unverified_and_unknown_documents_are_counted_not_scored(tmp_path: Path, snapshot_dirs) -> None:
    extra = make_snapshot(tmp_path, "near_gold", [near_gold_row("GHOST", {"sender": "Boo"})], snapshot_id="ng-x")
    docs, refs = docs_and_refs([*snapshot_dirs, extra])
    report = coverage(docs, profile(), refs)
    letter = report["classes"]["letter"]
    assert (letter["documents"], letter["verified_documents"], letter["scorable_documents"]) == (4, 3, 3)
    assert letter["fields"]["sender"]["scorable"] == 3


def test_a_class_outside_the_profile_is_listed(snapshot_dirs) -> None:
    docs, _ = docs_and_refs(snapshot_dirs)
    stranger = docs[0].model_copy(update={"document_id": "Z9", "doc_class": "mystery", "scorable_fields": frozenset()})
    report = coverage([*docs, stranger], profile())
    assert report["classes"]["mystery"] == {
        "in_profile": False, "documents": 1, "verified_documents": 1, "scorable_documents": 0, "fields": {},
    }
    assert list(report["classes"]) == ["filing", "letter", "mystery"]


def test_report_is_byte_stable_whatever_the_input_order(snapshot_dirs) -> None:
    docs, refs = docs_and_refs(snapshot_dirs)
    one = coverage(docs, profile(), refs)
    two = coverage(list(reversed(docs)), profile(), tuple(reversed(refs)))
    assert json.dumps(one) == json.dumps(two)  # key order included, not only equality
    assert render_markdown(one) == render_markdown(two)


def test_markdown_rows(snapshot_dirs) -> None:
    docs, refs = docs_and_refs(snapshot_dirs)
    md = render_markdown(coverage(docs, profile(), refs))
    assert "Documents: 4; verified: 4; with a scorable field: 4; scorable labels: 11." in md
    assert md.index("## filing") < md.index("## letter")
    # | field | scorable | hub interim near_gold gold | ranks in RANKS order |
    assert "| sender | 3 | 1 | 1 | 1 | 0 | 0 | 0 | 1 | 0 | 1 | 0 | 0 | 1 |" in md
    assert "| filing_id | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |" in md


def test_markdown_escapes_a_pipe_in_a_field_name(snapshot_dirs) -> None:
    docs, refs = docs_and_refs(snapshot_dirs)
    report = coverage(docs, profile(), refs)
    cls = next(iter(report["classes"].values()))
    first = next(iter(cls["fields"]))
    cls["fields"] = {"a|b" if k == first else k: v for k, v in cls["fields"].items()}
    md = render_markdown(report)
    row = next(line for line in md.splitlines() if line.startswith("| a"))
    assert row.startswith("| a\\|b |")
    header = next(line for line in md.splitlines() if line.startswith("| Field"))
    assert row.replace("\\|", "").count("|") == header.count("|")
