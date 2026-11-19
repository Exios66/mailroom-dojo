"""The data-quality gate: every check of plan decision 5, reported as typed findings.

``check`` and ``check_dirs`` never raise on bad data. Each problem becomes a ``Finding``
(``findings.SEVERITY`` gives its severity) and the valid records around it are still read
and checked, so one run lists every problem at once. ``verify`` turns a report into an
exit code: non-zero when any error is found. A warning accepted by the ``known_warnings``
of the snapshot that raised it (``known_entry``) is marked ``known``; it changes the exit
code only under ``strict``, which fails on any warning that is not known.

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
from collections.abc import Iterable, Mapping, Sequence
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
from mailroom_labels.models import DROPPED_SOURCE_KEYS, Finding, LayerRecord
from mailroom_labels.profile import ClientProfile
from mailroom_labels.snapshot import LabelSnapshot, load_snapshot
from mailroom_record.hashing import canonical_json

__all__ = [
    "QualityReport",
    "check",
    "check_dirs",
    "check_read",
    "known_entry",
    "value_matches_type",
    "verify",
]

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

    @property
    def unknown_warnings(self) -> tuple[Finding, ...]:
        """Warnings no snapshot accepted (``verify --strict`` fails on them)."""
        return tuple(f for f in self.warnings if not f.known)

    def kinds(self) -> Counter:
        return Counter(f.kind for f in self.findings)


def _owner(f: Finding, snapshots: Sequence[LabelSnapshot]) -> tuple[set[str], str | None]:
    """The snapshot(s) a finding comes from and, for a record finding, the file. A record
    finding's location is ``<snapshot_id>/<file>:<line>`` (snapshot ids hold no ``/``); a
    finding about the corpus index has no location and belongs to the snapshots of its layer."""
    ids = {s.snapshot_id for s in snapshots}
    if f.location:
        head, sep, rest = f.location.partition("/")
        if sep and head in ids:
            return {head}, rest.rpartition(":")[0] or rest
        if f.location in ids:
            return {f.location}, None
    return {s.snapshot_id for s in snapshots if s.kind == f.layer}, None


def known_entry(f: Finding, snapshots: Sequence[LabelSnapshot]) -> Any:
    """The ``known_warnings`` entry that accepts warning ``f``, or ``None``. An entry
    accepts only warnings raised by its own snapshot, matching kind, document and field,
    and the file when the entry names one."""
    if f.severity != "warning":
        return None
    owners, file = _owner(f, snapshots)
    for snap in snapshots:
        if snap.snapshot_id not in owners:
            continue
        for w in snap.manifest.known_warnings:
            if (w.get("kind"), w.get("doc"), w.get("field")) == (f.kind, f.doc, f.field) and (
                w.get("file") is None or w.get("file") == file
            ):
                return w
    return None


def _sort_key(f: Finding) -> tuple:
    return (
        f.severity != "error",
        f.kind,
        f.doc or "",
        f.field or "",
        f.layer or "",
        f.location or "",
        f.detail,
    )


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
        if isinstance(value, int):
            return True  # shape only; never convert a huge integer to float
        if isinstance(value, float):
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
            finding(
                "unknown_document",
                doc=r.document_id,
                layer=r.layer,
                location=_where(r),
                detail="not in the Hub corpus index",
            ),
            ("unknown_document", r.document_id, r.snapshot_id, r.file),
        )
    if entry is not None:
        if r.doc_class is not None and r.doc_class != entry.doc_class:
            out.add(
                finding(
                    "class_mismatch",
                    doc=r.document_id,
                    layer=r.layer,
                    location=_where(r),
                    detail=f"layer says {r.doc_class!r}, index says {entry.doc_class!r}",
                ),
                ("class_mismatch", r.document_id, r.snapshot_id, r.file),
            )
        if r.subclass is not None and r.subclass != entry.subclass:
            out.add(
                finding(
                    "subclass_mismatch",
                    doc=r.document_id,
                    layer=r.layer,
                    location=_where(r),
                    detail=f"layer says {r.subclass!r}, index says {entry.subclass!r}",
                ),
                ("subclass_mismatch", r.document_id, r.snapshot_id, r.file),
            )
        if not hash_matches(r.content_sha256, entry.content_sha256):
            out.add(
                finding(
                    "stale_content_hash",
                    doc=r.document_id,
                    layer=r.layer,
                    location=_where(r),
                    detail=f"label hash {r.content_sha256} does not name index hash {entry.content_sha256}",
                ),
                ("stale_content_hash", r.document_id, r.snapshot_id, r.file),
            )
    doc_class = entry.doc_class if entry is not None else r.doc_class
    subclass = entry.subclass if entry is not None else r.subclass
    spec = profile.classes.get(doc_class) if doc_class is not None else None
    if spec is None:
        if doc_class is not None:
            out.add(
                finding(
                    "unknown_class",
                    doc=r.document_id,
                    layer=r.layer,
                    location=_where(r),
                    detail=f"class {doc_class!r} is not in profile {profile.client!r}",
                ),
                ("unknown_class", r.document_id),
            )
        return
    if subclass is not None and subclass not in spec.subclasses:
        out.add(
            finding(
                "unknown_subclass",
                doc=r.document_id,
                layer=r.layer,
                location=_where(r),
                detail=f"subclass {subclass!r} is not a subclass of {doc_class!r}",
            ),
            ("unknown_subclass", r.document_id),
        )
    fspec = spec.fields.get(r.field)
    if fspec is None:
        out.add(
            finding(
                "unknown_field",
                doc=r.document_id,
                field=r.field,
                layer=r.layer,
                location=_where(r),
                detail=f"{doc_class!r} has no field {r.field!r}",
            ),
        )
        return
    if not value_matches_type(fspec.type, r.value):
        out.add(
            finding(
                "type_mismatch",
                doc=r.document_id,
                field=r.field,
                layer=r.layer,
                location=_where(r),
                detail=f"{type(r.value).__name__} value for a {fspec.type} field",
            ),
        )
        return
    if fspec.vocabulary is not None and r.value is not None:
        allowed = profile.vocabularies[fspec.vocabulary]
        if r.value not in allowed:
            out.add(
                finding(
                    "vocabulary_violation",
                    doc=r.document_id,
                    field=r.field,
                    layer=r.layer,
                    location=_where(r),
                    detail=f"{r.value!r} is not in vocabulary {fspec.vocabulary!r}",
                ),
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
                    finding(
                        "duplicate_label",
                        doc=doc,
                        field=fld,
                        layer=scope,
                        location=_where(r),
                        detail=f"also labelled at {_where(first)}",
                    ),
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
                finding(
                    "orphan_decision",
                    doc=r.document_id,
                    field=r.field,
                    layer="review",
                    location=_where(r),
                    detail="no layer labels this field",
                ),
            )
        (human if r.rank == "review:gold" else others)[key].append(r)
    for key, decisions in sorted(human.items()):
        values = {canonical_json(d.value) for d in decisions}
        for r in sorted(decisions[1:] + others.get(key, []), key=_where):
            if canonical_json(r.value) not in values or len(values) > 1:
                out.add(
                    finding(
                        "conflicting_decision",
                        doc=key[0],
                        field=key[1],
                        layer="review",
                        location=_where(r),
                        detail=f"{r.reviewer} disagrees with a human decision",
                    ),
                )


def _text_unverified_owners(snapshots: Sequence[LabelSnapshot]) -> dict[str, str]:
    """Document id -> the first Hub snapshot (in the order given, as ``corpus_index`` keeps
    the first entry) whose manifest lists it under ``text_unverified_documents``."""
    owners: dict[str, str] = {}
    for snap in snapshots:
        listed = snap.manifest.source.get("text_unverified_documents") if snap.kind == "hub" else None
        for doc in listed if isinstance(listed, list) else ():
            if isinstance(doc, str):
                owners.setdefault(doc, snap.snapshot_id)
    return owners


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
    unverified_in = _text_unverified_owners(snapshots)
    for entry in sorted(index.values(), key=lambda e: e.document_id):
        if not entry.text_verified:
            out.add(
                finding(
                    "text_unverified",
                    doc=entry.document_id,
                    layer="hub",
                    detail="Hub text did not hash to content_sha256 at import",
                    # the Hub snapshot that lists it, so only that snapshot can accept it
                    location=unverified_in.get(entry.document_id),
                )
            )
        spec = profile.classes.get(entry.doc_class)
        if spec is None:
            out.add(
                finding(
                    "unknown_class",
                    doc=entry.document_id,
                    layer="hub",
                    detail=f"class {entry.doc_class!r} is not in profile {profile.client!r}",
                ),
                ("unknown_class", entry.document_id),
            )
        elif entry.subclass is not None and entry.subclass not in spec.subclasses:
            out.add(
                finding(
                    "unknown_subclass",
                    doc=entry.document_id,
                    layer="hub",
                    detail=f"subclass {entry.subclass!r} is not a subclass of {entry.doc_class!r}",
                ),
                ("unknown_subclass", entry.document_id),
            )
    for r in read.records:
        _check_record(r, index, profile, have_hub, out)
    _check_duplicates(read.records, out)
    _check_decisions(read.records, out)

    items = [
        f.model_copy(update={"known": True}) if known_entry(f, snapshots) is not None else f
        for f in out.items
    ]
    return QualityReport(tuple(sorted(items, key=_sort_key)))


def check(snapshots: Sequence[LabelSnapshot], profile: ClientProfile) -> QualityReport:
    """Read and check already-loaded snapshots. A snapshot whose files changed after it was
    loaded is a ``manifest_mismatch`` finding (its rows are left out), never an exception."""
    read = LayerRead()
    extra = []
    for snap in snapshots:
        try:
            part = read_layer(snap)
            snap.check_manifest()  # the manifest is read after the data: it may have been replaced
        except UnknownSnapshotSchema as exc:
            extra.append(finding("unknown_schema", layer=snap.kind, location=str(exc.path), detail=str(exc)))
            continue
        except ManifestMismatch as exc:
            extra.append(
                finding(
                    "manifest_mismatch",
                    layer=snap.kind,
                    location=str(exc.path),
                    detail=f"{exc.kind}: {exc.detail}",
                )
            )
            continue
        read.extend(part)
    for snap in snapshots:
        text_check = snap.manifest.source.get("text_check")
        if snap.kind == "hub" and not (isinstance(text_check, Mapping) and text_check.get("run") is True):
            extra.append(
                finding(
                    "text_unchecked",
                    layer="hub",
                    location=snap.snapshot_id,
                    detail="the import did not check document text against content_sha256 "
                    "(source.text_check.run is not true): verified means only 'not stale'",
                )
            )
        for key in DROPPED_SOURCE_KEYS:
            entries = snap.manifest.source.get(key)
            if entries:
                n = len(entries) if isinstance(entries, (list, dict)) else 1
                extra.append(
                    finding(
                        "dropped_source_data",
                        layer=snap.kind,
                        location=snap.snapshot_id,
                        detail=f"the importer left {n} source item(s) out (source.{key})",
                    )
                )
    return check_read(read, profile, snapshots, extra)


def check_dirs(
    directories: Iterable[Path | str], profile: ClientProfile
) -> tuple[QualityReport, tuple[LabelSnapshot, ...]]:
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
            extra.append(
                finding("manifest_mismatch", location=str(exc.path), detail=f"{exc.kind}: {exc.detail}")
            )
            continue
        except UnknownSnapshotSchema as exc:
            extra.append(finding("unknown_schema", location=str(exc.path), detail=str(exc)))
            continue
        if not supports(snap.kind, snap.manifest.layer_schema):
            path = snap.directory / "manifest.json"
            extra.append(
                finding(
                    "unknown_schema",
                    location=str(path),
                    detail=str(UnknownSnapshotSchema(snap.manifest.layer_schema, path)),
                )
            )
            continue
        if snap.snapshot_id in seen:
            extra.append(
                finding(
                    "duplicate_snapshot",
                    location=str(directory),
                    detail=str(DuplicateSnapshot(snap.snapshot_id)),
                )
            )
            continue
        seen.add(snap.snapshot_id)
        loaded.append(snap)
    loaded.sort(key=lambda s: s.snapshot_id)
    report = check(loaded, profile)
    if extra:
        report = QualityReport(tuple(sorted((*report.findings, *extra), key=_sort_key)))
    return report, tuple(loaded)


def verify(report: QualityReport, *, strict: bool = False) -> int:
    """Exit code for a report: 0 when it has no error, 1 otherwise. With ``strict``, a
    warning that its snapshot's ``known_warnings`` does not accept is also a failure."""
    return 0 if report.ok and not (strict and report.unknown_warnings) else 1
