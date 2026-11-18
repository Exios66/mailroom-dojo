"""Plain helpers shared by the labels tests (fixtures live in conftest.py).

Everything is synthetic: invented document ids, classes and values, no real document text,
no network, no model. Row builders follow the native shapes documented in
``mailroom_labels.layers``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from mailroom_labels.layers import CURRENT_LAYER_SCHEMA
from mailroom_labels.profile import ClientProfile, load_profile
from mailroom_labels.snapshot import MANIFEST_NAME, manifest_hash_of, write_snapshot

PROFILE_PATH = Path(__file__).parent / "fixtures" / "synthetic-profile.yaml"

UNSET = object()


def profile() -> ClientProfile:
    """The synthetic client profile."""
    return load_profile(PROFILE_PATH)


def text_hash(doc: str) -> str:
    """The synthetic content hash of a document id (as if its text were ``text-<doc>``)."""
    return hashlib.sha256(f"text-{doc}".encode()).hexdigest()


def hub_row(doc: str, doc_class: str = "letter", subclass: str | None = "email",
            fields: dict[str, Any] | None = None, *, sha: Any = UNSET) -> dict[str, Any]:
    """A Hub row. ``fields`` maps name -> value (presence populated, or
    schema_documented_absence for None) or name -> (value, presence)."""
    out = {}
    for name, spec in (fields or {}).items():
        if isinstance(spec, tuple):
            out[name] = {"value": spec[0], "presence": spec[1]}
        else:
            out[name] = {"value": spec, "presence": "populated" if spec is not None else "schema_documented_absence"}
    return {
        "document_id": doc,
        "doc_class": doc_class,
        "subclass": subclass,
        "split": "test",
        "content_sha256": text_hash(doc) if sha is UNSET else sha,
        "filename": f"{doc}.txt",
        "fields": out,
    }


def interim_row(doc: str, labels: dict[str, tuple[str, Any]], doc_class: str = "letter",
                subclass: str | None = "email", *, sha: Any = UNSET) -> dict[str, Any]:
    """An interim overlay row; ``labels`` maps field -> (status, value)."""
    truth = ("gap_fill", "derived", "correction")
    return {
        "doc_class": doc_class,
        "doc_id": doc,
        "doc_sha256": text_hash(doc)[:16] if sha is UNSET else sha,
        "doc_subclass": subclass,
        "labels": {
            f: {"basis": "synthetic", "status": st, "use_as_truth": st in truth, "value": v}
            for f, (st, v) in labels.items()
        },
        "source": {"gate_run": "gate-synthetic"},
    }


def near_gold_row(doc: str, labels: dict[str, Any], doc_class: str = "letter",
                  subclass: str | None = "email", *, sha: Any = UNSET,
                  basis: str = "near_gold:two_pass_agree") -> dict[str, Any]:
    """A near-gold overlay row; ``labels`` maps field -> value."""
    return {
        "doc_class": doc_class,
        "doc_id": doc,
        "doc_sha256": text_hash(doc)[:16] if sha is UNSET else sha,
        "doc_subclass": subclass,
        "labels": {
            f: {
                "basis": basis,
                "label_id": f"{doc}-{f}",
                "labeler": {"batch": "b0", "model": "synthetic-model", "role": "label"},
                "presence": "populated" if v is not None else "schema_documented_absence",
                "status": "gap_fill",
                "use_as_truth": False,
                "value": v,
            }
            for f, v in labels.items()
        },
    }


def review_row(doc: str, field: str, value: Any, *, human: bool = False, verdict: str = "correct",
               absent: str | None = None) -> dict[str, Any]:
    """A review decision line."""
    entry: dict[str, Any] = {"value": value}
    if absent is not None:
        entry["absent"] = absent
    else:
        entry["evidence"] = ["synthetic evidence"]
    return {
        "doc_id": doc,
        "field": field,
        "verdict": verdict,
        "reviewer": {"model": "human", "role": "desk"} if human else {"model": "synthetic-auditor", "role": "audit"},
        "note": "synthetic",
        "entry": entry,
    }


def make_snapshot(root: Path, kind: str, rows: list[dict[str, Any]], *, snapshot_id: str | None = None,
                  file: str = "rows.jsonl", source: dict[str, Any] | None = None,
                  known_warnings: tuple = ()) -> Path:
    """Write a one-file snapshot of ``kind`` under ``root/<snapshot_id>``; return its path."""
    sid = snapshot_id or f"{kind}-test"
    directory = root / sid
    write_snapshot(
        directory,
        {file: rows},
        snapshot_id=sid,
        kind=kind,
        layer_schema=CURRENT_LAYER_SCHEMA[kind],
        source=source or {"repository": "synthetic"},
        builder="labels_testkit",
        known_warnings=known_warnings,
    )
    return directory


def rehash_manifest(directory: Path, edit: Any = None) -> None:
    """Re-hash ``manifest.json`` after a hand edit (the tamperer that also fixes the hash).
    ``edit`` is called with the manifest dict first; file facts are recomputed from disk."""
    path = directory / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        data = (directory / entry["path"]).read_bytes()
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        entry["bytes"] = len(data)
        entry["rows"] = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
    if edit is not None:
        edit(manifest)
    manifest["manifest_hash"] = manifest_hash_of(manifest)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def standard_set(root: Path) -> list[Path]:
    """A small, clean four-layer set used by many tests (zero quality errors)."""
    hub = make_snapshot(root, "hub", [
        hub_row("D1", fields={"sender": "Ann", "recipient": None, "urgency": "routine"}),
        hub_row("D2", fields={"sender": ("", "unlabeled"), "amount": 12.5}),
        hub_row("D3", "filing", "annual", {"entity": "Acme", "kind": "annual"}),
        hub_row("D4", fields={}),
    ])
    interim = make_snapshot(root, "interim", [
        interim_row("D1", {"sender": ("gap_fill", "Ann B."), "recipient": ("correction", "Bob")}),
        interim_row("D2", {"sender": ("gap_fill", "Cy"), "urgency": ("disputed", "urgent")}),
        interim_row("D3", {"signers": ("gap_fill", ["P. Q."])}, "filing", "annual"),
    ])
    near_gold = make_snapshot(root, "near_gold", [
        near_gold_row("D1", {"sent_on": "2020-01-02", "sender": "Ann C."}),
        near_gold_row("D2", {"action_items": ["call back"], "sender": "Cyrus"}),
        near_gold_row("D4", {"sender": "Dee"}),
    ])
    review = make_snapshot(root, "review", [
        review_row("D1", "sent_on", "2020-01-03", human=True),
        review_row("D2", "action_items", [], absent="nothing asked"),
    ])
    return [hub, interim, near_gold, review]
