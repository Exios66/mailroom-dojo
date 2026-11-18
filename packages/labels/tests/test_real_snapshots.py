"""Golden checks on the committed real snapshots (no network): they verify, they resist
tampering, their counts are pinned, and the resolver behaves on real positive controls.

A failing count here means the committed data or the resolver changed: re-run the importers
with ``--check`` and the report script, and update the numbers only for an intended change.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mailroom_labels import load_label_set, load_profile
from mailroom_labels.errors import ManifestMismatch
from mailroom_labels.layers import read_layers
from mailroom_labels.cli import app
from mailroom_labels.quality import check_dirs, verify
from mailroom_labels.snapshot import MANIFEST_NAME, load_snapshot, load_snapshot_set

ROOT = Path(__file__).resolve().parents[3]
PROFILE = ROOT / "config/clients/mailroom-dataset/profile.yaml"
SNAPSHOTS = {
    "hub-ccd040f6": "hub",
    "interim-2026-11-01": "interim",
    "near-gold-2026-11-17": "near_gold",
    "gold-review-2026-11-18": "review",
}
DIRS = [ROOT / "labels" / name for name in SNAPSHOTS]
REPORTS = ROOT / "docs/reports/labels"


@lru_cache(maxsize=1)
def label_set():
    return load_label_set(DIRS, load_profile(PROFILE))


@lru_cache(maxsize=1)
def records():
    return read_layers(load_snapshot_set(DIRS))


def doc(doc_id: str):
    return label_set().document(doc_id)


# --- integrity --------------------------------------------------------------------------


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_committed_snapshot_verifies(name: str) -> None:
    snap = load_snapshot(ROOT / "labels" / name)
    assert (snap.snapshot_id, snap.kind) == (name, SNAPSHOTS[name])


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_flipping_a_byte_in_every_file_is_caught(tmp_path: Path, name: str) -> None:
    src = ROOT / "labels" / name
    targets = [p.relative_to(src).as_posix() for p in sorted(src.rglob("*")) if p.is_file()]
    assert MANIFEST_NAME in targets and len(targets) >= 2
    for rel in targets:
        copy = tmp_path / rel.replace("/", "_") / name
        shutil.copytree(src, copy)
        path = copy / rel
        data = bytearray(path.read_bytes())
        i = len(data) // 2
        data[i] = ord("x") if data[i] != ord("x") else ord("y")
        path.write_bytes(bytes(data))
        with pytest.raises(ManifestMismatch) as info:
            load_snapshot(copy)
        assert info.value.kind in ("changed", "manifest_hash", "manifest_invalid"), rel


def test_zero_errors_and_every_warning_known_with_a_reason() -> None:
    report, loaded = check_dirs(DIRS, load_profile(PROFILE))
    assert report.errors == ()
    assert Counter(f.kind for f in report.warnings) == {"orphan_decision": 7, "subclass_mismatch": 6}
    assert all(f.known for f in report.warnings)
    reasons = [w.get("reason") for s in loaded for w in s.manifest.known_warnings]
    assert len(reasons) == 13 and all(isinstance(r, str) and len(r) > 40 for r in reasons)
    # each warning is accepted by the snapshot that raised it, and the strict gate passes
    owners = Counter((f.kind, f.location.split("/")[0]) for f in report.warnings)
    assert owners == {("orphan_decision", "gold-review-2026-11-18"): 7, ("subclass_mismatch", "interim-2026-11-01"): 6}
    assert verify(report, strict=True) == 0
    result = CliRunner().invoke(app, ["verify", "--strict", "--profile", str(PROFILE), *map(str, DIRS)])
    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines()[-1] == "0 error(s), 13 warning(s)"


# --- pinned counts ----------------------------------------------------------------------

LAYER_COUNTS = {  # (snapshot, class): (documents, labels)
    ("gold-review-2026-11-18", "corporate_record"): (6, 8),
    ("gold-review-2026-11-18", "correspondence"): (255, 1042),
    ("hub-ccd040f6", "contract"): (509, 509),
    ("hub-ccd040f6", "corporate_record"): (450, 2632),
    ("hub-ccd040f6", "correspondence"): (1000, 5000),
    ("hub-ccd040f6", "insurance_claim"): (1100, 17600),
    ("hub-ccd040f6", "merger_agreement"): (152, 608),
    ("interim-2026-11-01", "contract"): (45, 57),
    ("interim-2026-11-01", "corporate_record"): (200, 1006),
    ("interim-2026-11-01", "correspondence"): (300, 1074),
    ("interim-2026-11-01", "insurance_claim"): (536, 536),
    ("interim-2026-11-01", "merger_agreement"): (30, 94),
    ("near-gold-2026-11-17", "contract"): (110, 900),
    ("near-gold-2026-11-17", "corporate_record"): (63, 111),
    ("near-gold-2026-11-17", "correspondence"): (780, 4835),
}


def test_documents_and_labels_per_layer_and_class() -> None:
    read = records()
    index = {e.document_id: e for e in read.corpus}
    docs, labels = Counter(), Counter()
    seen = set()
    for r in read.records:
        key = (r.snapshot_id, index[r.document_id].doc_class)
        labels[key] += 1
        if (key, r.document_id) not in seen:
            seen.add((key, r.document_id))
            docs[key] += 1
    assert {k: (docs[k], labels[k]) for k in labels} == LAYER_COUNTS
    assert Counter(e.doc_class for e in read.corpus) == {
        "contract": 600, "corporate_record": 450, "correspondence": 1000, "insurance_claim": 1100, "merger_agreement": 152}


SCORABLE = {  # class: (documents, verified, scorable documents, {status: scorable labels})
    "contract": (600, 600, 539, {"gold": 0, "hub": 509, "interim": 45, "near_gold": 900}),
    "corporate_record": (450, 444, 444, {"gold": 8, "hub": 2188, "interim": 617, "near_gold": 74}),
    "correspondence": (1000, 1000, 1000, {"gold": 0, "hub": 5000, "interim": 933, "near_gold": 4328}),
    "insurance_claim": (1100, 1100, 1100, {"gold": 0, "hub": 17600, "interim": 536, "near_gold": 0}),
    "merger_agreement": (152, 152, 152, {"gold": 0, "hub": 152, "interim": 93, "near_gold": 0}),
}


def test_resolved_scorable_counts_per_class_and_status() -> None:
    got: dict[str, list] = {}
    for d in label_set().documents():
        row = got.setdefault(d.doc_class, [0, 0, 0, Counter()])
        row[0] += 1
        row[1] += d.verified
        row[2] += bool(d.scorable_fields)
        row[3].update(d.fields[f].status for f in d.scorable_fields)
    assert {c: (a, b, s, {k: st[k] for k in ("gold", "hub", "interim", "near_gold")})
            for c, (a, b, s, st) in got.items()} == SCORABLE


# --- behaviour on real positive controls --------------------------------------------------


def test_every_human_desk_decision_resolves_to_gold() -> None:
    gold = [(d.document_id, f) for d in label_set().documents() for f, lab in d.fields.items() if lab.status == "gold"]
    assert len(gold) == 8
    assert doc("DOC-b0801e4d4bb58191").fields["signatories"].value == [
        "Melanie Figueroa", "Anthony C. Pintsopoulos", "Jawahar Tandon", "Vivek Tandon", "David H. Clarke"]
    absent = doc("DOC-bda69bc956881bab").fields["jurisdiction"]
    assert (absent.status, absent.value, absent.presence) == ("gold", None, "schema_documented_absence")


def test_audit_decision_that_differs_from_the_catalog_wins_over_near_gold() -> None:
    lab = doc("DOC-672cec2c85c83c39").fields["action_items"]
    assert (lab.rank, lab.status, lab.source) == ("review:near_gold", "near_gold", "review:claude-opus-audit")
    assert lab.value == ["please continue to spread the word", "Check it out!"]
    assert [(f.rank, f.value) for f in lab.layers_seen] == [
        ("review:near_gold", ["please continue to spread the word", "Check it out!"]), ("near_gold", [])]


def test_populated_hub_is_never_replaced_by_a_fill_or_near_gold() -> None:
    checked = 0
    for d in label_set().documents():
        for lab in d.fields.values():
            fresh = {f.rank for f in lab.layers_seen if not f.stale}
            if "hub" in fresh and fresh & {"interim:gap_fill", "interim:derived", "near_gold", "review:near_gold"}:
                checked += 1
            if "hub" in fresh:
                assert lab.rank in ("hub", "review:gold", "interim:correction")
    assert checked > 0  # the rule is exercised, not vacuous
    lab = doc("DOC-01054efd0980066b").fields["entity_name"]
    assert (lab.rank, lab.value) == ("hub", "Bravo Brio Restaurant Group, Inc.")
    assert [f.rank for f in lab.layers_seen] == ["hub", "interim:gap_fill"]


def test_interim_fills_replace_a_hub_absence_like_the_sandbox() -> None:
    fills = Counter()
    for d in label_set().documents():
        for name, lab in d.fields.items():
            if "hub:absent" in {f.rank for f in lab.layers_seen} and lab.rank.startswith("interim:"):
                fills[(name, lab.rank)] += 1
    assert fills == {("filing_number", "interim:gap_fill"): 136, ("jurisdiction", "interim:gap_fill"): 14,
                     ("signatories", "interim:gap_fill"): 21}
    lab = doc("DOC-02e5b3d7ea700ab0").fields["filing_number"]
    assert [f.rank for f in lab.layers_seen] == ["interim:gap_fill", "hub:absent"] and lab.rank == "interim:gap_fill"


def test_disputed_interim_labels_are_never_truth() -> None:
    disputed = [(d.document_id, n) for d in label_set().documents() for n, facts in d.untrusted.items()
                if any(f.rank == "interim:disputed" for f in facts)]
    in_fields = [(d.document_id, n) for d in label_set().documents() for n, lab in d.fields.items()
                 if lab.rank == "interim:disputed"]
    assert in_fields == []
    assert ("DOC-0a29136832720e65", "merger_consideration") in disputed


def test_audit_never_overrides_a_hub_value_or_a_correction() -> None:
    for d in label_set().documents():
        for lab in d.fields.values():
            ranks = {f.rank for f in lab.layers_seen}
            if "review:near_gold" in ranks and ranks & {"hub", "interim:correction"}:
                assert lab.rank in ("hub", "interim:correction", "review:gold")


def test_subclass_stale_documents_are_unverified_and_not_scored() -> None:
    stale = sorted(d.document_id for d in label_set().documents() if not d.verified)
    assert stale == ["DOC-033adce2fbdff0ec", "DOC-4a8f1db0546e42be", "DOC-5c087a08edcb4a91",
                     "DOC-691191d21d96b75d", "DOC-e00ef4799dbd6b37", "DOC-ea4b0e70dc4677d1"]
    assert all(doc(x).scorable_fields == frozenset() and doc(x).unverified_reasons == ("stale_label",) for x in stale)


def test_every_label_document_is_in_the_hub_index() -> None:
    read = records()
    index = {e.document_id for e in read.corpus}
    assert {r.document_id for r in read.records} <= index
    assert len(index) == 3302


def test_profile_covers_every_field_of_every_snapshot() -> None:
    profile = load_profile(PROFILE)
    read = records()
    classes = {e.document_id: e.doc_class for e in read.corpus}
    missing = {(classes[r.document_id], r.field) for r in read.records
               if profile.field_spec(classes[r.document_id], r.field) is None}
    assert missing == set()
    assert set(profile.classes) == {"contract", "corporate_record", "correspondence", "insurance_claim", "merger_agreement"}
    assert all(spec.subclasses for spec in profile.classes.values())


def test_label_set_identity_is_stable() -> None:
    ls = label_set()
    assert ls.snapshot_id == "gold-review-2026-11-18+hub-ccd040f6+interim-2026-11-01+near-gold-2026-11-17"
    assert [r["snapshot_id"] for r in ls.snapshot_refs()] == sorted(SNAPSHOTS)


# --- the committed reports are current ------------------------------------------------------


def test_committed_reports_match_a_fresh_build(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("labels_report_under_test", ROOT / "packages/labels/scripts/labels_report.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    crosscheck = json.loads((REPORTS / "sandbox-crosscheck-2026-11-18.json").read_text(encoding="utf-8"))
    quality_md, cov, cov_md = module.build(DIRS, PROFILE, crosscheck, [("DOC-672cec2c85c83c39", "action_items")], "2026-11-18")
    assert quality_md == (REPORTS / "data-quality-2026-11-18.md").read_text(encoding="utf-8")
    assert cov_md == (REPORTS / "coverage-2026-11-18.md").read_text(encoding="utf-8")
    assert json.dumps(cov, indent=2, sort_keys=True) + "\n" == (REPORTS / "coverage-2026-11-18.json").read_text(encoding="utf-8")


# --- the layout map names what this package added ---------------------------------------------


def test_layout_map_names_the_label_folders() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    block = readme.split("## Layout", 1)[1].split("```")[1]
    for line_start in ("├── labels/", "│   │   └── mailroom-dataset/", "│       └── labels/"):
        assert any(line.startswith(line_start) for line in block.splitlines()), line_start
    entries = sorted(p.name for p in (ROOT / "labels").iterdir())
    assert entries == sorted([*SNAPSHOTS, "README.md"])
    assert all((ROOT / "labels" / name / MANIFEST_NAME).is_file() for name in SNAPSHOTS)
    assert sorted(p.name for p in (ROOT / "config/clients/mailroom-dataset").iterdir()) == [
        "hub-revisions.yaml", "known-warnings.yaml", "profile.yaml"]
