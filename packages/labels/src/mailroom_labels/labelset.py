"""A label set: the snapshots a run scores against, verified, checked and resolved.

``load_label_set(directories, profile)`` is what the Dojo codes against. It loads every
snapshot (manifest verification first), runs the quality gate, refuses the set when the
gate finds an error (``QualityGateFailed``, which carries the report), and resolves every
document once. The result is immutable: its own fields are frozen, and every resolved
value it hands out (``documents()``, ``document()``) is a fresh copy of any list or dict,
so a caller that changes the value it was given never changes the labels:

- ``snapshot_id``: the member snapshot ids, sorted and joined with ``+`` (deterministic,
  readable, and different for any other membership);
- ``manifest_hash``: ``hash_obj`` of the sorted ``[snapshot_id, manifest_hash]`` pairs, so
  it changes when any member's manifest changes;
- ``documents()``: the resolved ``DocumentLabels``, sorted by document id;
- ``snapshot_refs()``: what a run records about each member (id, kind, manifest hash);
- ``label_snapshot_ids()``: the same as the strings ``mailroom_record.models.Run`` stores in
  ``label_snapshot_ids`` (``<snapshot_id>@<manifest_hash>``);
- ``report``: the quality report (warnings stay visible; known ones are marked).

``load_label_set`` is strict by default: it also refuses a set with a warning that the
snapshot raising it does not accept, the same rule as ``mailroom-labels verify --strict``,
so a scorer never runs on an unreviewed warning (a Hub import without the text check only
warns ``text_unchecked``). ``strict=False`` loads such a set for inspection only.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mailroom_labels.errors import LabelsError, ManifestMismatch
from mailroom_labels.findings import finding
from mailroom_labels.layers import corpus_index, read_layers
from mailroom_labels.models import DocumentLabels, FieldLabel, LayerFact
from mailroom_labels.profile import ClientProfile
from mailroom_labels.quality import QualityReport, check_dirs, verify
from mailroom_labels.resolve import resolve
from mailroom_labels.snapshot import LabelSnapshot, label_snapshot_ids, snapshot_refs
from mailroom_record.hashing import hash_obj

__all__ = ["LabelSet", "QualityGateFailed", "load_label_set"]


class QualityGateFailed(LabelsError):
    """The quality gate failed: at least one error, or (strict) a warning its snapshot does
    not accept. ``report`` holds every finding."""

    def __init__(self, report: QualityReport, *, strict: bool = False) -> None:
        self.report = report
        failing = (*report.errors, *(report.unknown_warnings if strict else ()))
        kinds = sorted({f.kind for f in failing})
        super().__init__(
            f"{len(report.errors)} quality error(s)"
            + (f", {len(report.unknown_warnings)} unknown warning(s)" if strict else "")
            + f": {', '.join(kinds)}"
        )


def _thawed(value: Any) -> Any:
    """A deep copy of a JSON value's lists and dicts (scalars are immutable and shared)."""
    if isinstance(value, list):
        return [_thawed(v) for v in value]
    if isinstance(value, dict):
        return {k: _thawed(v) for k, v in value.items()}
    return value


def _fact_copy(fact: LayerFact) -> LayerFact:
    if not isinstance(fact.value, (list, dict)):
        return fact
    return fact.model_copy(update={"value": _thawed(fact.value)})


def _label_copy(label: FieldLabel) -> FieldLabel:
    seen = tuple(_fact_copy(f) for f in label.layers_seen)
    if not isinstance(label.value, (list, dict)) and all(
        a is b for a, b in zip(seen, label.layers_seen, strict=True)
    ):
        return label
    return label.model_copy(update={"value": _thawed(label.value), "layers_seen": seen})


def _detached(doc: DocumentLabels) -> DocumentLabels:
    """``doc`` with every list or dict value copied, so the caller cannot reach the stored
    one. Frozen models and scalars are shared; only what could be mutated is copied."""
    fields = {name: _label_copy(label) for name, label in doc.fields.items()}
    untrusted = {name: tuple(_fact_copy(f) for f in facts) for name, facts in doc.untrusted.items()}
    if all(fields[n] is doc.fields[n] for n in fields) and all(
        a is b for n, facts in untrusted.items() for a, b in zip(facts, doc.untrusted[n], strict=True)
    ):
        return doc
    # model_copy does not re-run validators: keep the read-only views ourselves.
    return doc.model_copy(
        update={"fields": MappingProxyType(fields), "untrusted": MappingProxyType(untrusted)}
    )


@dataclass(frozen=True)
class LabelSet:
    """A verified, quality-checked and resolved set of snapshots (see the module doc)."""

    snapshots: tuple[LabelSnapshot, ...]
    profile: ClientProfile
    report: QualityReport
    _documents: tuple[DocumentLabels, ...]

    @property
    def snapshot_id(self) -> str:
        return "+".join(s.snapshot_id for s in self.snapshots)

    @property
    def manifest_hash(self) -> str:
        return hash_obj([[s.snapshot_id, s.manifest_hash] for s in self.snapshots])

    def documents(self) -> Iterator[DocumentLabels]:
        """Every resolved document, sorted by document id (list and dict values are copies)."""
        return (_detached(d) for d in self._documents)

    def document(self, document_id: str) -> DocumentLabels | None:
        """One resolved document, or None when no snapshot names it (list and dict values
        are copies)."""
        found = next((d for d in self._documents if d.document_id == document_id), None)
        return None if found is None else _detached(found)

    def snapshot_refs(self) -> tuple[dict[str, str], ...]:
        """What a run records about the labels it was scored against."""
        return snapshot_refs(self.snapshots)

    def label_snapshot_ids(self) -> list[str]:
        """The run header's ``label_snapshot_ids`` value (``<id>@<manifest_hash>``, sorted)."""
        return label_snapshot_ids(self.snapshots)


def load_label_set(
    directories: Iterable[Path | str], profile: ClientProfile, *, strict: bool = True
) -> LabelSet:
    """Load, check and resolve snapshot directories; raise ``QualityGateFailed`` on any error
    (a snapshot that fails to load, or whose files change between the gate and resolving,
    is an error too: the ``ManifestMismatch`` is the cause). With ``strict`` (the default,
    what ``mailroom-labels verify --strict`` checks) a warning that the snapshot raising it
    does not list in its ``known_warnings`` fails the gate too; ``strict=False`` is for
    inspection, never for scoring."""
    report, loaded = check_dirs(list(directories), profile)
    if verify(report, strict=strict) != 0:
        raise QualityGateFailed(report, strict=strict)
    try:
        read = read_layers(loaded)
        for snap in loaded:  # the manifest too: the set must not name one the directory lost
            snap.check_manifest()
    except ManifestMismatch as exc:  # a file or manifest changed after the gate read it
        changed = finding("manifest_mismatch", location=str(exc.path), detail=f"{exc.kind}: {exc.detail}")
        raise QualityGateFailed(QualityReport((changed, *report.findings))) from exc
    docs = resolve(read.records, profile, corpus_index(read.corpus))
    return LabelSet(snapshots=tuple(loaded), profile=profile, report=report, _documents=docs)
