"""``load_label_set``: the set-level surface the Dojo codes against."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from labels_testkit import make_snapshot, near_gold_row, profile, standard_set

from mailroom_labels import LabelSet, QualityGateFailed, load_label_set
from mailroom_labels.models import DocumentLabels
from mailroom_labels.snapshot import load_snapshot
from mailroom_record.hashing import hash_obj
from mailroom_record.models import Run


def test_label_set_resolves_documents_and_names_its_members(tmp_path: Path) -> None:
    dirs = standard_set(tmp_path)
    ls = load_label_set(dirs, profile())
    assert isinstance(ls, LabelSet)
    docs = list(ls.documents())
    assert [d.document_id for d in docs] == ["D1", "D2", "D3", "D4"]
    assert all(isinstance(d, DocumentLabels) for d in docs)
    assert ls.document("D1").fields["sent_on"].status == "gold"
    assert ls.document("NOPE") is None
    assert ls.snapshot_id == "hub-test+interim-test+near_gold-test+review-test"
    members = sorted((load_snapshot(d) for d in dirs), key=lambda s: s.snapshot_id)
    assert ls.manifest_hash == hash_obj([[s.snapshot_id, s.manifest_hash] for s in members])
    assert [r["snapshot_id"] for r in ls.snapshot_refs()] == [s.snapshot_id for s in members]
    assert ls.report.ok


def test_label_set_is_deterministic_and_order_free(tmp_path: Path) -> None:
    dirs = standard_set(tmp_path)
    a, b = load_label_set(dirs, profile()), load_label_set(list(reversed(dirs)), profile())
    assert (a.snapshot_id, a.manifest_hash, list(a.documents())) == (b.snapshot_id, b.manifest_hash, list(b.documents()))


def test_manifest_hash_changes_with_any_member(tmp_path: Path) -> None:
    dirs = standard_set(tmp_path)
    before = load_label_set(dirs, profile()).manifest_hash
    shutil.rmtree(dirs[2])
    make_snapshot(tmp_path, "near_gold", [near_gold_row("D4", {"sender": "Dee!"})])
    assert load_label_set(dirs, profile()).manifest_hash != before


def test_label_set_refuses_a_set_with_an_error(tmp_path: Path) -> None:
    dirs = standard_set(tmp_path)
    bad = make_snapshot(tmp_path, "near_gold", [near_gold_row("D4", {"urgency": "panic"})], snapshot_id="ng-x")
    with pytest.raises(QualityGateFailed) as info:
        load_label_set([*dirs, bad], profile())
    assert [f.kind for f in info.value.report.errors] == ["vocabulary_violation"]
    (dirs[0] / "rows.jsonl").unlink()
    with pytest.raises(QualityGateFailed):
        load_label_set(dirs, profile())


def test_label_snapshot_ids_fit_the_run_header(tmp_path: Path) -> None:
    ls = load_label_set(standard_set(tmp_path), profile())
    ids = ls.label_snapshot_ids()
    assert ids == sorted(ids) and all("@" in i for i in ids)
    assert ids[0] == f"hub-test@{load_snapshot(tmp_path / 'hub-test').manifest_hash}"
    assert Run.model_fields["label_snapshot_ids"].annotation == list[str]


def test_strict_label_set_refuses_a_warning_its_snapshot_does_not_accept(tmp_path: Path) -> None:
    from labels_testkit import review_row

    stray = make_snapshot(tmp_path, "review", [review_row("D3", "filing_id", "F1")], snapshot_id="rv-extra")
    dirs = [*standard_set(tmp_path), stray]
    assert load_label_set(dirs, profile()).report.unknown_warnings  # lenient: loads, warning visible
    with pytest.raises(QualityGateFailed, match="1 unknown warning") as info:
        load_label_set(dirs, profile(), strict=True)
    assert [f.kind for f in info.value.report.unknown_warnings] == ["orphan_decision"]
    accepted = ({"kind": "orphan_decision", "doc": "D3", "field": "filing_id", "reason": "added by review"},)
    ok = make_snapshot(tmp_path / "ok", "review", [review_row("D3", "filing_id", "F1")], snapshot_id="rv-extra",
                       known_warnings=accepted)
    assert load_label_set([*standard_set(tmp_path / "ok"), ok], profile(), strict=True).report.unknown_warnings == ()
