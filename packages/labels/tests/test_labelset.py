"""``load_label_set``: the set-level surface the Dojo codes against."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from labels_testkit import hub_row, interim_row, make_snapshot, near_gold_row, profile, standard_set
from mailroom_labels import LabelSet, QualityGateFailed, corpus_index, load_label_set, read_layers, resolve
from mailroom_labels.models import DocumentLabels
from mailroom_labels.quality import QualityReport
from mailroom_labels.snapshot import load_snapshot, load_snapshot_set
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
    assert (a.snapshot_id, a.manifest_hash, list(a.documents())) == (
        b.snapshot_id,
        b.manifest_hash,
        list(b.documents()),
    )


def test_manifest_hash_changes_with_any_member(tmp_path: Path) -> None:
    dirs = standard_set(tmp_path)
    before = load_label_set(dirs, profile()).manifest_hash
    shutil.rmtree(dirs[2])
    make_snapshot(  # the standard near_gold rows with one value changed: still strict-clean
        tmp_path,
        "near_gold",
        [
            near_gold_row("D1", {"sent_on": "2020-01-02", "sender": "Ann C."}),
            near_gold_row("D2", {"action_items": ["call back"], "sender": "Cyrus"}),
            near_gold_row("D4", {"sender": "Dee!"}),
        ],
    )
    assert load_label_set(dirs, profile()).manifest_hash != before


def test_label_set_refuses_a_set_with_an_error(tmp_path: Path) -> None:
    dirs = standard_set(tmp_path)
    bad = make_snapshot(
        tmp_path, "near_gold", [near_gold_row("D4", {"urgency": "panic"})], snapshot_id="ng-x"
    )
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
    # lenient only when asked: loads, the warning stays visible
    assert load_label_set(dirs, profile(), strict=False).report.unknown_warnings
    for kwargs in ({}, {"strict": True}):  # strict is the default
        with pytest.raises(QualityGateFailed, match="1 unknown warning") as info:
            load_label_set(dirs, profile(), **kwargs)
        assert [f.kind for f in info.value.report.unknown_warnings] == ["orphan_decision"]
    accepted = ({"kind": "orphan_decision", "doc": "D3", "field": "filing_id", "reason": "added by review"},)
    ok = make_snapshot(
        tmp_path / "ok",
        "review",
        [review_row("D3", "filing_id", "F1")],
        snapshot_id="rv-extra",
        known_warnings=accepted,
    )
    assert (
        load_label_set([*standard_set(tmp_path / "ok"), ok], profile(), strict=True).report.unknown_warnings
        == ()
    )


def test_a_file_changed_after_the_gate_raises_the_documented_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mailroom_labels.labelset as labelset_module
    from mailroom_labels.errors import ManifestMismatch
    from mailroom_labels.quality import check_dirs as real_check_dirs

    dirs = standard_set(tmp_path)

    def gate_then_tamper(directories, prof):
        result = real_check_dirs(directories, prof)
        target = tmp_path / "near_gold-test" / "rows.jsonl"
        target.write_bytes(target.read_bytes().replace(b"near_gold", b"near_g0ld", 1))
        return result

    monkeypatch.setattr(labelset_module, "check_dirs", gate_then_tamper)
    with pytest.raises(QualityGateFailed) as info:
        load_label_set(dirs, profile())
    assert isinstance(info.value.__cause__, ManifestMismatch)
    assert info.value.report.errors[0].kind == "manifest_mismatch"


def test_resolved_labels_cannot_be_changed_after_the_gate(tmp_path: Path) -> None:
    doc = load_label_set(standard_set(tmp_path), profile()).document("D1")
    with pytest.raises(TypeError):
        doc.fields["sender"] = doc.fields["urgency"]  # type: ignore[index]
    with pytest.raises(TypeError):
        del doc.fields["sender"]  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        doc.untrusted["x"] = ()  # type: ignore[index]
    with pytest.raises(ValidationError):  # frozen model
        doc.fields["sender"].value = "Mallory"  # type: ignore[misc]
    assert isinstance(doc.model_dump()["fields"], dict) and doc == doc.model_copy()
    assert DocumentLabels.model_validate_json(doc.model_dump_json()) == doc


def test_the_default_refuses_a_hub_snapshot_whose_text_was_not_checked(tmp_path: Path) -> None:
    # A Hub import without --default-dir only warns text_unchecked; the Dojo's loader must
    # not score against it unless that warning is accepted in the snapshot's manifest.
    from labels_testkit import hub_row

    dirs = standard_set(tmp_path)
    shutil.rmtree(dirs[0])
    rows = [hub_row("D1", fields={"sender": "Ann", "recipient": None, "urgency": "routine"})]
    make_snapshot(tmp_path, "hub", rows, source={"repository": "synthetic", "text_check": {"run": False}})
    with pytest.raises(QualityGateFailed, match="text_unchecked"):
        load_label_set([dirs[0]], profile())
    assert load_label_set([dirs[0]], profile(), strict=False).report.unknown_warnings


def test_a_manifest_replaced_after_the_gate_raises_the_documented_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mailroom_labels.labelset as labelset_module
    from labels_testkit import rehash_manifest
    from mailroom_labels.quality import check_dirs as real_check_dirs

    dirs = standard_set(tmp_path)
    target = tmp_path / "near_gold-test"

    def gate_then_replace_manifest(directories, prof):
        result = real_check_dirs(directories, prof)
        rehash_manifest(target, lambda m: m["source"].update(note="swapped after the gate"))
        return result

    monkeypatch.setattr(labelset_module, "check_dirs", gate_then_replace_manifest)
    with pytest.raises(QualityGateFailed) as info:
        load_label_set(dirs, profile())
    assert info.value.report.errors[0].kind == "manifest_mismatch"
    assert "near_gold-test" in (info.value.report.errors[0].location or "")


def _scribble(value: object) -> None:
    """Change a JSON container in place, at every depth."""
    if isinstance(value, list):
        for item in value:
            _scribble(item)
        value.append("MALLORY")
        value.reverse()
    elif isinstance(value, dict):
        for item in value.values():
            _scribble(item)
        value["mallory"] = True
        value.pop(next(iter(value)))


def _every_value(doc: DocumentLabels) -> list[object]:
    facts = [f for fl in doc.fields.values() for f in fl.layers_seen]
    facts += [f for seen in doc.untrusted.values() for f in seen]
    return [fl.value for fl in doc.fields.values()] + [f.value for f in facts]


def test_nested_values_handed_out_cannot_change_the_stored_labels(tmp_path: Path) -> None:
    """A caller that mutates a list or dict it was given changes its own copy, never the
    label set: ``documents()`` and ``document()`` hand out the same values every time."""
    gated = load_label_set(standard_set(tmp_path / "std"), profile())  # D3.signers is a list
    # A dict value cannot pass the gate (no field type holds one), so build that set from
    # resolve() directly: a nested dict, a dict in a list, and an untrusted dict.
    root = tmp_path / "dicts"
    hub = make_snapshot(
        root, "hub", [hub_row("D1", fields={"amount": {"currency": "XTS", "parts": [1, {"n": 2}]}})]
    )
    interim = make_snapshot(root, "interim", [interim_row("D1", {"sent_on": ("disputed", {"a": [1]})})])
    read = read_layers(load_snapshot_set([hub, interim]))
    built = LabelSet(
        snapshots=(),
        profile=profile(),
        report=QualityReport(()),
        _documents=resolve(read.records, profile(), corpus_index(read.corpus)),
    )
    for ls, doc_id in ((gated, "D3"), (built, "D1")):
        before = [d.model_dump(mode="json") for d in ls.documents()]
        values = [v for d in ls.documents() for v in _every_value(d)]
        assert any(isinstance(v, list) for v in values) or ls is built
        assert any(isinstance(v, dict) for v in values) or ls is gated
        for v in values:
            _scribble(v)
        one = ls.document(doc_id)
        assert one is not None
        for v in _every_value(one):
            _scribble(v)
        assert [d.model_dump(mode="json") for d in ls.documents()] == before
        assert ls.document(doc_id).model_dump(mode="json") in before
