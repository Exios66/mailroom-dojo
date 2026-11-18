"""The resolver against a hand-written oracle, its invariants under random layers, the
sandbox-parity table, and the clean synthetic set resolved by hand."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from labels_testkit import hub_row, interim_row, make_snapshot, near_gold_row, profile, review_row, text_hash

from mailroom_labels.layers import corpus_index, read_layers
from mailroom_labels.models import PRESENCES, CorpusEntry, LayerRecord
from mailroom_labels.resolve import resolve
from mailroom_labels.snapshot import load_snapshot_set

PROFILE = profile()

# --- the oracle ---------------------------------------------------------------------
# Written from the plan's decision 1 and 4, independently of resolve.py: a table of rank
# positions, a staleness rule and a scorability rule. It hard-codes the synthetic
# profile's precedence on purpose, so a profile-reading bug in resolve.py shows up.

ORACLE_RANK = {
    "review:gold": 0,
    "interim:correction": 1,
    "hub": 2,
    "interim:gap_fill": 3,
    "interim:derived": 4,
    "review:near_gold": 5,
    "near_gold": 6,
}
ORACLE_STATUS = {
    "review:gold": "gold", "interim:correction": "interim", "hub": "hub", "interim:gap_fill": "interim",
    "interim:derived": "interim", "review:near_gold": "near_gold", "near_gold": "near_gold",
}
ORACLE_FIELDS = {"letter": {"sender", "recipient", "sent_on", "urgency", "action_items", "amount"},
                 "filing": {"entity", "filing_id", "signers", "kind"}}


def oracle_stale(r: LayerRecord, entry: CorpusEntry | None) -> bool:
    """A label disagrees with the index: hash not a prefix of the indexed hash, or class differs."""
    if entry is None:
        return False
    if r.content_sha256 is not None and not (entry.content_sha256 or "").startswith(r.content_sha256):
        return True
    if r.doc_class is not None and r.doc_class != entry.doc_class:
        return True
    return r.subclass is not None and r.subclass != entry.subclass


def oracle(records: list[LayerRecord], index: dict[str, CorpusEntry]) -> dict:
    out = {}
    for doc in sorted({r.document_id for r in records} | set(index)):
        mine = [r for r in records if r.document_id == doc]
        entry = index.get(doc)

        def stale(r: LayerRecord) -> bool:
            return oracle_stale(r, entry)

        verified = entry is not None and entry.text_verified and not any(stale(r) for r in mine)
        doc_class = entry.doc_class if entry else next(
            (r.doc_class for r in sorted(mine, key=lambda r: (r.snapshot_id, r.file, r.line)) if r.doc_class), None)
        fields, untrusted = {}, set()
        for fld in sorted({r.field for r in mine}):
            truth = [r for r in mine if r.field == fld and not stale(r) and r.rank in ORACLE_RANK]
            if not truth:
                untrusted.add(fld)
                continue
            win = sorted(truth, key=lambda r: (ORACLE_RANK[r.rank], r.snapshot_id, r.file, r.line,
                                               json.dumps(r.value, sort_keys=True)))[0]
            fields[fld] = (win.value, ORACLE_STATUS[win.rank], win.rank, win.presence)
        scorable = {
            f for f, (_, _, _, presence) in fields.items()
            if verified and f in ORACLE_FIELDS.get(doc_class, set()) and presence != "not_applicable"
        }
        out[doc] = (verified, fields, untrusted, scorable)
    return out


def as_oracle_shape(docs) -> dict:
    return {
        d.document_id: (
            d.verified,
            {f: (lab.value, lab.status, lab.rank, lab.presence) for f, lab in d.fields.items()},
            set(d.untrusted),
            set(d.scorable_fields),
        )
        for d in docs
    }


# --- strategies ---------------------------------------------------------------------

DOCS = ("D1", "D2", "D3")
FIELDS = ("sender", "recipient", "urgency", "entity", "zzz")
RANK_CHOICES = (*ORACLE_RANK, "interim:disputed")
LAYER_OF = {"hub": "hub", "near_gold": "near_gold"}


@st.composite
def layer_sets(draw):
    n = draw(st.integers(0, 14))
    records = []
    for i in range(n):
        doc = draw(st.sampled_from(DOCS))
        rank = draw(st.sampled_from(RANK_CHOICES))
        layer = LAYER_OF.get(rank, rank.split(":")[0])
        sha = draw(st.sampled_from([None, "good", "bad"]))
        records.append(LayerRecord(
            document_id=doc,
            field=draw(st.sampled_from(FIELDS)),
            value=draw(st.sampled_from(["a", "b", None, ["x"]])),
            presence=draw(st.sampled_from(PRESENCES)),
            rank=rank,
            layer=layer,
            native_status=rank,
            source=f"src-{rank}",
            content_sha256=None if sha is None else (text_hash(doc)[:16] if sha == "good" else "0" * 16),
            doc_class=draw(st.sampled_from([None, "letter", "letter", "filing"])),
            subclass=draw(st.sampled_from([None, "email"])),
            snapshot_id=f"s{draw(st.integers(0, 2))}",
            file="rows.jsonl",
            line=i + 1,
        ))
    index = {}
    for doc in DOCS:
        if draw(st.booleans()) or doc == "D1":
            index[doc] = CorpusEntry(
                document_id=doc,
                doc_class="letter",
                subclass=draw(st.sampled_from(["email", None])),
                content_sha256=draw(st.sampled_from([text_hash(doc), text_hash(doc), None])),
                text_verified=draw(st.booleans()) or doc == "D1",
            )
    return records, index


@given(layer_sets())
def test_resolver_equals_the_oracle(data) -> None:
    records, index = data
    assert as_oracle_shape(resolve(records, PROFILE, index)) == oracle(records, index)


@given(layer_sets(), st.randoms(use_true_random=False))
def test_resolver_invariants(data, rnd) -> None:
    records, index = data
    docs = resolve(records, PROFILE, index)
    shuffled = list(records)
    rnd.shuffle(shuffled)
    assert resolve(shuffled, PROFILE, index) == docs  # input order never matters
    assert resolve(records, PROFILE, index) == docs  # resolving twice is the same
    assert [d.document_id for d in docs] == sorted(d.document_id for d in docs)
    for doc in docs:
        mine = [r for r in records if r.document_id == doc.document_id]
        if not doc.verified:
            assert doc.scorable_fields == frozenset()
            assert doc.unverified_reasons
        for name, label in doc.fields.items():
            sources = [r for r in mine if r.field == name and r.rank == label.rank and r.value == label.value]
            assert sources, "every resolved value comes from some layer"
            assert label.rank != "interim:disputed"
            fresh = {r.rank for r in mine if r.field == name and not oracle_stale(r, index.get(doc.document_id))}
            if "review:gold" in fresh:
                assert label.rank == "review:gold", "a gold decision always wins"
            if "hub" in fresh:
                assert label.rank in {"hub", "review:gold", "interim:correction"}, "Hub is never replaced by a fill"
            assert len(label.layers_seen) == len([r for r in mine if r.field == name])
        for name, facts in doc.untrusted.items():
            assert name not in doc.fields
            assert all(f.stale or f.rank == "interim:disputed" for f in facts)


# --- worked examples ----------------------------------------------------------------


def resolve_dirs(dirs) -> dict:
    read = read_layers(load_snapshot_set(dirs))
    return {d.document_id: d for d in resolve(read.records, PROFILE, corpus_index(read.corpus))}


def test_standard_set_resolves_as_computed_by_hand(snapshot_dirs) -> None:
    docs = resolve_dirs(snapshot_dirs)
    d1, d2, d3, d4 = (docs[k] for k in ("D1", "D2", "D3", "D4"))
    assert {f: (lab.value, lab.status) for f, lab in d1.fields.items()} == {
        "sender": ("Ann", "hub"),  # Hub beats gap_fill and near_gold
        "recipient": ("Bob", "interim"),  # correction replaces the Hub's documented absence
        "sent_on": ("2020-01-03", "gold"),  # human review beats near_gold
        "urgency": ("routine", "hub"),
    }
    assert d1.fields["sent_on"].reviewer == "human:desk"
    assert [f.rank for f in d1.fields["sender"].layers_seen] == ["hub", "interim:gap_fill", "near_gold"]
    assert {f: (lab.value, lab.rank) for f, lab in d2.fields.items()} == {
        "sender": ("Cy", "interim:gap_fill"),  # Hub left it unlabelled; gap_fill beats near_gold
        "amount": (12.5, "hub"),
        "action_items": ([], "review:near_gold"),  # the audit decision beats the near_gold label
    }
    assert d2.fields["action_items"].presence == "schema_documented_absence"
    assert [f.rank for f in d2.untrusted["urgency"]] == ["interim:disputed"]
    assert {f: lab.value for f, lab in d3.fields.items()} == {"entity": "Acme", "kind": "annual", "signers": ["P. Q."]}
    assert d4.fields["sender"].status == "near_gold"
    assert all(d.verified for d in docs.values())
    assert d1.scorable_fields == frozenset({"sender", "recipient", "sent_on", "urgency"})


def test_scorable_statuses_come_from_the_profile(snapshot_dirs) -> None:
    strict = PROFILE.model_copy(update={"scorable_statuses": frozenset({"hub", "gold"})})
    read = read_layers(load_snapshot_set(snapshot_dirs))
    docs = {d.document_id: d for d in resolve(read.records, strict, corpus_index(read.corpus))}
    assert docs["D1"].scorable_fields == frozenset({"sender", "sent_on", "urgency"})
    assert docs["D4"].scorable_fields == frozenset()


def test_precedence_comes_from_the_profile(snapshot_dirs) -> None:
    near_gold_first = PROFILE.model_copy(update={"precedence": ("near_gold", "hub", "interim:gap_fill")})
    read = read_layers(load_snapshot_set(snapshot_dirs))
    docs = {d.document_id: d for d in resolve(read.records, near_gold_first, corpus_index(read.corpus))}
    assert docs["D1"].fields["sender"].value == "Ann C."
    assert "recipient" in docs["D1"].fields and docs["D1"].fields["recipient"].rank == "hub"  # correction not ranked


# --- parity with the sandbox (interim_labels.apply_overlay) --------------------------
# Restates today's rules: Hub labels win over gap_fill, derived and near_gold; a correction
# replaces the Hub value; a disputed label is never applied; a document whose text hash
# does not match the overlay's is left untouched (here: the overlay label is never truth
# and the document is flagged unverified, so it is not scored either).

PARITY = [
    # (hub field spec or None, layer, overlay status, expected (value, rank) or None)
    ("Hub", "interim", "gap_fill", ("Hub", "hub")),
    (None, "interim", "gap_fill", (None, "hub")),  # a documented absence is a Hub label
    ((None, "not_applicable"), "interim", "gap_fill", (None, "hub")),
    (("", "unlabeled"), "interim", "gap_fill", ("Fill", "interim:gap_fill")),
    ("Hub", "interim", "derived", ("Hub", "hub")),
    (("", "unlabeled"), "interim", "derived", ("Fill", "interim:derived")),
    ("Hub", "interim", "correction", ("Fill", "interim:correction")),
    (None, "interim", "correction", ("Fill", "interim:correction")),
    ("Hub", "interim", "disputed", ("Hub", "hub")),
    (("", "unlabeled"), "interim", "disputed", None),
    ("Hub", "near_gold", None, ("Hub", "hub")),
    (("", "unlabeled"), "near_gold", None, ("Fill", "near_gold")),
]


@pytest.mark.parametrize(("hub", "layer", "status", "expected"), PARITY)
def test_sandbox_parity(tmp_path: Path, hub, layer, status, expected) -> None:
    dirs = [make_snapshot(tmp_path, "hub", [hub_row("D1", fields={"sender": hub})])]
    if layer == "interim":
        dirs.append(make_snapshot(tmp_path, "interim", [interim_row("D1", {"sender": (status, "Fill")})]))
    else:
        dirs.append(make_snapshot(tmp_path, "near_gold", [near_gold_row("D1", {"sender": "Fill"})]))
    doc = resolve_dirs(dirs)["D1"]
    if expected is None:
        assert "sender" not in doc.fields and "sender" in doc.untrusted
    else:
        assert (doc.fields["sender"].value, doc.fields["sender"].rank) == expected
    assert doc.verified


@pytest.mark.parametrize("layer", ["interim", "near_gold"])
def test_sandbox_parity_hash_mismatch_leaves_the_document_untouched(tmp_path: Path, layer: str) -> None:
    dirs = [make_snapshot(tmp_path, "hub", [
        hub_row("D1", fields={"sender": ("", "unlabeled"), "recipient": "Hub"}),
        hub_row("D2", fields={"sender": "Other"}),
    ])]
    wrong = "f" * 16
    if layer == "interim":
        dirs.append(make_snapshot(tmp_path, "interim", [
            interim_row("D1", {"sender": ("gap_fill", "Fill"), "recipient": ("correction", "Fix")}, sha=wrong),
            interim_row("D2", {"recipient": ("gap_fill", "Ok")}),
        ]))
    else:
        dirs.append(make_snapshot(tmp_path, "near_gold", [
            near_gold_row("D1", {"sender": "Fill", "recipient": "Fix"}, sha=wrong),
            near_gold_row("D2", {"recipient": "Ok"}),
        ]))
    docs = resolve_dirs(dirs)
    d1 = docs["D1"]
    assert "sender" not in d1.fields and d1.untrusted["sender"][0].stale
    assert d1.fields["recipient"].value == "Hub"  # untouched: the stale correction is not applied
    assert (d1.verified, d1.unverified_reasons, d1.scorable_fields) == (False, ("stale_label",), frozenset())
    # the neighbour is not blocked
    assert docs["D2"].verified and docs["D2"].fields["recipient"].value == "Ok"
    assert docs["D2"].scorable_fields == frozenset({"sender", "recipient"})


def test_document_missing_from_the_index_is_resolved_but_not_scorable(tmp_path: Path) -> None:
    dirs = [
        make_snapshot(tmp_path, "hub", [hub_row("D1", fields={"sender": "Ann"})]),
        make_snapshot(tmp_path, "near_gold", [near_gold_row("GHOST", {"sender": "Boo"})]),
    ]
    docs = resolve_dirs(dirs)
    ghost = docs["GHOST"]
    assert (ghost.verified, ghost.unverified_reasons, ghost.doc_class) == (False, ("not_in_corpus_index",), "letter")
    assert ghost.fields["sender"].value == "Boo" and ghost.scorable_fields == frozenset()
    assert docs["D1"].scorable_fields == frozenset({"sender"})


def test_hub_text_unverified_flags_the_document(tmp_path: Path) -> None:
    dirs = [make_snapshot(tmp_path, "hub", [hub_row("D1", fields={"sender": "Ann"}), hub_row("D2", fields={"sender": "Bo"})],
                          source={"text_unverified_documents": ["D1"]})]
    docs = resolve_dirs(dirs)
    assert docs["D1"].unverified_reasons == ("hub_text_unverified",) and docs["D1"].scorable_fields == frozenset()
    assert docs["D2"].verified


def test_class_disagreement_makes_the_label_stale(tmp_path: Path) -> None:
    dirs = [
        make_snapshot(tmp_path, "hub", [hub_row("D1", fields={"sender": ("", "unlabeled")})]),
        make_snapshot(tmp_path, "near_gold", [near_gold_row("D1", {"sender": "X"}, "filing", "annual")]),
    ]
    d1 = resolve_dirs(dirs)["D1"]
    assert not d1.verified and "sender" in d1.untrusted


def test_records_order_shuffled_end_to_end(snapshot_dirs) -> None:
    read = read_layers(load_snapshot_set(snapshot_dirs))
    index = corpus_index(read.corpus)
    base = resolve(read.records, PROFILE, index)
    for seed in range(5):
        recs = list(read.records)
        random.Random(seed).shuffle(recs)
        assert resolve(recs, PROFILE, index) == base
