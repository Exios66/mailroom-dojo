"""Layer readers: turn each snapshot kind's native rows into ``LayerRecord``s.

Native shapes (one JSON object per line), keyed by the manifest's ``kind`` and
``layer_schema``:

``hub`` / ``mailroom-labels/hub/v1`` — one row per dataset document (rank ``hub`` for a
populated field, ``hub:absent`` for a documented absence or ``not_applicable``)::

    {"document_id", "doc_class", "subclass", "split", "content_sha256", "filename",
     "fields": {<field>: {"value": <json>, "presence": <presence>}}}

  ``presence`` is ``populated``, ``schema_documented_absence``, ``not_applicable`` or
  ``unlabeled``; an ``unlabeled`` field yields no label. Every row also becomes a
  ``CorpusEntry``. The manifest's ``source.text_unverified_documents`` (a list of document
  ids whose Hub text did not hash to ``content_sha256`` at import) marks entries unverified.

``interim`` / ``mailroom-interim-labels/v1`` — the sandbox interim overlay, one row per
document: ``{"doc_id", "doc_class", "doc_subclass", "doc_sha256", "labels": {<field>:
{"status", "value", "basis", "use_as_truth", ...}}, "source": {"gate_run", ...}}``.
  ``status`` ``gap_fill``, ``derived`` and ``correction`` are truth; ``disputed`` is recorded
  under the untrusted rank ``interim:disputed``. ``use_as_truth`` must follow the status: a
  label whose flag contradicts it is an ``inconsistent_truth_flag`` finding and is recorded
  under the untrusted rank ``interim:flag_mismatch``, never applied as truth.
  Presence is ``schema_documented_absence`` for a null value, else ``populated`` (the
  sandbox's ``apply_overlay`` rule).

``near_gold`` / ``mailroom-interim-labels/v1`` — the sandbox near-gold overlay: the same
outer shape; every label has ``basis`` ``near_gold:<how>`` and a ``presence``. Its native
``status`` word is the overlay's (``gap_fill``) and is ignored: the layer is near_gold.

``review`` / ``mailroom-labels/review/v1`` — one decision per line: ``{"doc_id", "field",
"verdict", "reviewer": {"model", "role"}, "note"?, "entry": {"value", "evidence"?,
"absent"?, "basis"?}, "origin"?, "reviewed_at"?}``. ``origin`` (a list of
``<source file>:<line>`` strings) and ``reviewed_at`` (a timestamp string or null) are
provenance only and never change how a decision resolves. ``verdict`` is ``correct`` or ``accept``. A decision whose
``reviewer.model`` is ``human`` is ``gold`` (rank ``review:gold``); any other reviewer's is
``near_gold`` (rank ``review:near_gold``) with source ``review:<model>-<role>``. An
``absent`` reason makes the presence ``schema_documented_absence``.

A row that does not have its shape is a ``malformed_record`` finding and yields nothing; a
label with a status its layer does not allow is a ``status_not_allowed`` finding. Readers
never raise on bad rows. A new native version is an entry in ``UPGRADES`` (a function from
the old row to the new one, applied at read time; ADR-0005), never a rewrite of the files.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mailroom_labels.errors import CorruptLine, UnknownSnapshotSchema
from mailroom_labels.findings import finding
from mailroom_labels.models import PRESENCES, CorpusEntry, Finding, LayerRecord
from mailroom_labels.snapshot import MANIFEST_NAME, LabelSnapshot, Row

__all__ = [
    "CURRENT_LAYER_SCHEMA",
    "UPGRADES",
    "LayerRead",
    "corpus_index",
    "hash_matches",
    "read_layer",
    "read_layers",
    "supports",
]

CURRENT_LAYER_SCHEMA: dict[str, str] = {
    "hub": "mailroom-labels/hub/v1",
    "interim": "mailroom-interim-labels/v1",
    "near_gold": "mailroom-interim-labels/v1",
    "review": "mailroom-labels/review/v1",
}

#: ``(kind, old_schema) -> (new_schema, upgrade_row)``. Applied until the current schema.
UPGRADES: dict[tuple[str, str], tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}

INTERIM_TRUTH = ("gap_fill", "derived", "correction")
#: The rank of an interim label whose ``use_as_truth`` contradicts its status: recorded, never truth.
FLAG_MISMATCH_RANK = "interim:flag_mismatch"
INTERIM_STATUSES = (*INTERIM_TRUTH, "disputed")
REVIEW_VERDICTS = ("correct", "accept")
_HASH = re.compile(r"^[0-9a-f]{16,64}$")


@dataclass
class LayerRead:
    """What one snapshot contributed: labels, corpus entries (Hub only) and findings."""

    records: list[LayerRecord] = field(default_factory=list)
    corpus: list[CorpusEntry] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def extend(self, other: LayerRead) -> None:
        self.records.extend(other.records)
        self.corpus.extend(other.corpus)
        self.findings.extend(other.findings)


def hash_matches(label_hash: str | None, index_hash: str | None) -> bool:
    """True when a label's recorded hash names the indexed text: the index hash starts
    with it (the sandbox overlays store a 16-hex prefix). A label with no hash claims
    nothing and matches; a label with a hash cannot match an index without one, and a
    label hash that is not 16 to 64 lowercase hex digits (empty, short, uppercase) never
    matches."""
    if label_hash is None:
        return True
    return bool(_HASH.match(label_hash)) and index_hash is not None and index_hash.startswith(label_hash)


def _is_name(value: object) -> bool:
    return isinstance(value, str) and value != "" and value.strip() == value


def _opt_name(value: object) -> bool:
    return value is None or _is_name(value)


class _Ctx:
    """Where a row came from, for findings and records."""

    def __init__(self, snapshot: LabelSnapshot, row: Row) -> None:
        self.snapshot_id = snapshot.snapshot_id
        self.kind = snapshot.kind
        self.row = row
        self.location = f"{snapshot.snapshot_id}/{row.file}:{row.line}"

    def bad(self, detail: str, doc: object = None, fld: object = None) -> Finding:
        return finding(
            "malformed_record",
            doc=doc if isinstance(doc, str) else None,
            field=fld if isinstance(fld, str) else None,
            layer=self.kind,
            detail=detail,
            location=self.location,
        )

    def record(self, **kw: Any) -> LayerRecord:
        return LayerRecord(
            layer=self.kind, snapshot_id=self.snapshot_id, file=self.row.file, line=self.row.line, **kw
        )


def _absence(value: Any) -> str:
    return "schema_documented_absence" if value is None else "populated"


def _read_hub(snap: LabelSnapshot, row: Row, out: LayerRead, unverified: frozenset[str]) -> None:
    ctx, d = _Ctx(snap, row), row.data
    doc = d.get("document_id")
    expected = {"document_id", "doc_class", "subclass", "split", "content_sha256", "filename", "fields"}
    problems = []
    if set(d) != expected:
        problems.append(f"keys must be {sorted(expected)}, got {sorted(d)}")
    if not _is_name(doc):
        problems.append("document_id must be a name")
    if not _is_name(d.get("doc_class")):
        problems.append("doc_class must be a name")
    for key in ("subclass", "split", "filename"):
        if not _opt_name(d.get(key)):
            problems.append(f"{key} must be a name or null")
    sha = d.get("content_sha256")
    if sha is not None and not (isinstance(sha, str) and len(sha) == 64 and _HASH.match(sha)):
        problems.append("content_sha256 must be 64 lowercase hex digits or null")
    if not isinstance(d.get("fields"), dict):
        problems.append("fields must be an object")
    if problems:
        out.findings.append(ctx.bad("; ".join(problems), doc))
        return
    out.corpus.append(
        CorpusEntry(
            document_id=doc,
            doc_class=d["doc_class"],
            subclass=d["subclass"],
            split=d["split"],
            content_sha256=sha,
            text_verified=doc not in unverified,
        )
    )
    for fname, lab in sorted(d["fields"].items()):
        if not _is_name(fname) or not isinstance(lab, dict) or set(lab) != {"value", "presence"}:
            out.findings.append(ctx.bad("a field must be {value, presence}", doc, fname))
            continue
        presence = lab["presence"]
        if presence == "unlabeled":
            continue
        if presence not in PRESENCES:
            out.findings.append(ctx.bad(f"unknown presence {presence!r}", doc, fname))
            continue
        out.records.append(
            ctx.record(
                document_id=doc,
                field=fname,
                value=lab["value"],
                presence=presence,
                rank="hub" if presence == "populated" else "hub:absent",
                native_status="hub",
                source=f"hub:{snap.snapshot_id}",
                content_sha256=sha,
                doc_class=d["doc_class"],
                subclass=d["subclass"],
            )
        )


def _overlay_header(ctx: _Ctx, d: Mapping[str, Any]) -> list[str]:
    problems = []
    for key in ("doc_id", "doc_class"):
        if not _is_name(d.get(key)):
            problems.append(f"{key} must be a name")
    if not _opt_name(d.get("doc_subclass")):
        problems.append("doc_subclass must be a name or null")
    sha = d.get("doc_sha256")
    if sha is not None and not (isinstance(sha, str) and _HASH.match(sha)):
        problems.append("doc_sha256 must be 16 to 64 lowercase hex digits or null")
    if not isinstance(d.get("labels"), dict):
        problems.append("labels must be an object")
    if "source" in d and not isinstance(d["source"], dict):
        problems.append("source must be an object")
    return problems


def _read_interim(snap: LabelSnapshot, row: Row, out: LayerRead) -> None:
    ctx, d = _Ctx(snap, row), row.data
    problems = _overlay_header(ctx, d)
    if problems:
        out.findings.append(ctx.bad("; ".join(problems), d.get("doc_id")))
        return
    doc = d["doc_id"]
    gate = (d.get("source") or {}).get("gate_run")
    source = f"interim:{gate}" if isinstance(gate, str) and gate else "interim"
    for fname, lab in sorted(d["labels"].items()):
        if not _is_name(fname) or not isinstance(lab, dict) or "value" not in lab or "status" not in lab:
            out.findings.append(ctx.bad("a label must be an object with status and value", doc, fname))
            continue
        status = lab["status"]
        if status not in INTERIM_STATUSES:
            out.findings.append(
                finding(
                    "status_not_allowed", doc=doc, field=fname, layer="interim",
                    detail=f"interim status {status!r} (allowed: {', '.join(INTERIM_STATUSES)})",
                    location=ctx.location,
                )
            )
            continue
        rank = f"interim:{status}"
        if lab.get("use_as_truth") is not (status in INTERIM_TRUTH):
            out.findings.append(
                finding(
                    "inconsistent_truth_flag", doc=doc, field=fname, layer="interim",
                    detail=f"status {status!r} with use_as_truth={lab.get('use_as_truth')!r}",
                    location=ctx.location,
                )
            )
            rank = FLAG_MISMATCH_RANK  # never truth: the label contradicts itself
        basis = lab.get("basis")
        out.records.append(
            ctx.record(
                document_id=doc,
                field=fname,
                value=lab["value"],
                presence=_absence(lab["value"]),
                rank=rank,
                native_status=status,
                source=source,
                basis=basis if isinstance(basis, str) else None,
                content_sha256=d.get("doc_sha256"),
                doc_class=d["doc_class"],
                subclass=d.get("doc_subclass"),
            )
        )


def _read_near_gold(snap: LabelSnapshot, row: Row, out: LayerRead) -> None:
    ctx, d = _Ctx(snap, row), row.data
    problems = _overlay_header(ctx, d)
    if problems:
        out.findings.append(ctx.bad("; ".join(problems), d.get("doc_id")))
        return
    doc = d["doc_id"]
    for fname, lab in sorted(d["labels"].items()):
        if not _is_name(fname) or not isinstance(lab, dict) or "value" not in lab:
            out.findings.append(ctx.bad("a label must be an object with a value", doc, fname))
            continue
        basis = lab.get("basis")
        if not (isinstance(basis, str) and basis.startswith("near_gold:") and len(basis) > 10):
            out.findings.append(
                finding(
                    "status_not_allowed", doc=doc, field=fname, layer="near_gold",
                    detail=f"basis {basis!r} is not near_gold:<how>", location=ctx.location,
                )
            )
            continue
        presence = lab.get("presence")
        if presence not in PRESENCES:
            out.findings.append(ctx.bad(f"unknown presence {presence!r}", doc, fname))
            continue
        if lab.get("use_as_truth"):
            out.findings.append(
                finding(
                    "inconsistent_truth_flag", doc=doc, field=fname, layer="near_gold",
                    detail="a near_gold label claims use_as_truth", location=ctx.location,
                )
            )
        out.records.append(
            ctx.record(
                document_id=doc,
                field=fname,
                value=lab["value"],
                presence=presence,
                rank="near_gold",
                native_status=str(lab.get("status")),
                source=basis,
                basis=basis,
                content_sha256=d.get("doc_sha256"),
                doc_class=d["doc_class"],
                subclass=d.get("doc_subclass"),
            )
        )


def _read_review(snap: LabelSnapshot, row: Row, out: LayerRead) -> None:
    ctx, d = _Ctx(snap, row), row.data
    doc, fname = d.get("doc_id"), d.get("field")
    problems = []
    extra = set(d) - {"doc_id", "field", "verdict", "reviewer", "note", "entry", "origin", "reviewed_at"}
    if extra:
        problems.append(f"unknown keys {sorted(extra)}")
    origin = d.get("origin", [])
    if not (isinstance(origin, list) and all(_is_name(o) for o in origin)):
        problems.append("origin must be a list of names")
    if not isinstance(d.get("reviewed_at"), (str, type(None))):
        problems.append("reviewed_at must be a string or null")
    if not _is_name(doc):
        problems.append("doc_id must be a name")
    if not _is_name(fname):
        problems.append("field must be a name")
    reviewer = d.get("reviewer")
    if not (isinstance(reviewer, dict) and set(reviewer) == {"model", "role"}
            and _is_name(reviewer["model"]) and _is_name(reviewer["role"])):
        problems.append("reviewer must be {model, role} with both named")
    entry = d.get("entry")
    if not (isinstance(entry, dict) and "value" in entry
            and set(entry) <= {"value", "evidence", "absent", "basis"}):
        problems.append("entry must be {value, evidence?, absent?, basis?}")
    if "note" in d and not isinstance(d["note"], (str, type(None))):
        problems.append("note must be a string or null")
    if problems:
        out.findings.append(ctx.bad("; ".join(problems), doc, fname))
        return
    if d.get("verdict") not in REVIEW_VERDICTS:
        out.findings.append(
            finding(
                "status_not_allowed", doc=doc, field=fname, layer="review",
                detail=f"verdict {d.get('verdict')!r} (allowed: {', '.join(REVIEW_VERDICTS)})",
                location=ctx.location,
            )
        )
        return
    human = reviewer["model"] == "human"
    value = entry["value"]
    absent = entry.get("absent")
    basis = entry.get("basis")
    out.records.append(
        ctx.record(
            document_id=doc,
            field=fname,
            value=value,
            presence="schema_documented_absence" if absent else _absence(value),
            rank="review:gold" if human else "review:near_gold",
            native_status=d["verdict"],
            source=f"review:{reviewer['model']}-{reviewer['role']}",
            reviewer=f"{reviewer['model']}:{reviewer['role']}",
            basis=basis if isinstance(basis, str) else None,
        )
    )


def _upgrade_steps(kind: str, schema: str) -> list[Callable[[dict[str, Any]], dict[str, Any]]] | None:
    """The upgrade functions from ``schema`` to the current one, or None if there is no path."""
    steps = []
    seen: set[str] = set()
    while schema != CURRENT_LAYER_SCHEMA[kind]:
        if schema in seen or (kind, schema) not in UPGRADES:
            return None
        seen.add(schema)
        schema, step = UPGRADES[(kind, schema)]
        steps.append(step)
    return steps


def supports(kind: str, layer_schema: str) -> bool:
    """True when this code can read ``layer_schema`` for ``kind`` (current, or upgradable)."""
    return kind in CURRENT_LAYER_SCHEMA and _upgrade_steps(kind, layer_schema) is not None


def read_layer(snapshot: LabelSnapshot) -> LayerRead:
    """Read one verified snapshot into normalised records and findings; never raises on a
    bad row (a corrupt line becomes a ``corrupt_line`` finding). Raises
    ``UnknownSnapshotSchema`` when the kind's ``layer_schema`` has no upgrade path."""
    kind, schema = snapshot.kind, snapshot.manifest.layer_schema
    steps = _upgrade_steps(kind, schema)
    if steps is None:
        raise UnknownSnapshotSchema(schema, snapshot.directory / MANIFEST_NAME)
    out = LayerRead()

    def on_corrupt(exc: CorruptLine) -> None:
        out.findings.append(
            finding(
                "corrupt_line", layer=kind, detail=exc.detail,
                location=f"{snapshot.snapshot_id}/{exc.path.relative_to(snapshot.directory).as_posix()}:{exc.line}",
            )
        )

    unverified = frozenset()
    if kind == "hub":
        listed = snapshot.manifest.source.get("text_unverified_documents", [])
        unverified = frozenset(x for x in listed if isinstance(x, str)) if isinstance(listed, list) else frozenset()
    for row in snapshot.rows(on_corrupt=on_corrupt):
        if steps:
            data = row.data
            try:
                for step in steps:
                    data = step(data)
            except (KeyError, TypeError, ValueError) as exc:
                out.findings.append(_Ctx(snapshot, row).bad(f"cannot upgrade {schema}: {exc!r}"))
                continue
            row = Row(row.file, row.line, data)
        if kind == "hub":
            _read_hub(snapshot, row, out, unverified)
        elif kind == "interim":
            _read_interim(snapshot, row, out)
        elif kind == "near_gold":
            _read_near_gold(snapshot, row, out)
        else:
            _read_review(snapshot, row, out)
    return out


def read_layers(snapshots: Iterable[LabelSnapshot]) -> LayerRead:
    """``read_layer`` over several snapshots, concatenated in the order given."""
    out = LayerRead()
    for snap in snapshots:
        out.extend(read_layer(snap))
    return out


def corpus_index(entries: Iterable[CorpusEntry]) -> dict[str, CorpusEntry]:
    """Corpus entries by document id. A repeated id keeps its first entry (the quality
    gate reports the repeat as ``duplicate_document``)."""
    index: dict[str, CorpusEntry] = {}
    for entry in entries:
        index.setdefault(entry.document_id, entry)
    return index
