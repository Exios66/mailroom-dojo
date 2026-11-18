"""The data-quality gate: every check of plan decision 5, reported as typed findings.

``check`` and ``check_dirs`` never raise on bad data. Each problem becomes a ``Finding``
(``findings.SEVERITY`` gives its severity) and the valid records around it are still read
and checked, so one run lists every problem at once. ``verify`` turns a report into an
exit code: non-zero when any error is found. A warning named in a manifest's
``known_warnings`` is marked ``known``; it never changes the exit code either way.

Checks against the corpus index (the Hub snapshot): unknown document, class and subclass
disagreement, stale content hash, Hub text not verified, a document indexed twice. Within
and across layers: duplicate (document, field) in one layer (review decisions per rank),
a status the layer does not allow, a truth flag that contradicts the status, a review
decision aimed at a label no layer has, a decision that contradicts a human decision.
Against the client profile: unknown class, subclass or field, a value that does not have
its field type, a closed-label value outside its vocabulary.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mailroom_labels.errors import (
    DuplicateSnapshot,
    ManifestMismatch,
    ManifestMissing,
    UnknownSnapshotSchema,
)
from mailroom_labels.findings import finding
from mailroom_labels.layers import LayerRead, corpus_index, hash_matches, read_layer, supports
from mailroom_labels.models import Finding, LayerRecord
from mailroom_labels.profile import ClientProfile
from mailroom_labels.snapshot import LabelSnapshot, load_snapshot
from mailroom_record.hashing import canonical_json

__all__ = ["QualityReport", "check", "check_dirs", "check_read", "value_matches_type", "verify"]

_STRING_TYPES = ("name", "label", "date", "id", "free_text", "containment", "cms_doc_ref")


@dataclass(frozen=True)
class QualityReport:
    """Findings sorted errors first, then by kind, document, field, layer and location."""

    findings: tuple[Finding, ...]

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def kinds(self) -> Counter:
        return Counter(f.kind for f in self.findings)


def _sort_key(f: Finding) -> tuple:
    return (f.severity != "error", f.kind, f.doc or "", f.field or "", f.layer or "", f.location or "", f.detail)


def value_matches_type(field_type: str, value: Any) -> bool:
    """True when ``value`` has the JSON shape a field of ``field_type`` holds (null is an
    absence and always fits). Formats (date syntax, money parsing) are the Dojo's job."""
    if value is None:
        return True
    if field_type in _STRING_TYPES:
        return isinstance(value, str)
    if field_type == "money":
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return math.isfinite(value)
        return isinstance(value, str)
    if field_type == "entity_list" or field_type.startswith("entity_list:"):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return False


class _Once:
    """Collects findings, keeping one per key (an unknown document is reported once per
    layer, not once per field)."""

    def __init__(self) -> None:
        self.items: list[Finding] = []
        self._seen: set[tuple] = set()

    def add(self, item: Finding, key: tuple | None = None) -> None:
        if key is not None:
            if key in self._seen:
                return
            self._seen.add(key)
        self.items.append(item)


def _where(r: LayerRecord) -> str:
    return f"{r.snapshot_id}/{r.file}:{r.line}"


def _check_record(r: LayerRecord, index: dict, profile: ClientProfile, have_hub: bool, out: _Once) -> None:
    entry = index.get(r.document_id)
    if have_hub and entry is None:
        out.add(
            finding("unknown_document", doc=r.document_id, layer=r.layer, location=_where(r),
                    detail="not in the Hub corpus index"),
            ("unknown_document", r.document_id, r.snapshot_id),
        )
    if entry is not None:
        if r.doc_class is not None and r.doc_class != entry.doc_class:
            out.add(
                finding("class_mismatch", doc=r.document_id, layer=r.layer, location=_where(r),
                        detail=f"layer says {r.doc_class!r}, index says {entry.doc_class!r}"),
                ("class_mismatch", r.document_id, r.snapshot_id),
            )
        if r.subclass is not None and r.subclass != entry.subclass:
            out.add(
                finding("subclass_mismatch", doc=r.document_id, layer=r.layer, location=_where(r),
                        detail=f"layer says {r.subclass!r}, index says {entry.subclass!r}"),
                ("subclass_mismatch", r.document_id, r.snapshot_id),
            )
        if not hash_matches(r.content_sha256, entry.content_sha256):
            out.add(
                finding("stale_content_hash", doc=r.document_id, layer=r.layer, location=_where(r),
                        detail=f"label hash {r.content_sha256} does not name index hash {entry.content_sha256}"),
                ("stale_content_hash", r.document_id, r.snapshot_id),
            )
    doc_class = entry.doc_class if entry is not None else r.doc_class
    subclass = entry.subclass if entry is not None else r.subclass
    spec = profile.classes.get(doc_class) if doc_class is not None else None
    if spec is None:
        if doc_class is not None:
            out.add(
                finding("unknown_class", doc=r.document_id, layer=r.layer, location=_where(r),
                        detail=f"class {doc_class!r} is not in profile {profile.client!r}"),
                ("unknown_class", r.document_id),
            )
        return
    if subclass is not None and subclass not in spec.subclasses:
        out.add(
            finding("unknown_subclass", doc=r.document_id, layer=r.layer, location=_where(r),
                    detail=f"subclass {subclass!r} is not a subclass of {doc_class!r}"),
            ("unknown_subclass", r.document_id),
        )
    fspec = spec.fields.get(r.field)
    if fspec is None:
        out.add(
            finding("unknown_field", doc=r.document_id, field=r.field, layer=r.layer, location=_where(r),
                    detail=f"{doc_class!r} has no field {r.field!r}"),
        )
        return
    if not value_matches_type(fspec.type, r.value):
        out.add(
            finding("type_mismatch", doc=r.document_id, field=r.field, layer=r.layer, location=_where(r),
                    detail=f"{type(r.value).__name__} value for a {fspec.type} field"),
        )
        return
    if fspec.vocabulary is not None and r.value is not None:
        allowed = profile.vocabularies[fspec.vocabulary]
        if r.value not in allowed:
            out.add(
                finding("vocabulary_violation", doc=r.document_id, field=r.field, layer=r.layer,
                        location=_where(r), detail=f"{r.value!r} is not in vocabulary {fspec.vocabulary!r}"),
            )


def _check_duplicates(records: Sequence[LayerRecord], out: _Once) -> None:
    groups: dict[tuple, list[LayerRecord]] = defaultdict(list)
    for r in records:
        scope = r.rank if r.layer == "review" else r.layer
        groups[(scope, r.document_id, r.field)].append(r)
    for (scope, doc, fld), group in sorted(groups.items()):
        if len(group) > 1:
            first, *rest = sorted(group, key=lambda r: (r.snapshot_id, r.file, r.line))
            for r in rest:
                out.add(
                    finding("duplicate_label", doc=doc, field=fld, layer=scope, location=_where(r),
                            detail=f"also labelled at {_where(first)}"),
                )


def _check_decisions(records: Sequence[LayerRecord], out: _Once) -> None:
    labelled = {(r.document_id, r.field) for r in records if r.layer != "review"}
    human: dict[tuple, list[LayerRecord]] = defaultdict(list)
    others: dict[tuple, list[LayerRecord]] = defaultdict(list)
    for r in records:
        if r.layer != "review":
            continue
        key = (r.document_id, r.field)
        if key not in labelled:
            out.add(
                finding("orphan_decision", doc=r.document_id, field=r.field, layer="review",
                        location=_where(r), detail="no layer labels this field"),
            )
        (human if r.rank == "review:gold" else others)[key].append(r)
    for key, decisions in sorted(human.items()):
        values = {canonical_json(d.value) for d in decisions}
        for r in sorted(decisions[1:] + others.get(key, []), key=_where):
            if canonical_json(r.value) not in values or len(values) > 1:
                out.add(
                    finding("conflicting_decision", doc=key[0], field=key[1], layer="review",
                            location=_where(r), detail=f"{r.reviewer} disagrees with a human decision"),
                )


def check_read(
    read: LayerRead,
    profile: ClientProfile,
    snapshots: Sequence[LabelSnapshot] = (),
    extra: Iterable[Finding] = (),
) -> QualityReport:
    """Run every record-level check over what the layer readers produced."""
    out = _Once()
    for f in (*extra, *read.findings):
        out.add(f)
    have_hub = any(s.kind == "hub" for s in snapshots) or bool(read.corpus)
    if not have_hub:
        out.add(finding("missing_hub", detail="no hub snapshot: nothing to check documents against"))
    counts = Counter(e.document_id for e in read.corpus)
    for doc, n in sorted(counts.items()):
        if n > 1:
            out.add(finding("duplicate_document", doc=doc, layer="hub", detail=f"indexed {n} times"))
    index = corpus_index(read.corpus)
    for entry in sorted(index.values(), key=lambda e: e.document_id):
        if not entry.text_verified:
            out.add(finding("text_unverified", doc=entry.document_id, layer="hub",
                            detail="Hub text did not hash to content_sha256 at import"))
        spec = profile.classes.get(entry.doc_class)
        if spec is None:
            out.add(
                finding("unknown_class", doc=entry.document_id, layer="hub",
                        detail=f"class {entry.doc_class!r} is not in profile {profile.client!r}"),
                ("unknown_class", entry.document_id),
            )
        elif entry.subclass is not None and entry.subclass not in spec.subclasses:
            out.add(
                finding("unknown_subclass", doc=entry.document_id, layer="hub",
                        detail=f"subclass {entry.subclass!r} is not a subclass of {entry.doc_class!r}"),
                ("unknown_subclass", entry.document_id),
            )
    for r in read.records:
        _check_record(r, index, profile, have_hub, out)
    _check_duplicates(read.records, out)
    _check_decisions(read.records, out)

    known = {
        (w.get("kind"), w.get("doc"), w.get("field"))
        for s in snapshots
        for w in s.manifest.known_warnings
    }
    items = [
        f.model_copy(update={"known": True})
        if f.severity == "warning" and (f.kind, f.doc, f.field) in known
        else f
        for f in out.items
    ]
    return QualityReport(tuple(sorted(items, key=_sort_key)))


def check(snapshots: Sequence[LabelSnapshot], profile: ClientProfile) -> QualityReport:
    """Read and check already-loaded snapshots."""
    read = LayerRead()
    extra = []
    for snap in snapshots:
        try:
            read.extend(read_layer(snap))
        except UnknownSnapshotSchema as exc:
            extra.append(finding("unknown_schema", layer=snap.kind, location=str(exc.path), detail=str(exc)))
    return check_read(read, profile, snapshots, extra)


def check_dirs(directories: Iterable[Path | str], profile: ClientProfile) -> tuple[QualityReport, tuple[LabelSnapshot, ...]]:
    """Load and check snapshot directories. A snapshot that fails to load (or whose layer
    schema this code cannot read) is a finding and is left out; the others are still
    checked. Returns the report and the snapshots that loaded, safe to ``read_layers``."""
    loaded: list[LabelSnapshot] = []
    extra: list[Finding] = []
    seen: set[str] = set()
    for directory in directories:
        try:
            snap = load_snapshot(directory)
        except ManifestMissing as exc:
            extra.append(finding("manifest_missing", location=str(exc.path), detail=str(exc)))
            continue
        except ManifestMismatch as exc:
            extra.append(finding("manifest_mismatch", location=str(exc.path), detail=f"{exc.kind}: {exc.detail}"))
            continue
        except UnknownSnapshotSchema as exc:
            extra.append(finding("unknown_schema", location=str(exc.path), detail=str(exc)))
            continue
        if not supports(snap.kind, snap.manifest.layer_schema):
            path = snap.directory / "manifest.json"
            extra.append(finding("unknown_schema", location=str(path),
                                 detail=str(UnknownSnapshotSchema(snap.manifest.layer_schema, path))))
            continue
        if snap.snapshot_id in seen:
            extra.append(finding("duplicate_snapshot", location=str(directory),
                                 detail=str(DuplicateSnapshot(snap.snapshot_id))))
            continue
        seen.add(snap.snapshot_id)
        loaded.append(snap)
    loaded.sort(key=lambda s: s.snapshot_id)
    report = check(loaded, profile)
    if extra:
        report = QualityReport(tuple(sorted((*report.findings, *extra), key=_sort_key)))
    return report, tuple(loaded)


def verify(report: QualityReport) -> int:
    """Exit code for a report: 0 when it has no error, 1 otherwise."""
    return 0 if report.ok else 1
