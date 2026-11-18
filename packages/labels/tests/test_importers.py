"""Importers on small synthetic sources in the native shapes: mapping, refusals, determinism,
``--check``, and the review merge rules. No network; class and field names are the
synthetic profile's."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from labels_testkit import PROFILE_PATH, interim_row, near_gold_row, profile
from mailroom_labels.cli import app
from mailroom_labels.importers import (
    ImportRefused,
    build_hub,
    build_interim,
    build_near_gold,
    build_review,
    check_snapshot,
)
from mailroom_labels.importers.common import known_warnings_for
from mailroom_labels.layers import corpus_index, read_layer, read_layers
from mailroom_labels.quality import check_dirs
from mailroom_labels.resolve import resolve
from mailroom_labels.snapshot import load_snapshot

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
runner = CliRunner()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- a synthetic Hub dataset ------------------------------------------------------------


def gt_row(
    doc: str,
    cls: str,
    sub: str | None,
    split: str,
    gt: dict[str, Any],
    presence: dict[str, str],
    *,
    content: str | None = None,
) -> dict[str, Any]:
    return {
        "filename": f"{doc}.txt",
        "expected": cls,
        "expected_subclass": sub or "",
        "split": split,
        "document_id": doc,
        "content_sha256": content if content is not None else sha(f"text-{doc}".encode()),
        "gt_fields": json.dumps({**gt, "gt_presence": json.dumps(presence)}),
        "annotation_confidence": "0.9",
    }


GT_TRAIN = [
    gt_row(
        "D1",
        "letter",
        "email",
        "train",
        {
            "sender": "Ann",
            "recipient": "",
            "amount": "",
            "action_items": '["call back", "pay"]',
            "kind": "",
            "token_estimate": "120",
        },
        {
            "sender": "populated",
            "recipient": "schema_documented_absence",
            "amount": "not_applicable",
            "action_items": "populated",
            "kind": "not_applicable",
        },
    ),
    gt_row(
        "D2",
        "letter",
        "memo",
        "train",
        {"sender": "", "urgency": "routine"},
        {"sender": "pending_annotation", "urgency": "populated"},
    ),
]
GT_TEST = [
    gt_row(
        "F1",
        "filing",
        "annual",
        "test",
        {"entity": "Acme", "signers": "not a list"},
        {"entity": "populated", "signers": "populated"},
    ),
    gt_row(
        "D3", "letter", "email", "test", {"sender": "Bo"}, {"sender": "populated"}, content=sha(b"other text")
    ),
]
TEXT = {f"{d}.txt": f"text-{d}" for d in ("D1", "D2", "F1", "D3")}


def write_dataset(
    root: Path,
    *,
    tamper: str | None = None,
    unlisted: bool = False,
    extra: list | None = None,
    extra_text: list | None = None,
) -> Path:
    files: dict[str, bytes] = {}
    for config, split, rows in (
        ("ground_truth", "train", GT_TRAIN + list(extra or ())),
        ("ground_truth", "test", GT_TEST),
        (
            "default",
            "train",
            [{"filename": f, "doc_text": t} for f, t in TEXT.items() if f.startswith(("D1", "D2"))]
            + list(extra_text or ()),
        ),
        (
            "default",
            "test",
            [{"filename": f, "doc_text": t} for f, t in TEXT.items() if f.startswith(("F1", "D3"))],
        ),
    ):
        rel = f"parquet/{config}/{split}/{split}-00000-of-00001.parquet"
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows), path)
        files[rel] = path.read_bytes()
    listed = {rel: sha(data) for rel, data in files.items()}
    if unlisted:
        del listed["parquet/ground_truth/test/test-00000-of-00001.parquet"]
    if tamper:
        listed[tamper] = "0" * 64
    lines = ["name       : synthetic", "sha256     :"] + [
        f"  {rel}  {digest}" for rel, digest in sorted(listed.items())
    ]
    lines += ["  README.md  " + "1" * 64, "built_utc  : never"]
    (root / "manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def pins_for(data: Path, revision: str = "0" * 40) -> dict[str, dict[str, str]]:
    """Pin every file of a synthetic dataset directory, as hub-revisions.yaml does."""
    return {
        revision: {
            p.relative_to(data).as_posix(): sha(p.read_bytes()) for p in data.rglob("*") if p.is_file()
        }
    }


def hub_build(out: Path, data: Path, *, default: bool = True, **kw):
    kw.setdefault("pins", pins_for(data))
    return build_hub(
        out,
        dataset_dir=data,
        profile=profile(),
        profile_sha256=sha(PROFILE_PATH.read_bytes()),
        repository="example/synthetic",
        revision="0" * 40,
        default_dir=data if default else None,
        **kw,
    )


def hub_rows(directory: Path) -> dict[str, dict]:
    return {r.data["document_id"]: r.data for r in load_snapshot(directory).rows()}


def test_hub_mapping_reproduces_the_sandbox_rules(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data")
    manifest = hub_build(tmp_path / "hub-x", data)
    rows = hub_rows(tmp_path / "hub-x")
    d1 = rows["D1"]["fields"]
    assert d1["sender"] == {"value": "Ann", "presence": "populated"}
    assert d1["recipient"] == {"value": "", "presence": "schema_documented_absence"}  # raw value kept
    assert d1["amount"] == {
        "value": None,
        "presence": "not_applicable",
    }  # a class field: kept, placeholder nulled
    assert d1["action_items"] == {"value": ["call back", "pay"], "presence": "populated"}  # JSON list decoded
    assert "kind" not in d1  # another class's not_applicable placeholder: omitted
    assert "token_estimate" not in d1  # metadata excluded
    assert rows["D2"]["fields"]["sender"]["presence"] == "unlabeled"  # pending_annotation
    assert rows["F1"]["fields"]["signers"]["value"] == "not a list"  # never repaired
    assert rows["D1"]["subclass"] == "email" and rows["D1"]["split"] == "train"
    src = manifest.source
    assert src["excluded_metadata_keys"] == {"token_estimate": 1}
    assert src["omitted_not_applicable_placeholders"] == {"letter": {"kind": 1}}
    assert src["presence_mapped"] == {"pending_annotation->unlabeled": 1}
    assert src["decoded_json_lists"] == {"action_items": 1}
    assert src["undecodable_list_values"] == {"signers": 1}
    assert src["not_applicable_placeholders_nulled"] == {"amount=''": 1}
    assert src["text_unverified_documents"] == ["D3"]
    assert src["text_check"]["unverified_by_reason"] == {"hash_mismatch": 1}
    assert [f["path"] for f in src["files"]][:2] == [
        "parquet/ground_truth/train/train-00000-of-00001.parquet",
        "parquet/ground_truth/test/test-00000-of-00001.parquet",
    ]
    read = read_layer(load_snapshot(tmp_path / "hub-x"))
    entry = corpus_index(read.corpus)["D3"]
    assert not entry.text_verified and corpus_index(read.corpus)["D1"].text_verified


def test_hub_out_of_class_not_applicable_placeholders_are_omitted_and_counted(tmp_path: Path) -> None:
    GT_TEST.append(
        gt_row(
            "F2",
            "filing",
            "annual",
            "test",
            {"entity": "B", "sender": ""},
            {"entity": "populated", "sender": "not_applicable"},
        )
    )
    try:
        data = write_dataset(tmp_path / "data2")
        manifest = hub_build(tmp_path / "hub-x", data, default=False)
    finally:
        GT_TEST.pop()
    assert "sender" not in hub_rows(tmp_path / "hub-x")["F2"]["fields"]
    assert manifest.source["omitted_not_applicable_placeholders"] == {
        "filing": {"sender": 1},
        "letter": {"kind": 1},
    }
    assert manifest.source["text_check"] == {"run": False}


@pytest.mark.parametrize(
    "tamper",
    [
        "parquet/ground_truth/train/train-00000-of-00001.parquet",
        "parquet/default/test/test-00000-of-00001.parquet",
    ],
)
def test_hub_refuses_a_shard_whose_hash_is_not_the_manifest_one(tmp_path: Path, tamper: str) -> None:
    data = write_dataset(tmp_path / "data", tamper=tamper)
    with pytest.raises(ImportRefused, match="differs from manifest.txt"):
        hub_build(tmp_path / "hub-x", data)
    assert not (tmp_path / "hub-x").exists()


def test_hub_refuses_an_unlisted_shard(tmp_path: Path) -> None:
    with pytest.raises(ImportRefused, match="not listed"):
        hub_build(tmp_path / "hub-x", write_dataset(tmp_path / "data", unlisted=True))


def test_hub_build_is_deterministic_and_check_detects_drift(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data")
    a = hub_build(tmp_path / "a" / "hub-x", data)
    b = hub_build(tmp_path / "b" / "hub-x", data)
    assert a.manifest_hash == b.manifest_hash
    assert (tmp_path / "a/hub-x/documents.jsonl").read_bytes() == (
        tmp_path / "b/hub-x/documents.jsonl"
    ).read_bytes()
    assert check_snapshot(tmp_path / "a" / "hub-x", lambda d: hub_build(d, data)).identical
    path = tmp_path / "a/hub-x/documents.jsonl"
    path.write_bytes(path.read_bytes().replace(b'"Ann"', b'"Anne"'))
    result = check_snapshot(tmp_path / "a" / "hub-x", lambda d: hub_build(d, data))
    assert not result.identical and result.differences == ["differs from a fresh build: documents.jsonl"]


# --- overlays ---------------------------------------------------------------------------


def write_overlay(
    root: Path,
    files: dict[str, bytes],
    *,
    schema: str = "mailroom-interim-labels/v1",
    bad_hash: str | None = None,
    extra: str | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    listed = {}
    for name, data in files.items():
        (root / name).write_bytes(data)
        listed[name] = "f" * 64 if name == bad_hash else sha(data)
    if extra:
        (root / extra).write_bytes(b"{}\n")
    (root / "MANIFEST.json").write_text(json.dumps({"schema": schema, "files": listed, "built_at": "then"}))
    return root


def jsonl(*rows: Any) -> bytes:
    return b"".join((r if isinstance(r, bytes) else json.dumps(r).encode()) + b"\n" for r in rows)


def overlay(out: Path, src: Path, kind: str = "interim", **kw):
    fn = build_interim if kind == "interim" else build_near_gold
    return fn(
        out, source_dir=src, source_path="data/labels/x", repository="example/sandbox", commit="1" * 40, **kw
    )


def test_interim_rows_are_copied_unchanged_and_bad_lines_listed(tmp_path: Path) -> None:
    good = interim_row("D1", {"sender": ("gap_fill", "Ann"), "urgency": ("disputed", "urgent")})
    stale = interim_row("D2", {"sender": ("gap_fill", "Bo")}, sha="0" * 16)
    no_labels = {k: v for k, v in interim_row("D3", {}).items() if k != "labels"}
    src = write_overlay(
        tmp_path / "src", {"letter.jsonl": jsonl(good, b"{not json", stale, no_labels, b"[1]")}
    )
    with pytest.raises(ImportRefused, match="rejected_lines 2"):
        overlay(tmp_path / "interim-x", src)
    assert not (tmp_path / "interim-x").exists()  # refused: nothing written
    manifest = overlay(tmp_path / "interim-x", src, allow_rejected=True)
    rows = [r.data for r in load_snapshot(tmp_path / "interim-x").rows()]
    assert rows[0] == good  # unchanged, disputed included
    assert [r["doc_id"] for r in rows] == ["D1", "D2", "D3"]
    assert manifest.source["rejected_lines"] == [
        {"file": "letter.jsonl", "line": 2, "reason": "not JSON: JSONDecodeError"},
        {"file": "letter.jsonl", "line": 5, "reason": "not a JSON object (list)"},
    ]
    assert manifest.source["label_counts"] == {"letter": {"disputed": 1, "gap_fill": 2}}
    assert manifest.source["files"] == [
        {"path": "data/labels/x/letter.jsonl", "sha256": sha((src / "letter.jsonl").read_bytes()), "rows": 3}
    ]
    read = read_layer(load_snapshot(tmp_path / "interim-x"))
    assert {(r.document_id, r.rank) for r in read.records} == {
        ("D1", "interim:gap_fill"),
        ("D1", "interim:disputed"),
        ("D2", "interim:gap_fill"),
    }
    assert [(f.kind, f.doc) for f in read.findings] == [
        ("malformed_record", "D3")
    ]  # the missing key, reported


def test_interim_duplicate_and_stale_labels_surface_in_the_gate(tmp_path: Path) -> None:
    from labels_testkit import hub_row, make_snapshot

    hub = make_snapshot(
        tmp_path,
        "hub",
        [
            hub_row("D1", fields={"sender": ("", "unlabeled")}),
            hub_row("D2", fields={"sender": ("", "unlabeled")}),
        ],
    )
    src = write_overlay(
        tmp_path / "src",
        {
            "letter.jsonl": jsonl(
                interim_row("D1", {"sender": ("gap_fill", "Ann")}),
                interim_row("D1", {"sender": ("gap_fill", "Anna")}),
                interim_row("D2", {"sender": ("gap_fill", "Bo")}, sha="0" * 16),
            )
        },
    )
    overlay(tmp_path / "interim-x", src)
    report, loaded = check_dirs([hub, tmp_path / "interim-x"], profile())
    assert {(f.kind, f.doc) for f in report.findings} == {
        ("duplicate_label", "D1"),
        ("stale_content_hash", "D2"),
    }
    read = read_layers(loaded)
    docs = {d.document_id: d for d in resolve(read.records, profile(), corpus_index(read.corpus))}
    assert "sender" in docs["D2"].untrusted and not docs["D2"].verified  # stale: never truth


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"bad_hash": "letter.jsonl"}, "differs from MANIFEST.json"),
        ({"extra": "stray.jsonl"}, "unlisted overlay file"),
        ({"schema": "other/v9"}, "schema is not"),
    ],
)
def test_overlay_refuses_a_source_that_fails_its_own_manifest(tmp_path: Path, kw: dict, match: str) -> None:
    src = write_overlay(
        tmp_path / "src", {"letter.jsonl": jsonl(interim_row("D1", {"sender": ("gap_fill", "A")}))}, **kw
    )
    with pytest.raises(ImportRefused, match=match):
        overlay(tmp_path / "interim-x", src)
    assert not (tmp_path / "interim-x").exists()


def test_near_gold_keeps_labeler_evidence_and_basis(tmp_path: Path) -> None:
    row = near_gold_row("D1", {"sender": "Ann"})
    row["labels"]["sender"]["evidence"] = [{"start": 0, "end": 3, "text": "Ann"}]
    src = write_overlay(tmp_path / "src", {"letter.jsonl": jsonl(row)})
    overlay(tmp_path / "near-gold-x", src, kind="near_gold")
    (out,) = [r.data for r in load_snapshot(tmp_path / "near-gold-x").rows()]
    assert out == row
    (rec,) = read_layer(load_snapshot(tmp_path / "near-gold-x")).records
    assert (rec.rank, rec.basis) == ("near_gold", "near_gold:two_pass_agree")


def test_overlay_build_is_deterministic_whatever_the_source_line_order(tmp_path: Path) -> None:
    rows = [interim_row(f"D{i}", {"sender": ("gap_fill", f"S{i}")}) for i in range(5)]
    a = overlay(tmp_path / "a" / "interim-x", write_overlay(tmp_path / "s1", {"letter.jsonl": jsonl(*rows)}))
    b = overlay(
        tmp_path / "b" / "interim-x", write_overlay(tmp_path / "s2", {"letter.jsonl": jsonl(*reversed(rows))})
    )
    assert (tmp_path / "a/interim-x/letter.jsonl").read_bytes() == (
        tmp_path / "b/interim-x/letter.jsonl"
    ).read_bytes()
    assert a.files == b.files


# --- review -----------------------------------------------------------------------------


def decision(doc: str, field: str, value: Any, *, human: bool = False, evidence: bool = True) -> dict:
    entry: dict[str, Any] = {"value": value}
    if evidence:
        entry["evidence"] = [f"quote for {value}"]
    return {
        "doc_id": doc,
        "field": field,
        "verdict": "correct",
        "reviewer": {"model": "human", "role": "owner_review_desk"}
        if human
        else {"model": "claude-opus", "role": "audit"},
        "note": "synthetic",
        "entry": entry,
    }


def desk(
    doc: str, field: str, value: Any, decision_: str = "correct", at: str | None = "2026-11-17T18:00:00Z"
) -> dict:
    return {
        "catalog": "letter",
        "doc_id": doc,
        "filename": f"{doc}.txt",
        "field": field,
        "decision": decision_,
        "value": value,
        "label_before_review": "old",
        "claude_verdict": "needs_human",
        "note": None,
        "reviewed_at": at,
        "source": "review_desk",
    }


def review(
    tmp_path: Path, decisions: list, desks: list, name: str = "gold-review-x", *, bom: bool = False, **kw
):
    dpath, kpath = tmp_path / "decisions.jsonl", tmp_path / "desk.jsonl"
    dpath.write_bytes(jsonl(*decisions))
    kpath.write_bytes((b"\xef\xbb\xbf" if bom else b"") + jsonl(*desks))
    manifest = build_review(
        tmp_path / name,
        decisions=[(dpath, "review/decisions.jsonl")],
        desk=[(kpath, "sandbox/human_reviews.jsonl")],
        provenance={"origin": "synthetic"},
        **kw,
    )
    rows = [r.data for r in load_snapshot(tmp_path / name).rows()]
    return manifest, rows


def test_desk_decisions_are_gold_and_map_their_verdicts(tmp_path: Path) -> None:
    manifest, rows = review(
        tmp_path,
        [],
        [
            desk("D1", "sender", "Ann", "keep"),
            desk("D2", "sender", "Bo", "use_suggestion"),
            desk("D3", "sender", "Cy", "unsure"),
        ],
    )
    assert [(r["doc_id"], r["verdict"], r["reviewer"]) for r in rows] == [
        ("D1", "accept", {"model": "human", "role": "review_desk"}),
        ("D2", "correct", {"model": "human", "role": "review_desk"}),
    ]
    assert (
        rows[0]["origin"] == ["sandbox/human_reviews.jsonl:1"]
        and rows[0]["reviewed_at"] == "2026-11-17T18:00:00Z"
    )
    assert [(n["doc_id"], n["decision"]) for n in manifest.source["undecided"]] == [("D3", "unsure")]
    assert manifest.source["not_imported"] == []
    recs = read_layer(load_snapshot(tmp_path / "gold-review-x")).records
    assert {r.rank for r in recs} == {"review:gold"}


def test_later_reviewed_at_wins_and_the_earlier_is_listed(tmp_path: Path) -> None:
    manifest, rows = review(
        tmp_path,
        [],
        [
            desk("D1", "sender", "Old", at="2026-11-17T10:00:00Z"),
            desk("D1", "sender", "New", at="2026-11-17T12:00:00Z"),
        ],
    )
    assert [r["entry"]["value"] for r in rows] == ["New"]
    (sup,) = manifest.source["superseded"]
    assert (sup["value"], sup["reason"], sup["origin"]) == (
        "Old",
        "a later reviewed_at decision on the same label",
        ["sandbox/human_reviews.jsonl:1"],
    )


def test_human_beats_claude_on_the_same_label(tmp_path: Path) -> None:
    manifest, rows = review(
        tmp_path, [decision("D1", "sender", "Claude says")], [desk("D1", "sender", "Owner says")]
    )
    assert [(r["reviewer"]["model"], r["entry"]["value"]) for r in rows] == [("human", "Owner says")]
    (sup,) = manifest.source["superseded"]
    assert (sup["reviewer"]["model"], sup["reason"]) == ("claude-opus", "a human decision on the same label")


def test_equal_decisions_from_two_files_merge_with_both_origins_and_the_evidence(tmp_path: Path) -> None:
    manifest, rows = review(
        tmp_path, [decision("D1", "sender", "Ann", human=True)], [desk("D1", "sender", "Ann")]
    )
    (row,) = rows
    assert row["origin"] == ["review/decisions.jsonl:1", "sandbox/human_reviews.jsonl:1"]
    assert row["entry"] == {"value": "Ann", "evidence": ["quote for Ann"]}
    assert row["reviewed_at"] == "2026-11-17T18:00:00Z"
    assert manifest.source["merged_duplicates"] == {"human": 1} and manifest.source["superseded"] == []


def test_undated_conflicting_human_decisions_are_kept_for_the_gate(tmp_path: Path) -> None:
    _, rows = review(
        tmp_path, [decision("D1", "sender", "A", human=True), decision("D1", "sender", "B", human=True)], []
    )
    assert sorted(r["entry"]["value"] for r in rows) == ["A", "B"]
    from labels_testkit import hub_row, make_snapshot

    hub = make_snapshot(tmp_path, "hub", [hub_row("D1", fields={"sender": "Hub"})])
    report, _ = check_dirs([hub, tmp_path / "gold-review-x"], profile())
    assert {f.kind for f in report.errors} >= {"conflicting_decision"}


def test_claude_duplicates_merge_and_bad_lines_are_rejected(tmp_path: Path) -> None:
    missing = decision("D2", "sender", "X")
    del missing["reviewer"]
    manifest, rows = review(
        tmp_path,
        [decision("D1", "sender", "Ann"), decision("D1", "sender", "Ann", evidence=False), missing, b"{oops"],
        [{k: v for k, v in desk("D3", "sender", "Y").items() if k != "reviewed_at"}],
        allow_rejected=True,
    )
    assert len(rows) == 1 and rows[0]["origin"] == ["review/decisions.jsonl:1", "review/decisions.jsonl:2"]
    assert manifest.source["merged_duplicates"] == {"other": 1}
    assert manifest.source["rejected_lines"] == [
        {"file": "review/decisions.jsonl", "line": 4, "reason": "not JSON: JSONDecodeError"},
        {"file": "review/decisions.jsonl", "line": 3, "reason": "missing key(s) ['reviewer']"},
        {"file": "sandbox/human_reviews.jsonl", "line": 1, "reason": "missing key(s) ['reviewed_at']"},
    ]


def test_known_warnings_need_a_reason(tmp_path: Path) -> None:
    path = tmp_path / "kw.yaml"
    path.write_text("snap-a:\n- {kind: orphan_decision, doc: D1, field: sender, reason: 'added by review'}\n")
    assert known_warnings_for(path, "snap-a") == [
        {"kind": "orphan_decision", "doc": "D1", "field": "sender", "reason": "added by review"}
    ]
    assert known_warnings_for(path, "snap-b") == []
    path.write_text("snap-a:\n- {kind: orphan_decision, doc: D1, field: sender}\n")
    with pytest.raises(ImportRefused, match="reason"):
        known_warnings_for(path, "snap-a")


def test_a_known_warning_with_a_file_keeps_it(tmp_path: Path) -> None:
    path = tmp_path / "kw.yaml"
    path.write_text(
        "snap-a:\n- {kind: orphan_decision, doc: D1, field: sender, reason: r, file: decisions.jsonl}\n"
    )
    assert known_warnings_for(path, "snap-a") == [
        {"kind": "orphan_decision", "doc": "D1", "field": "sender", "reason": "r", "file": "decisions.jsonl"}
    ]


def test_an_overlay_manifest_with_a_utf8_bom_is_read(tmp_path: Path) -> None:
    good = interim_row("D1", {"sender": ("gap_fill", "Ann")})
    src = write_overlay(tmp_path / "src", {"letter.jsonl": jsonl(good)})
    man = src / "MANIFEST.json"
    man.write_bytes(b"\xef\xbb\xbf" + man.read_bytes())
    manifest = overlay(tmp_path / "interim-x", src)
    assert [r.data for r in load_snapshot(tmp_path / "interim-x").rows()] == [good]
    assert manifest.source["source_manifest"]["sha256"] == sha(man.read_bytes())  # the BOM is hashed


@pytest.mark.parametrize(
    "entry",
    [
        "{kind: orphan_decision, doc: 123, field: sender, reason: r}",
        "{kind: orphan_decision, doc: D1, field: [sender], reason: r}",
        "{kind: orphan_decision, doc: D1, field: sender, reason: r, file: 7}",
    ],
)
def test_known_warnings_need_string_or_null_doc_and_field(tmp_path: Path, entry: str) -> None:
    path = tmp_path / "kw.yaml"
    path.write_text(f"snap-a:\n- {entry}\n")
    with pytest.raises(ImportRefused, match="string or null"):
        known_warnings_for(path, "snap-a")
    path.write_text("snap-a:\n- {kind: orphan_decision, doc: null, field: null, reason: r}\n")
    assert known_warnings_for(path, "snap-a")[0]["doc"] is None


# --- CLI ----------------------------------------------------------------------------------


def test_cli_import_and_check_exit_codes(tmp_path: Path) -> None:
    src = write_overlay(
        tmp_path / "src", {"letter.jsonl": jsonl(interim_row("D1", {"sender": ("gap_fill", "A")}))}
    )
    out = tmp_path / "interim-x"
    args = [
        "import-interim",
        "--out",
        str(out),
        "--source-dir",
        str(src),
        "--source-path",
        "data/labels/x",
        "--repository",
        "example/sandbox",
        "--commit",
        "1" * 40,
    ]
    assert runner.invoke(app, args).exit_code == 0
    ok = runner.invoke(app, [*args, "--check"])
    assert ok.exit_code == 0 and "identical to a fresh build" in ok.output
    (out / "letter.jsonl").write_bytes((out / "letter.jsonl").read_bytes().replace(b'"A"', b'"B"'))
    bad = runner.invoke(app, [*args, "--check"])
    assert bad.exit_code == 1 and "DIFFERS" in bad.output
    (src / "letter.jsonl").write_bytes(b"{}\n")
    refused = runner.invoke(app, args)
    assert refused.exit_code == 1 and "import refused" in refused.output


def test_cli_import_hub_and_review(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data")
    out = tmp_path / "hub-x"
    import yaml

    (tmp_path / "pins.yaml").write_text(yaml.safe_dump(pins_for(data)))
    hub_args = [
        "import-hub",
        "--out",
        str(out),
        "--profile",
        str(PROFILE_PATH),
        "--dataset-dir",
        str(data),
        "--default-dir",
        str(data),
        "--revision",
        "0" * 40,
        "--pins",
        str(tmp_path / "pins.yaml"),
    ]
    assert runner.invoke(app, hub_args).exit_code == 0
    assert runner.invoke(app, [*hub_args, "--check"]).exit_code == 0
    (tmp_path / "d.jsonl").write_bytes(jsonl(decision("D1", "sender", "Ann")))
    rv = [
        "import-review",
        "--out",
        str(tmp_path / "rv-x"),
        "--decisions-root",
        str(tmp_path),
        "--decisions",
        "d.jsonl",
    ]
    assert runner.invoke(app, rv).exit_code == 0
    assert runner.invoke(app, [*rv, "--check"]).exit_code == 0
    assert runner.invoke(app, ["import-review", "--out", str(tmp_path / "rv-y")]).exit_code == 2


# --- fetch_hub.py (network replaced by local bytes) -----------------------------------------


def _load_fetch():
    spec = importlib.util.spec_from_file_location("fetch_hub_under_test", SCRIPTS / "fetch_hub.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fetch_hub_verifies_every_file_against_manifest_txt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = _load_fetch()
    data = write_dataset(tmp_path / "data")
    served = {"manifest.txt": (data / "manifest.txt").read_bytes()}
    good = "parquet/ground_truth/train/train-00000-of-00001.parquet"
    served[good] = (data / good).read_bytes()
    evil = "parquet/ground_truth/test/test-00000-of-00001.parquet"
    served[evil] = b"not the published bytes"

    def fake_download(url: str, target: Path) -> str:
        rel = url.split("/resolve/" + "0" * 40 + "/", 1)[1]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(served[rel])
        return sha(served[rel])

    monkeypatch.setattr(fetch, "_download", fake_download)
    assert fetch.fetch(tmp_path / "out", [good], revision="0" * 40) == 0
    assert (tmp_path / "out" / good).read_bytes() == served[good]
    assert fetch.fetch(tmp_path / "out2", [evil], revision="0" * 40) == 1
    assert not (tmp_path / "out2" / evil).exists()  # a bad download is deleted
    assert fetch.fetch(tmp_path / "out3", ["parquet/not/listed.parquet"], revision="0" * 40) == 1
    assert fetch.fetch(tmp_path / "out", [good], revision="0" * 40) == 2  # not empty
    assert fetch.fetch(tmp_path / "out4", [good], revision="main") == 2


# --- dropped source data refuses the import (and fails the gate when allowed) ---------------


def _gate_kinds(tmp_path: Path, *snapshots: Path) -> set[str]:
    from labels_testkit import hub_row, make_snapshot

    dirs = list(snapshots)
    if not any(load_snapshot(d).kind == "hub" for d in dirs):
        hub = make_snapshot(
            tmp_path / "gate", "hub", [hub_row(d, fields={"sender": "Hub"}) for d in ("D1", "D2", "D3")]
        )
        dirs.append(hub)
    report, _ = check_dirs(dirs, profile())
    return {f.kind for f in report.errors}


def test_a_bom_at_the_start_of_an_interim_file_is_not_a_rejected_line(tmp_path: Path) -> None:
    row = interim_row("D1", {"sender": ("gap_fill", "Ann")})
    src = write_overlay(
        tmp_path / "src", {"letter.jsonl": b"\xef\xbb\xbf" + jsonl(row, interim_row("D2", {}))}
    )
    manifest = overlay(tmp_path / "interim-x", src)
    assert manifest.source["rejected_lines"] == []
    assert [r.data for r in load_snapshot(tmp_path / "interim-x").rows()][0] == row


def test_an_extra_corrupt_line_refuses_the_import_and_fails_the_gate_when_allowed(tmp_path: Path) -> None:
    row = interim_row("D1", {"sender": ("gap_fill", "Ann")})
    src = write_overlay(tmp_path / "src", {"letter.jsonl": jsonl(row, b'{"doc_id": "D2", ')})
    with pytest.raises(ImportRefused, match="rejected_lines 1"):
        overlay(tmp_path / "interim-x", src)
    out = tmp_path / "interim-x"
    overlay(out, src, allow_rejected=True)
    assert "dropped_source_data" in _gate_kinds(tmp_path, out)
    args = [
        "import-interim",
        "--out",
        str(tmp_path / "interim-y"),
        "--source-dir",
        str(src),
        "--source-path",
        "x",
        "--repository",
        "example/sandbox",
        "--commit",
        "1" * 40,
    ]
    assert runner.invoke(app, args).exit_code == 1
    assert runner.invoke(app, [*args, "--allow-rejected"]).exit_code == 0


def test_a_hub_row_whose_presence_is_not_an_object_refuses_the_import(tmp_path: Path) -> None:
    bad = gt_row("D9", "letter", "email", "train", {"sender": "Zed"}, {})
    bad["gt_fields"] = json.dumps({"sender": "Zed", "gt_presence": "[1,2]"})
    data = write_dataset(tmp_path / "data", extra=[bad])
    with pytest.raises(ImportRefused, match="unparseable_gt_fields 1"):
        hub_build(tmp_path / "hub-x", data)
    manifest = hub_build(tmp_path / "hub-x", data, allow_rejected=True)
    assert manifest.source["unparseable_gt_fields"] == [
        {"document_id": "D9", "reason": "gt_presence is not a JSON object"}
    ]
    assert "D9" not in hub_rows(tmp_path / "hub-x")
    assert "dropped_source_data" in _gate_kinds(tmp_path, tmp_path / "hub-x")


def test_a_desk_file_with_a_bom_imports_every_decision(tmp_path: Path) -> None:
    manifest, rows = review(tmp_path, [], [desk("D1", "sender", "Ann"), desk("D2", "sender", "Bo")], bom=True)
    assert [r["doc_id"] for r in rows] == ["D1", "D2"] and manifest.source["rejected_lines"] == []


def test_an_unknown_desk_verdict_refuses_the_review_import(tmp_path: Path) -> None:
    with pytest.raises(ImportRefused, match="not_imported 1"):
        review(tmp_path, [], [desk("D1", "sender", "Ann", "maybe")])
    review(
        tmp_path, [], [desk("D1", "sender", "Ann", "maybe"), desk("D2", "sender", "Bo")], allow_rejected=True
    )
    assert "dropped_source_data" in _gate_kinds(tmp_path, tmp_path / "gold-review-x")


# --- reviewed_at is compared as an instant --------------------------------------------------


def _conflict_reaches_the_gate(tmp_path: Path) -> bool:
    return "conflicting_decision" in _gate_kinds(tmp_path, tmp_path / "gold-review-x")


def test_offsets_are_ordered_by_instant_not_by_string(tmp_path: Path) -> None:
    # 12:00+05:00 is 07:00Z: earlier than 10:00Z although it sorts later as a string
    manifest, rows = review(
        tmp_path,
        [],
        [
            desk("D1", "sender", "Offset", at="2026-11-17T12:00:00+05:00"),
            desk("D1", "sender", "Zulu", at="2026-11-17T10:00:00Z"),
        ],
    )
    assert [r["entry"]["value"] for r in rows] == ["Zulu"]
    assert [s["value"] for s in manifest.source["superseded"]] == ["Offset"]


def test_milliseconds_order_decisions(tmp_path: Path) -> None:
    _, rows = review(
        tmp_path,
        [],
        [
            desk("D1", "sender", "Later", at="2026-11-17T10:00:00.500Z"),
            desk("D1", "sender", "Earlier", at="2026-11-17T10:00:00.499+00:00"),
        ],
    )
    assert [r["entry"]["value"] for r in rows] == ["Later"]


def test_the_same_instant_written_two_ways_is_a_tie_for_the_gate(tmp_path: Path) -> None:
    manifest, rows = review(
        tmp_path,
        [],
        [
            desk("D1", "sender", "A", at="2026-11-17T10:00:00Z"),
            desk("D1", "sender", "B", at="2026-11-17T15:30:00.000+05:30"),
        ],
    )
    assert sorted(r["entry"]["value"] for r in rows) == ["A", "B"] and manifest.source["superseded"] == []
    assert _conflict_reaches_the_gate(tmp_path)


@pytest.mark.parametrize(
    ("at", "reason"),
    [
        ("yesterday", "is not an ISO 8601 timestamp"),
        ("2026-11-17T10:00:00", "has no UTC offset"),
        (1763373600, "must be a string or null"),
    ],
)
def test_an_unusable_reviewed_at_is_a_rejected_line(tmp_path: Path, at: Any, reason: str) -> None:
    with pytest.raises(ImportRefused, match="rejected_lines 1"):
        review(
            tmp_path, [{**decision("D2", "sender", "X", human=True), "reviewed_at": at}], []
        )  # decision files too
    manifest, rows = review(
        tmp_path, [], [desk("D1", "sender", "A", at=at), desk("D2", "sender", "B")], allow_rejected=True
    )
    assert [r["doc_id"] for r in rows] == ["D2"]
    (bad,) = manifest.source["rejected_lines"]
    assert bad["line"] == 1 and reason in bad["reason"]


def test_an_undated_human_decision_that_disagrees_with_a_dated_one_is_a_conflict(tmp_path: Path) -> None:
    # Rule: an undated decision cannot be ordered against a dated one, so it never loses
    # silently: a different value is kept beside the dated winner and the gate fails.
    manifest, rows = review(
        tmp_path,
        [decision("D1", "sender", "Undated", human=True)],
        [desk("D1", "sender", "Dated", at="2026-11-17T10:00:00Z")],
    )
    assert sorted(r["entry"]["value"] for r in rows) == ["Dated", "Undated"]
    assert manifest.source["superseded"] == []
    assert _conflict_reaches_the_gate(tmp_path)


def test_an_undated_human_decision_that_agrees_with_a_dated_one_is_merged(tmp_path: Path) -> None:
    manifest, rows = review(
        tmp_path,
        [decision("D1", "sender", "Same", human=True)],
        [
            desk("D1", "sender", "Same", at="2026-11-17T10:00:00Z"),
            desk("D1", "sender", "Older", at="2026-11-17T09:00:00Z"),
        ],
    )
    (row,) = rows
    assert (row["entry"]["value"], row["reviewed_at"]) == ("Same", "2026-11-17T10:00:00Z")
    assert row["origin"] == ["review/decisions.jsonl:1", "sandbox/human_reviews.jsonl:1"]
    assert [s["value"] for s in manifest.source["superseded"]] == ["Older"]


# --- Hub revision pins and fetch_hub safety ---------------------------------------------------


def test_hub_refuses_a_revision_without_pins(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data")
    with pytest.raises(ImportRefused, match="no pinned file hashes"):
        hub_build(tmp_path / "hub-x", data, pins=pins_for(data, revision="1" * 40))
    assert not (tmp_path / "hub-x").exists()


@pytest.mark.parametrize(
    "rel",
    [
        "manifest.txt",
        "parquet/ground_truth/test/test-00000-of-00001.parquet",
        "parquet/default/train/train-00000-of-00001.parquet",
    ],
)
def test_hub_refuses_files_that_are_not_the_ones_pinned_for_the_revision(tmp_path: Path, rel: str) -> None:
    # a self-consistent download (manifest.txt agrees with its shards) of another revision
    data = write_dataset(tmp_path / "data")
    pins = pins_for(data)
    pins["0" * 40][rel] = "e" * 64
    with pytest.raises(ImportRefused, match=f"{rel}: sha256 .* is not the one pinned for revision"):
        hub_build(tmp_path / "hub-x", data, pins=pins)


def test_the_committed_pins_match_the_hub_snapshot_sources() -> None:
    from mailroom_labels.importers.hub import load_hub_pins

    root = Path(__file__).resolve().parents[3]
    pins = load_hub_pins(root / "config/clients/mailroom-dataset/hub-revisions.yaml")
    src = load_snapshot(root / "labels/hub-ccd040f6").manifest.source
    recorded = {f["path"]: f["sha256"] for f in src["files"]} | {
        "manifest.txt": src["dataset_manifest"]["sha256"]
    }
    assert pins == {src["revision"]: recorded}


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("parquet/ground_truth/train/train-00000-of-00001.parquet", True),
        ("../escape.parquet", False),
        ("parquet/../../escape", False),
        ("/etc/passwd", False),
        ("parquet\\..\\x", False),
        ("", False),
        ("C:/Windows/x.parquet", False),
        ("C:x.parquet", False),
        ("c:\\x.parquet", False),
        ("parquet/a:b.parquet", False),
    ],
)
def test_fetch_hub_refuses_paths_outside_the_download_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, ok: bool
) -> None:
    fetch = _load_fetch()
    assert fetch._safe(path) is ok
    if not ok:
        data = write_dataset(tmp_path / "data")
        calls: list[str] = []

        def fake_download(url: str, target: Path) -> str:
            calls.append(url)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((data / "manifest.txt").read_bytes())
            return sha(target.read_bytes())

        monkeypatch.setattr(fetch, "_download", fake_download)
        assert fetch.fetch(tmp_path / "out", [path], revision="0" * 40) == 1
        assert len(calls) == 1  # only manifest.txt was fetched
        assert sorted(p.name for p in (tmp_path / "out").rglob("*")) == ["manifest.txt"]


def test_fetch_hub_deletes_a_download_that_fails_part_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = _load_fetch()

    class Broken:
        def __init__(self) -> None:
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, n: int) -> bytes:
            if self.sent:
                raise ConnectionResetError("cut off")
            self.sent = True
            return b"partial bytes"

    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda url, timeout: Broken())
    target = tmp_path / "out" / "parquet" / "x.parquet"
    with pytest.raises(ConnectionResetError):
        fetch._download("https://example.invalid/x", target)
    assert not target.exists()
    assert fetch.fetch(tmp_path / "out2", ["parquet/x.parquet"], revision="0" * 40) == 1
    assert not (tmp_path / "out2" / "manifest.txt").exists()


# --- unlisted files and directories ---------------------------------------------------------


# STRAY.JSONL, not LETTER.JSONL: on a case-insensitive file system that name would
# overwrite the listed letter.jsonl and the test would meet a hash mismatch instead.
@pytest.mark.parametrize("stray", ["STRAY.JSONL", "nested/letter.jsonl", "a/b/More.Jsonl"])
def test_overlay_refuses_unlisted_jsonl_of_any_case_or_depth(tmp_path: Path, stray: str) -> None:
    src = write_overlay(
        tmp_path / "src", {"letter.jsonl": jsonl(interim_row("D1", {"sender": ("gap_fill", "A")}))}
    )
    (src / stray).parent.mkdir(parents=True, exist_ok=True)
    (src / stray).write_bytes(jsonl(interim_row("D2", {"sender": ("gap_fill", "B")})))
    with pytest.raises(ImportRefused, match="unlisted overlay file"):
        overlay(tmp_path / "interim-x", src)
    (src / "README.md").write_text("notes")  # a non-label file is not refused by itself
    (src / stray).unlink()
    overlay(tmp_path / "interim-x", src)


@pytest.mark.parametrize("extra", ["sub/stray.jsonl", "sub/", "deep/er/"])
def test_check_and_load_treat_an_extra_file_or_directory_as_a_difference(tmp_path: Path, extra: str) -> None:
    from mailroom_labels.errors import ManifestMismatch

    src = write_overlay(
        tmp_path / "src", {"letter.jsonl": jsonl(interim_row("D1", {"sender": ("gap_fill", "A")}))}
    )
    out = tmp_path / "interim-x"
    overlay(out, src)
    if extra.endswith("/"):
        (out / extra).mkdir(parents=True)
    else:
        (out / extra).parent.mkdir(parents=True)
        (out / extra).write_bytes(b"{}\n")
    result = check_snapshot(out, lambda d: overlay(d, src))
    assert not result.identical
    assert any(extra.split("/")[0] in line for line in result.differences)
    with pytest.raises(ManifestMismatch) as info:
        load_snapshot(out)
    assert info.value.kind == "extra"


# --- Hub fields without a presence code, and list placeholders --------------------------------


def test_hub_field_without_a_presence_code_is_unlabeled_only_when_empty(tmp_path: Path) -> None:
    row = gt_row(
        "D9",
        "letter",
        "email",
        "train",
        {"sender": "Zed", "recipient": "", "action_items": "[]", "amount": "nan", "urgency": "routine"},
        {"urgency": "populated"},
    )
    data = write_dataset(tmp_path / "data", extra=[row])
    manifest = hub_build(tmp_path / "hub-x", data, default=False)
    fields = hub_rows(tmp_path / "hub-x")["D9"]["fields"]
    assert {k: v["presence"] for k, v in fields.items()} == {
        "sender": "populated",
        "recipient": "unlabeled",
        "action_items": "unlabeled",
        "amount": "unlabeled",
        "urgency": "populated",
    }
    assert fields["action_items"]["value"] == []  # the list string is still decoded, never invented
    assert manifest.source["fields_without_presence"] == {
        "action_items": 1,
        "amount": 1,
        "recipient": 1,
        "sender": 1,
    }


@pytest.mark.parametrize(
    ("placeholder", "nulled"),
    [("[]", True), ("{}", True), ("nan", True), ("", True), ('["x"]', False), ("none", False)],
)
def test_hub_not_applicable_list_placeholders_are_nulled(
    tmp_path: Path, placeholder: str, nulled: bool
) -> None:
    row = gt_row(
        "D9",
        "letter",
        "email",
        "train",
        {"action_items": placeholder, "sender": "Zed"},
        {"action_items": "not_applicable", "sender": "populated"},
    )
    data = write_dataset(tmp_path / "data", extra=[row])
    manifest = hub_build(tmp_path / "hub-x", data, default=False)
    value = hub_rows(tmp_path / "hub-x")["D9"]["fields"]["action_items"]["value"]
    counted = manifest.source["not_applicable_placeholders_nulled"]
    if nulled:
        assert value is None and counted.get(f"action_items={placeholder!r}") == 1
    else:
        assert value is not None and not any(k.startswith("action_items=") for k in counted)


# --- unsure is a recorded "no decision", never dropped data --------------------------------------


def test_an_unsure_mark_followed_by_a_decision_imports_the_decision(tmp_path: Path) -> None:
    manifest, rows = review(
        tmp_path,
        [],
        [
            desk("D1", "sender", "Ann", "unsure", at="2026-11-17T10:00:00Z"),
            desk("D1", "sender", "Ann", "keep", at="2026-11-17T11:00:00Z"),
        ],
    )
    assert [(r["doc_id"], r["verdict"]) for r in rows] == [("D1", "accept")]
    (u,) = manifest.source["undecided"]
    assert (u["doc_id"], u["field"], u["reviewed_at"], u["replaced"]) == (
        "D1",
        "sender",
        "2026-11-17T10:00:00Z",
        True,
    )
    assert manifest.source["not_imported"] == [] and manifest.source["rejected_lines"] == []


def test_an_unsure_mark_alone_imports_no_label_and_passes_the_gate(tmp_path: Path) -> None:
    manifest, rows = review(tmp_path, [], [desk("D1", "sender", "Ann", "unsure"), desk("D2", "sender", "Bo")])
    assert [r["doc_id"] for r in rows] == ["D2"]
    (u,) = manifest.source["undecided"]
    assert (u["doc_id"], u["decision"], u["replaced"], u["note"]) == ("D1", "unsure", False, None)
    assert "dropped_source_data" not in _gate_kinds(tmp_path, tmp_path / "gold-review-x")
    recs = read_layer(load_snapshot(tmp_path / "gold-review-x")).records
    assert {(r.document_id, r.field) for r in recs} == {("D2", "sender")}


def test_a_later_unsure_mark_withdraws_the_earlier_desk_decisions(tmp_path: Path) -> None:
    # The desk's rule: a later mark replaces an earlier one, and unsure keeps the label in
    # needs_human. So keep @10:00 then unsure @11:00 is no decision: never gold.
    manifest, rows = review(
        tmp_path,
        [decision("D1", "sender", "Claude says")],
        [
            desk("D1", "sender", "Ann", "keep", at="2026-11-17T10:00:00Z"),
            desk("D1", "sender", "Anne", "correct", at="2026-11-17T09:00:00+00:00"),
            desk("D1", "sender", "Ann", "unsure", at="2026-11-17T11:00:00Z"),
            desk("D2", "sender", "Bo", "keep"),
        ],
    )
    # the human decisions are withdrawn; the Claude decision stays what it is without a
    # human one (near_gold, never gold), as it does beside an unsure mark alone
    assert [(r["doc_id"], r["reviewer"]["model"]) for r in rows] == [("D1", "claude-opus"), ("D2", "human")]
    withdrawn = [s for s in manifest.source["superseded"] if s["doc_id"] == "D1"]
    assert sorted((s["value"], s["reason"]) for s in withdrawn) == [
        ("Ann", "a later unsure mark on the same label"),
        ("Anne", "a later unsure mark on the same label"),
    ]
    (u,) = manifest.source["undecided"]
    assert (u["doc_id"], u["reviewed_at"], u["replaced"]) == ("D1", "2026-11-17T11:00:00Z", False)
    assert "unordered_undecided" not in manifest.source
    recs = read_layer(load_snapshot(tmp_path / "gold-review-x")).records
    assert {(r.document_id, r.rank) for r in recs} == {("D1", "review:near_gold"), ("D2", "review:gold")}
    assert _gate_kinds(tmp_path, tmp_path / "gold-review-x") == set()


@pytest.mark.parametrize(
    ("mark_at", "decision_at", "decision_file"),
    [
        (None, "2026-11-17T10:00:00Z", False),  # an undated mark against a dated decision
        ("2026-11-17T11:00:00Z", None, True),  # a dated mark against an undated decision
        ("2026-11-17T11:00:00Z", "2026-11-17T12:00:00+01:00", False),  # the same instant
    ],
    ids=["undated_mark", "undated_decision", "same_instant"],
)
def test_an_unsure_mark_that_cannot_be_ordered_against_a_decision_fails_safe(
    tmp_path: Path, mark_at: str | None, decision_at: str | None, decision_file: bool
) -> None:
    decisions = [decision("D1", "sender", "Ann", human=True)] if decision_file else []
    desks = [] if decision_file else [desk("D1", "sender", "Ann", "keep", at=decision_at)]
    desks.append(desk("D1", "sender", "Ann", "unsure", at=mark_at))
    with pytest.raises(ImportRefused, match="unordered_undecided 1"):
        review(tmp_path, decisions, desks)
    assert not (tmp_path / "gold-review-x").exists()  # refused: nothing written
    manifest, _ = review(tmp_path, decisions, desks, allow_rejected=True)
    (u,) = manifest.source["unordered_undecided"]
    assert (u["doc_id"], u["field"], u["reviewed_at"]) == ("D1", "sender", mark_at)
    assert "dropped_source_data" in _gate_kinds(tmp_path, tmp_path / "gold-review-x")


def test_an_unexplained_superseded_reason_refuses_the_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unreachable with the real reasons: drop one from the allowed set to prove the guard bites.
    import mailroom_labels.importers.review as review_module

    monkeypatch.setattr(review_module, "SUPERSEDED_REASONS", ("a human decision on the same label",))
    later = [desk("D1", "sender", "Old", at="2026-11-17T10:00:00Z"), desk("D1", "sender", "New")]
    with pytest.raises(ImportRefused, match="unexplained_superseded 1"):
        review(tmp_path, [], later)
    manifest, _ = review(tmp_path, [], later, allow_rejected=True)
    (u,) = manifest.source["unexplained_superseded"]
    assert u["value"] == "Old"
    assert "dropped_source_data" in _gate_kinds(tmp_path, tmp_path / "gold-review-x")


# --- review lines whose shape the review reader would refuse are rejected at import ------------


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({"reviewer": {"model": "claude-opus"}}, "wrong shape"),  # no role (C1)
        ({"reviewer": {"model": "claude-opus", "role": ""}}, "wrong shape"),
        ({"reviewer": {"model": "claude-opus", "role": "audit", "extra": 1}}, "wrong shape"),
    ],
)
def test_a_decision_with_a_reviewer_the_reader_refuses_is_a_rejected_line(
    tmp_path: Path, bad: dict, reason: str
) -> None:
    manifest, rows = review(
        tmp_path,
        [{**decision("D1", "sender", "A"), **bad}, decision("D2", "sender", "B")],
        [],
        allow_rejected=True,
    )
    assert [r["doc_id"] for r in rows] == ["D2"]
    (rej,) = manifest.source["rejected_lines"]
    assert rej["line"] == 1 and reason in rej["reason"]
    assert read_layer(load_snapshot(tmp_path / "gold-review-x")).findings == []


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("decision", ["keep"], "decision must be a string"),  # C4: unhashable, used to raise TypeError
        ("decision", {"k": 1}, "decision must be a string"),
        ("source", 7, "source (the reviewer role) must be a name"),  # C5
        ("source", None, "source (the reviewer role) must be a name"),
    ],
)
def test_a_desk_line_with_a_malformed_decision_or_source_is_a_rejected_line(
    tmp_path: Path, key: str, value: Any, reason: str
) -> None:
    manifest, rows = review(
        tmp_path,
        [],
        [{**desk("D1", "sender", "A"), key: value}, desk("D2", "sender", "B")],
        allow_rejected=True,
    )
    assert [r["doc_id"] for r in rows] == ["D2"]
    (rej,) = manifest.source["rejected_lines"]
    assert rej["line"] == 1 and reason in rej["reason"]


# --- strict source parsing: repeated keys, deep nesting, a BOM that is not at the start ------------


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (b'{"doc_id": "D2", "doc_id": "D3", "labels": {}}', "duplicate key 'doc_id'"),
        (b'{"doc_id": "D2", "labels": {"sender": {"value": "A", "value": "B"}}}', "duplicate key 'value'"),
        (b'{"doc_id": "D2", "x": ' + b"[" * 100_000 + b"]" * 100_000 + b"}", "nested too deeply"),
        (b"\xef\xbb\xbf" + json.dumps(interim_row("D2", {})).encode(), "not JSON"),  # a BOM on line 2 is data
        (b'{"doc_id": "D2", "x": NaN, "labels": {}}', "non-finite number NaN"),
        (b'{"doc_id": "D2", "x": Infinity, "labels": {}}', "non-finite number Infinity"),
        (b'{"doc_id": "D2", "x": [-Infinity], "labels": {}}', "non-finite number -Infinity"),
    ],
)
def test_a_source_line_that_is_not_strict_json_is_rejected(tmp_path: Path, line: bytes, reason: str) -> None:
    good = interim_row("D1", {"sender": ("gap_fill", "A")})
    src = write_overlay(tmp_path / "src", {"letter.jsonl": jsonl(good, line)})
    with pytest.raises(ImportRefused, match="rejected_lines 1"):
        overlay(tmp_path / "interim-x", src)
    manifest = overlay(tmp_path / "interim-x", src, allow_rejected=True)
    (rej,) = manifest.source["rejected_lines"]
    assert rej["line"] == 2 and reason in rej["reason"]


@pytest.mark.parametrize(
    "text",
    [
        '{"schema": "a", "schema": "mailroom-interim-labels/v1", "files": {}}',
        '{"x": ' + "[" * 100_000 + "]" * 100_000 + "}",
    ],
)
def test_an_overlay_manifest_that_is_not_strict_json_is_refused(tmp_path: Path, text: str) -> None:
    src = write_overlay(tmp_path / "src", {"letter.jsonl": jsonl(interim_row("D1", {}))})
    (src / "MANIFEST.json").write_text(text)
    with pytest.raises(ImportRefused, match="MANIFEST.json is not JSON"):
        overlay(tmp_path / "interim-x", src)


def test_hub_gt_fields_with_a_repeated_key_is_unparseable(tmp_path: Path) -> None:
    bad = gt_row("D9", "letter", "email", "train", {}, {})
    bad["gt_fields"] = '{"sender": "A", "sender": "B", "gt_presence": "{}"}'
    data = write_dataset(tmp_path / "data", extra=[bad])
    manifest = hub_build(tmp_path / "hub-x", data, allow_rejected=True)
    assert manifest.source["unparseable_gt_fields"] == [
        {"document_id": "D9", "reason": "gt_fields is not a JSON object"}
    ]


@pytest.mark.parametrize("presence", ["[]", "false", "0", "[1,2]", '"x"', 0, False, []])
def test_hub_gt_presence_that_is_not_an_object_is_unparseable_never_empty(
    tmp_path: Path, presence: Any
) -> None:
    bad = gt_row("D9", "letter", "email", "train", {}, {})
    bad["gt_fields"] = json.dumps({"sender": "Zed", "gt_presence": presence})
    data = write_dataset(tmp_path / "data", extra=[bad])
    with pytest.raises(ImportRefused, match="unparseable_gt_fields 1"):
        hub_build(tmp_path / "hub-x", data)


@pytest.mark.parametrize("presence", [None, "", "{}"])
def test_hub_absent_or_empty_gt_presence_means_no_presence_codes(tmp_path: Path, presence: Any) -> None:
    row = gt_row("D9", "letter", "email", "train", {}, {})
    gt: dict[str, Any] = {"sender": "Zed"}
    if presence is not None:
        gt["gt_presence"] = presence
    row["gt_fields"] = json.dumps(gt)
    data = write_dataset(tmp_path / "data", extra=[row])
    hub_build(tmp_path / "hub-x", data, default=False)
    assert hub_rows(tmp_path / "hub-x")["D9"]["fields"]["sender"] == {"value": "Zed", "presence": "populated"}


@pytest.mark.parametrize("doc_text", [None])
def test_a_missing_doc_text_never_verifies_even_the_empty_text_hash(tmp_path: Path, doc_text: Any) -> None:
    row = gt_row(
        "D9", "letter", "email", "train", {"sender": "Zed"}, {"sender": "populated"}, content=sha(b"")
    )
    data = write_dataset(
        tmp_path / "data", extra=[row], extra_text=[{"filename": "D9.txt", "doc_text": doc_text}]
    )
    manifest = hub_build(tmp_path / "hub-x", data)
    assert "D9" in manifest.source["text_unverified_documents"]
    assert manifest.source["text_check"]["unverified_by_reason"]["no_doc_text"] == 1


def test_a_hub_import_without_the_text_check_warns_and_fails_strict(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data")
    manifest = hub_build(tmp_path / "hub-x", data, default=False)
    assert manifest.source["text_check"] == {"run": False}
    report, _ = check_dirs([tmp_path / "hub-x"], profile())
    unchecked = [f for f in report.warnings if f.kind == "text_unchecked"]
    assert len(unchecked) == 1 and not unchecked[0].known
    assert unchecked[0] in report.unknown_warnings  # verify --strict fails on it
    known = ({"kind": "text_unchecked", "doc": None, "field": None, "reason": "text not downloaded"},)
    hub_build(tmp_path / "hub-y", data, default=False, known_warnings=known)
    report, _ = check_dirs([tmp_path / "hub-y"], profile())
    assert [f.known for f in report.warnings if f.kind == "text_unchecked"] == [True]


# --- CLI usage errors exit 2 with one line, never a traceback ----------------------------------


def _interim_args(tmp_path: Path, out: Path) -> list[str]:
    src = write_overlay(
        tmp_path / "src", {"letter.jsonl": jsonl(interim_row("D1", {"sender": ("gap_fill", "A")}))}
    )
    return [
        "import-interim",
        "--out",
        str(out),
        "--source-dir",
        str(src),
        "--source-path",
        "x",
        "--repository",
        "example/sandbox",
        "--commit",
        "1" * 40,
    ]


@pytest.mark.parametrize(
    "case", ["bad_id_option", "bad_out_name", "out_holds_other_files", "missing_known_warnings"]
)
def test_cli_import_usage_errors_exit_2_without_a_traceback(tmp_path: Path, case: str) -> None:
    out = tmp_path / ("Bad Name" if case == "bad_out_name" else "interim-x")
    args = _interim_args(tmp_path, out)
    if case == "bad_id_option":
        args += ["--snapshot-id", "a+b"]
    if case == "out_holds_other_files":
        out.mkdir()
        (out / "old.jsonl").write_text("{}\n")
    if case == "missing_known_warnings":
        args += ["--known-warnings", str(tmp_path / "nope.yaml")]
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)  # handled: no traceback
    assert "usage error:" in result.output and "Traceback" not in result.output


def test_cli_import_hub_with_missing_pins_exits_2(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data")
    result = runner.invoke(
        app,
        [
            "import-hub",
            "--out",
            str(tmp_path / "hub-x"),
            "--profile",
            str(PROFILE_PATH),
            "--dataset-dir",
            str(data),
            "--revision",
            "0" * 40,
            "--pins",
            str(tmp_path / "nope.yaml"),
        ],
    )
    assert result.exit_code == 2 and isinstance(result.exception, SystemExit)
    assert "usage error: --pins" in result.output


def test_fetch_hub_refuses_a_stream_cut_off_mid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    fetch = _load_fetch()

    class Truncated:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, n: int) -> bytes:
            raise http.client.IncompleteRead(b"partial", 100)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda url, timeout: Truncated())
    assert fetch.fetch(tmp_path / "out", ["parquet/x.parquet"], revision="0" * 40) == 1
    assert not (tmp_path / "out" / "manifest.txt").exists()


# --- build_profile.py: pinned Hub files and a hex commit ---------------------------------------


def _load_build_profile():
    spec = importlib.util.spec_from_file_location("build_profile_under_test", SCRIPTS / "build_profile.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("commit", ["g" * 40, "A" * 40, "0" * 39, "0" * 40 + "\n"])
def test_build_profile_requires_a_lowercase_40_hex_eda_commit(tmp_path: Path, commit: str) -> None:
    bp = _load_build_profile()
    assert (
        bp.main(
            [
                "--eda-dir",
                str(tmp_path),
                "--eda-commit",
                commit,
                "--hub-dir",
                str(tmp_path),
                "--out",
                str(tmp_path / "p.yaml"),
            ]
        )
        == 2
    )


@pytest.mark.parametrize("rel", ["manifest.txt", "parquet/ground_truth/train/train-00000-of-00001.parquet"])
def test_build_profile_refuses_hub_files_that_are_not_the_pinned_ones(tmp_path: Path, rel: str) -> None:
    bp = _load_build_profile()
    data = write_dataset(tmp_path / "data")
    pinned = pins_for(data)["0" * 40]
    rows, _ = bp._hub_rows(data, pinned)
    assert rows
    pinned[rel] = "e" * 64  # a self-consistent download of another revision
    with pytest.raises(bp.BuildError, match="pinned|pins record"):
        bp._hub_rows(data, pinned)


# --- a refused rebuild leaves the earlier build as it was --------------------------------------


def _tree_bytes(d: Path) -> dict[str, bytes | None]:
    return {p.relative_to(d).as_posix(): None if p.is_dir() else p.read_bytes() for p in d.rglob("*")}


@pytest.mark.parametrize("fault", ["nan_line", "known_warning_doc_not_a_string"])
def test_a_refused_rebuild_leaves_the_earlier_build_intact(tmp_path: Path, fault: str) -> None:
    good = interim_row("D1", {"sender": ("gap_fill", "Ann")})
    out = tmp_path / "interim-x"

    def args(src: Path) -> list[str]:
        return [
            "import-interim",
            "--out",
            str(out),
            "--source-dir",
            str(src),
            "--source-path",
            "data/labels/x",
            "--repository",
            "example/sandbox",
            "--commit",
            "1" * 40,
        ]

    first = runner.invoke(app, args(write_overlay(tmp_path / "s1", {"letter.jsonl": jsonl(good)})))
    assert first.exit_code == 0, first.output
    before = _tree_bytes(out)
    if fault == "nan_line":
        bad = interim_row("D2", {"sender": ("gap_fill", "Bob")})
        bad["extra_note"] = float("nan")  # json.dumps writes the bare token NaN
        line = json.dumps(bad).encode()
        assert b"NaN" in line
        again = runner.invoke(app, args(write_overlay(tmp_path / "s2", {"letter.jsonl": jsonl(good, line)})))
        assert "rejected_lines 1" in again.output
    else:
        kw = tmp_path / "kw.yaml"
        kw.write_text("interim-x:\n- {kind: orphan_decision, doc: 123, field: sender, reason: r}\n")
        again = runner.invoke(app, [*args(tmp_path / "s1"), "--known-warnings", str(kw)])
        assert "string or null" in again.output
    assert again.exit_code == 1 and "import refused" in again.output
    assert _tree_bytes(out) == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == []
    assert load_snapshot(out).manifest.files[0].rows == 1


# --- the bytes an importer hashes are the bytes it parses --------------------------------------


@pytest.fixture
def swap_after_first_read(monkeypatch: pytest.MonkeyPatch):
    """Arm: the first binary open of ``target`` returns its bytes and then replaces the file
    with ``other``, as a writer racing the import would. A second read sees ``other``."""

    def arm(target: Path, other: bytes) -> None:
        real_open = Path.open
        done: list[bool] = []

        def racing_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any):
            if not done and "b" in mode and "r" in mode and self.resolve() == target.resolve():
                done.append(True)
                original = _raw_read(real_open, self)
                target.write_bytes(other)
                import io

                return io.BytesIO(original)
            return real_open(self, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", racing_open)

    return arm


def _raw_read(real_open: Any, path: Path) -> bytes:
    with real_open(path, "rb") as fh:
        data: bytes = fh.read()
    return data


def test_an_overlay_file_is_parsed_from_the_bytes_that_were_hashed(
    tmp_path: Path, swap_after_first_read
) -> None:
    good = interim_row("D1", {"sender": ("gap_fill", "Ann")})
    src = write_overlay(tmp_path / "src", {"letter.jsonl": jsonl(good)})
    original = (src / "letter.jsonl").read_bytes()
    swap_after_first_read(src / "letter.jsonl", jsonl(interim_row("D9", {"sender": ("gap_fill", "Eve")})))
    manifest = overlay(tmp_path / "interim-x", src)
    assert [r.data for r in load_snapshot(tmp_path / "interim-x").rows()] == [good]
    assert manifest.source["files"][0]["sha256"] == sha(original)


def test_a_desk_file_is_parsed_from_the_bytes_that_were_hashed(tmp_path: Path, swap_after_first_read) -> None:
    kpath = tmp_path / "desk.jsonl"
    kpath.write_bytes(jsonl(desk("D1", "sender", "Ann")))
    original = kpath.read_bytes()
    swap_after_first_read(kpath, jsonl(desk("D9", "sender", "Eve")))
    manifest = build_review(tmp_path / "gold-review-x", desk=[(kpath, "sandbox/human_reviews.jsonl")])
    assert [r.data["doc_id"] for r in load_snapshot(tmp_path / "gold-review-x").rows()] == ["D1"]
    assert manifest.source["files"][0]["sha256"] == sha(original)


def test_a_hub_shard_is_parsed_from_the_bytes_that_were_hashed(tmp_path: Path, swap_after_first_read) -> None:
    data = write_dataset(tmp_path / "data")
    train = data / "parquet/ground_truth/train/train-00000-of-00001.parquet"
    original, pins = train.read_bytes(), pins_for(data)
    swap_after_first_read(
        train, (data / "parquet/ground_truth/test/test-00000-of-00001.parquet").read_bytes()
    )
    manifest = build_hub(  # not hub_build: its setdefault reads every file to pin it
        tmp_path / "hub-x",
        dataset_dir=data,
        profile=profile(),
        profile_sha256=sha(PROFILE_PATH.read_bytes()),
        repository="example/synthetic",
        revision="0" * 40,
        default_dir=data,
        pins=pins,
    )
    assert sorted(hub_rows(tmp_path / "hub-x")) == ["D1", "D2", "D3", "F1"]
    assert {f["path"]: f["sha256"] for f in manifest.source["files"]}[
        "parquet/ground_truth/train/train-00000-of-00001.parquet"
    ] == sha(original)


def test_fetch_hub_rejects_a_download_that_ends_before_its_declared_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sized read can hit EOF early without raising; the declared Content-Length still catches it."""
    fetch = _load_fetch()

    class Short:
        headers = {"Content-Length": "100"}

        def __init__(self) -> None:
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, n: int) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return b"x" * 40

    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda url, timeout: Short())
    target = tmp_path / "out" / "x.parquet"
    assert fetch._fetch_one("https://example.invalid/x", target) is None
    assert not target.exists()
    Short.headers = {"Content-Length": "40"}  # positive control: a complete download is kept
    assert fetch._fetch_one("https://example.invalid/x", target) is not None
    assert target.read_bytes() == b"x" * 40
    Short.headers = {}  # no declared length: nothing to compare, kept as before
    assert fetch._fetch_one("https://example.invalid/y", tmp_path / "out" / "y.parquet") is not None


# --- duplicate YAML keys in the configuration files the importers read --------------------------


def test_hub_pins_refuse_a_duplicated_shard_path(tmp_path: Path) -> None:
    from mailroom_labels.importers.hub import load_hub_pins

    rel = "parquet/ground_truth/train/train-00000-of-00001.parquet"
    path = tmp_path / "pins.yaml"
    path.write_text(f"'{'0' * 40}':\n  manifest.txt: {'a' * 64}\n  {rel}: {'b' * 64}\n  {rel}: {'c' * 64}\n")
    with pytest.raises(ImportRefused, match="duplicate key"):
        load_hub_pins(path)
    path.write_text(f"'{'0' * 40}':\n  manifest.txt: {'a' * 64}\n  {rel}: {'b' * 64}\n")
    assert load_hub_pins(path) == {"0" * 40: {"manifest.txt": "a" * 64, rel: "b" * 64}}


def test_known_warnings_refuse_a_duplicated_snapshot_key(tmp_path: Path) -> None:
    entry = "  - {kind: orphan_decision, doc: D1, field: sender, reason: added by review}\n"
    path = tmp_path / "known.yaml"
    path.write_text("snap-a:\n" + entry + "snap-a:\n" + entry.replace("D1", "D2"))
    with pytest.raises(ImportRefused, match="duplicate key"):
        known_warnings_for(path, "snap-a")
    path.write_text("snap-a:\n" + entry)
    assert [w["doc"] for w in known_warnings_for(path, "snap-a")] == ["D1"]


def test_build_profile_refuses_a_duplicated_key_in_the_field_registry(tmp_path: Path) -> None:
    bp = _load_build_profile()
    registry = tmp_path / bp.FIELDS_YAML
    registry.parent.mkdir(parents=True)
    registry.write_text("vocabularies:\n  urgency: [routine]\n  urgency: [urgent]\n")
    with pytest.raises(bp.BuildError, match="duplicate key 'urgency'"):
        bp.build(tmp_path, "0" * 40, tmp_path, [], {})


def test_a_pin_or_revision_with_a_trailing_newline_is_refused(tmp_path: Path) -> None:
    from mailroom_labels.importers.hub import load_hub_pins

    rev, sha = "0" * 40, "a" * 64
    path = tmp_path / "pins.yaml"
    path.write_text(f'"{rev}\\n":\n  manifest.txt: {sha}\n')
    with pytest.raises(ImportRefused):
        load_hub_pins(path)
    path.write_text(f"'{rev}':\n  manifest.txt: \"{sha}\\n\"\n")
    with pytest.raises(ImportRefused):
        load_hub_pins(path)
    fetch = _load_fetch()
    assert fetch.fetch(tmp_path / "out", ["manifest.txt"], revision=rev + "\n") == 2
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "names",
    [("same.jsonl", "same.jsonl"), ("review/a.jsonl", "review/a.jsonl")],
    ids=["across_kinds", "same_path"],
)
def test_review_refuses_two_sources_with_one_name_before_reading(tmp_path: Path, names) -> None:
    dpath, kpath = tmp_path / "decisions.jsonl", tmp_path / "desk.jsonl"
    dpath.write_bytes(jsonl())
    kpath.write_bytes(jsonl(desk("D1", "sender", "Ann", "keep")))
    with pytest.raises(ValueError, match="same name"):
        build_review(tmp_path / "gold-review-x", decisions=[(dpath, names[0])], desk=[(kpath, names[1])])
    assert not (tmp_path / "gold-review-x").exists()
    build_review(tmp_path / "gold-review-x", decisions=[(dpath, "d.jsonl")], desk=[(kpath, "k.jsonl")])


@pytest.mark.parametrize("gt", [{}, {"sender": None}], ids=["key_missing", "value_null"])
def test_hub_populated_presence_without_a_value_refuses_the_import(
    tmp_path: Path, gt: dict[str, Any]
) -> None:
    """gt_presence says populated but gt_fields holds no value: the reader refuses that row
    (a populated label is never null), so the importer never writes it. Like any other
    unreadable gt_fields it refuses the import, and ``--allow-rejected`` lists it by
    document and key without writing the row."""
    bad = gt_row("D9", "letter", "email", "train", gt, {"sender": "populated"})
    data = write_dataset(tmp_path / "data", extra=[bad])
    with pytest.raises(ImportRefused, match="unparseable_gt_fields 1"):
        hub_build(tmp_path / "hub-x", data)
    manifest = hub_build(tmp_path / "hub-x", data, allow_rejected=True)
    assert manifest.source["unparseable_gt_fields"] == [
        {"document_id": "D9", "reason": "gt_presence says 'sender' is populated but gt_fields has no value"}
    ]
    assert "D9" not in hub_rows(tmp_path / "hub-x")
    kinds = _gate_kinds(tmp_path, tmp_path / "hub-x")
    assert "dropped_source_data" in kinds and "malformed_record" not in kinds


@pytest.mark.parametrize("code", ["schema_documented_absence", "not_applicable", "pending_annotation"])
def test_hub_absent_presence_without_a_value_is_kept_and_counted(tmp_path: Path, code: str) -> None:
    """A presence that is not populated needs no value: the field is written with null,
    counted in ``presence_without_value``, and the reader accepts it."""
    row = gt_row(
        "D9", "letter", "email", "train", {"sender": "Zed"}, {"sender": "populated", "recipient": code}
    )
    data = write_dataset(tmp_path / "data", extra=[row])
    manifest = hub_build(tmp_path / "hub-x", data)
    want = "unlabeled" if code == "pending_annotation" else code
    assert hub_rows(tmp_path / "hub-x")["D9"]["fields"]["recipient"] == {"value": None, "presence": want}
    assert manifest.source["presence_without_value"] == {"recipient": 1}
    assert "malformed_record" not in _gate_kinds(tmp_path, tmp_path / "hub-x")


@pytest.mark.parametrize(
    "code", ["flagged", 1, None, ["populated"]], ids=["unknown_string", "int", "null", "list"]
)
def test_hub_presence_code_the_reader_refuses_refuses_the_import(tmp_path: Path, code: Any) -> None:
    """A ``gt_presence`` code that is not one the Hub reader accepts (after the
    ``pending_annotation`` mapping) is never written: the reader would refuse the row as an
    unknown presence. The row is unreadable source data like a populated null: listed in
    ``unparseable_gt_fields`` by document, key and code, and the import refused unless
    ``--allow-rejected``. A list code must not crash the importer."""
    bad = gt_row("D9", "letter", "email", "train", {"sender": "Zed"}, {"sender": code})
    data = write_dataset(tmp_path / "data", extra=[bad])
    with pytest.raises(ImportRefused, match="unparseable_gt_fields 1"):
        hub_build(tmp_path / "hub-x", data)
    manifest = hub_build(tmp_path / "hub-x", data, allow_rejected=True)
    assert manifest.source["unparseable_gt_fields"] == [
        {"document_id": "D9", "reason": f"gt_presence code {code!r} for 'sender' is not a known presence"}
    ]
    assert "D9" not in hub_rows(tmp_path / "hub-x")
    kinds = _gate_kinds(tmp_path, tmp_path / "hub-x")
    assert "dropped_source_data" in kinds and "malformed_record" not in kinds


@pytest.mark.parametrize(
    ("sender_value", "sender_code"),
    [(None, "populated"), ("Zed", "flagged")],
    ids=["populated_null", "unknown_code"],
)
def test_hub_rejected_row_adds_nothing_to_the_tallies(
    tmp_path: Path, sender_value: Any, sender_code: str
) -> None:
    """A row refused part-way through its keys must not leave the counts of its earlier keys
    (metadata, omitted placeholders, mapped presence) in the manifest: the source tallies
    describe the rows that were written, so they equal those of the import without it."""
    counted = (
        "excluded_metadata_keys",
        "omitted_not_applicable_placeholders",
        "presence_mapped",
        "presence_without_value",
        "not_applicable_placeholders_nulled",
        "fields_without_presence",
        "decoded_json_lists",
        "undecodable_list_values",
        "rows",
    )
    base = hub_build(tmp_path / "hub-a", write_dataset(tmp_path / "a")).source
    bad = gt_row(
        "D9",
        "letter",
        "email",
        "train",
        {"aaa_meta": "x", "amount_zz": "", "sender": sender_value},
        {"amount_zz": "not_applicable", "sender": sender_code, "action_items": "pending_annotation"},
    )
    data = write_dataset(tmp_path / "b", extra=[bad])
    with_bad = hub_build(tmp_path / "hub-b", data, allow_rejected=True).source
    assert len(with_bad["unparseable_gt_fields"]) == 1
    assert {k: with_bad[k] for k in counted} == {k: base[k] for k in counted}


@pytest.mark.parametrize("depth", [300, 5000])
def test_hub_value_nested_too_deeply_is_refused_per_row(tmp_path: Path, depth: int) -> None:
    """A ``gt_fields`` value nested deeper than the snapshot reader accepts is refused for
    its row (into ``unparseable_gt_fields``), never written: the gate would otherwise meet a
    corrupt line. The refused row's counts (its decoded list) stay out of the tallies."""
    base = hub_build(tmp_path / "hub-a", write_dataset(tmp_path / "a")).source
    bad = gt_row("D9", "letter", "email", "train", {}, {})
    bad["gt_fields"] = (
        '{"action_items": "[\\"a\\"]", "sender": ' + "[" * depth + "]" * depth + ', "gt_presence": '
        '"{\\"sender\\": \\"populated\\", \\"action_items\\": \\"populated\\"}"}'
    )
    data = write_dataset(
        tmp_path / "b", extra=[bad], extra_text=[{"filename": "D9.txt", "doc_text": "text-D9"}]
    )
    with pytest.raises(ImportRefused, match="unparseable_gt_fields 1"):
        hub_build(tmp_path / "hub-x", data)
    manifest = hub_build(tmp_path / "hub-x", data, allow_rejected=True)
    assert manifest.source["unparseable_gt_fields"] == [
        {"document_id": "D9", "reason": "JSON nested too deeply (more than 64 levels)"}
    ]
    assert "D9" not in hub_rows(tmp_path / "hub-x")
    assert manifest.source["decoded_json_lists"] == base["decoded_json_lists"]
    assert manifest.source["rows"] == base["rows"]


def test_hub_value_with_a_lone_surrogate_is_refused_per_row(tmp_path: Path) -> None:
    """A ``gt_fields`` string holding a lone surrogate escape cannot be written as canonical
    JSON: its row is refused into ``unparseable_gt_fields``; the import never crashes."""
    bad = gt_row("D9", "letter", "email", "train", {}, {})
    bad["gt_fields"] = '{"sender": "Ann \\ud800", "gt_presence": "{\\"sender\\": \\"populated\\"}"}'
    data = write_dataset(
        tmp_path / "b", extra=[bad], extra_text=[{"filename": "D9.txt", "doc_text": "text-D9"}]
    )
    with pytest.raises(ImportRefused, match="unparseable_gt_fields 1"):
        hub_build(tmp_path / "hub-x", data)
    manifest = hub_build(tmp_path / "hub-x", data, allow_rejected=True)
    [entry] = manifest.source["unparseable_gt_fields"]
    assert entry["document_id"] == "D9" and "lone surrogate" in entry["reason"]
    assert "D9" not in hub_rows(tmp_path / "hub-x")


def test_an_import_never_replaces_a_snapshot_of_another_kind(tmp_path: Path) -> None:
    """``import-near-gold --out`` naming an interim snapshot of the same id is refused (exit
    2, one line) and the interim snapshot is left byte for byte as it was."""
    from labels_testkit import make_snapshot

    target = make_snapshot(
        tmp_path, "interim", [interim_row("D1", {"sender": ("gap_fill", "Ann")})], snapshot_id="lab-x"
    )
    before = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    src = write_overlay(tmp_path / "src", {"letter.jsonl": jsonl(near_gold_row("D1", {"sender": "Ann"}))})
    with pytest.raises(FileExistsError, match="kind"):
        overlay(target, src, kind="near_gold")
    args = ["import-near-gold", "--out", str(target), "--source-dir", str(src), "--source-path", "x"]
    result = runner.invoke(app, [*args, "--repository", "example/sandbox", "--commit", "1" * 40])
    assert result.exit_code == 2 and "kind" in result.output, result.output
    after = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("doc_id", [None, ""])
@pytest.mark.parametrize("text_row", [True, False])
def test_hub_row_without_a_document_id_is_refused_per_row(
    tmp_path: Path, doc_id: str | None, text_row: bool
) -> None:
    """A null or empty ``document_id`` is refused for its row (``unparseable_gt_fields``),
    with or without a text row for it; the import never crashes sorting the ids."""
    bad = gt_row("X1", "letter", "email", "train", {"sender": "a"}, {"sender": "populated"})
    bad["document_id"] = doc_id
    text = [{"filename": "X1.txt", "doc_text": "text-X1"}] if text_row else []
    data = write_dataset(tmp_path / "b", extra=[bad], extra_text=text)
    with pytest.raises(ImportRefused, match="unparseable_gt_fields 1"):
        hub_build(tmp_path / "hub-x", data)
    manifest = hub_build(tmp_path / "hub-x", data, allow_rejected=True)
    assert manifest.source["unparseable_gt_fields"] == [
        {"document_id": doc_id, "reason": "document_id must be a non-empty string"}
    ]
    assert sorted(hub_rows(tmp_path / "hub-x")) == ["D1", "D2", "D3", "F1"]


@pytest.mark.parametrize("doc_id", [5, ["D1"], {"id": "D1"}, True])
def test_hub_row_with_a_non_string_document_id_is_refused(doc_id: Any) -> None:
    from mailroom_labels.importers import hub

    tally = hub._Tally()
    raw = gt_row("X1", "letter", "email", "train", {"sender": "a"}, {"sender": "populated"})
    assert hub._fields({**raw, "document_id": doc_id}, profile(), tally) is None
    assert [e["reason"] for e in tally.bad_gt_fields] == ["document_id must be a non-empty string"]


def _nested(depth: int) -> list[Any]:
    """A list ``depth`` lists deep (``[]`` is 1)."""
    value: list[Any] = []
    for _ in range(depth - 1):
        value = [value]
    return value


TOO_DEEP = "JSON nested too deeply (more than 64 levels)"
SURROGATE = "a string holds a lone surrogate (not encodable as UTF-8)"


def test_an_overlay_row_the_reader_would_refuse_is_a_rejected_line(tmp_path: Path) -> None:
    """A source row nested deeper than the snapshot reader allows, or holding a lone
    surrogate, is listed in ``rejected_lines`` like any other bad line: never a crash."""
    good = interim_row("D1", {"sender": ("gap_fill", "Ann")})
    deep = interim_row("D2", {"sender": ("gap_fill", _nested(80))})
    lone = interim_row("D3", {"sender": ("gap_fill", "a\ud800b")})
    src = write_overlay(tmp_path / "src", {"letter.jsonl": jsonl(good, deep, lone)})
    with pytest.raises(ImportRefused, match="rejected_lines 2"):
        overlay(tmp_path / "interim-x", src)
    manifest = overlay(tmp_path / "interim-x", src, allow_rejected=True)
    assert [r.data for r in load_snapshot(tmp_path / "interim-x").rows()] == [good]
    assert manifest.source["rejected_lines"] == [
        {"file": "letter.jsonl", "line": 2, "reason": f"not a snapshot row: {TOO_DEEP}"},
        {"file": "letter.jsonl", "line": 3, "reason": f"not a snapshot row: {SURROGATE}"},
    ]


def test_a_review_line_the_reader_would_refuse_is_a_rejected_line(tmp_path: Path) -> None:
    """The same for the review importer, including a desk line that fits the depth limit as
    a source line but not once its value is moved under ``entry``."""
    lone = decision("D2", "sender", "Bo")
    lone["note"] = "a\ud800b"
    edge = desk("D4", "sender", _nested(63))  # the line is 64 deep; the written row 65
    with pytest.raises(ImportRefused, match="rejected_lines 3"):
        review(
            tmp_path, [decision("D1", "sender", "Ann"), decision("D3", "sender", _nested(80)), lone], [edge]
        )
    manifest, rows = review(
        tmp_path,
        [decision("D1", "sender", "Ann"), decision("D3", "sender", _nested(80)), lone],
        [edge],
        allow_rejected=True,
    )
    assert [r["doc_id"] for r in rows] == ["D1"]
    assert manifest.source["rejected_lines"] == [
        {"file": "review/decisions.jsonl", "line": 2, "reason": f"not a snapshot row: {TOO_DEEP}"},
        {"file": "review/decisions.jsonl", "line": 3, "reason": f"not a snapshot row: {SURROGATE}"},
        {"file": "sandbox/human_reviews.jsonl", "line": 1, "reason": f"not a snapshot row: {TOO_DEEP}"},
    ]


def test_hub_entity_list_string_too_deep_for_the_decoder_is_refused_per_row(tmp_path: Path) -> None:
    """An entity-list value given as a JSON string too deep for the decoder (a
    ``RecursionError`` at a depth that depends on the Python version) refuses its row for
    its depth on every version; it is never kept as a raw string on one version and refused
    on another."""
    depth = 200_000
    bad = gt_row("D9", "letter", "email", "train", {}, {})
    bad["gt_fields"] = json.dumps(
        {
            "action_items": "[" * depth + "]" * depth,
            "gt_presence": json.dumps({"action_items": "populated"}),
        }
    )
    data = write_dataset(
        tmp_path / "b", extra=[bad], extra_text=[{"filename": "D9.txt", "doc_text": "text-D9"}]
    )
    manifest = hub_build(tmp_path / "hub-x", data, allow_rejected=True)
    assert manifest.source["unparseable_gt_fields"] == [
        {"document_id": "D9", "reason": "JSON nested too deeply (more than 64 levels)"}
    ]
    assert "D9" not in hub_rows(tmp_path / "hub-x")
