"""A label set: the snapshots a run scores against, verified, checked and resolved.

``load_label_set(directories, profile)`` is what the Dojo codes against. It loads every
snapshot (manifest verification first), runs the quality gate, refuses the set when the
gate finds an error (``QualityGateFailed``, which carries the report), and resolves every
document once. The result is immutable:

- ``snapshot_id``: the member snapshot ids, sorted and joined with ``+`` (deterministic,
  readable, and different for any other membership);
- ``manifest_hash``: ``hash_obj`` of the sorted ``[snapshot_id, manifest_hash]`` pairs, so
  it changes when any member's manifest changes;
- ``documents()``: the resolved ``DocumentLabels``, sorted by document id;
- ``snapshot_refs()``: what a run records about each member (id, kind, manifest hash);
- ``label_snapshot_ids()``: the same as the strings ``mailroom_record.models.Run`` stores in
  ``label_snapshot_ids`` (``<snapshot_id>@<manifest_hash>``);
- ``report``: the quality report (warnings stay visible; known ones are marked).

``load_label_set(..., strict=True)`` also refuses a set with a warning that the snapshot
raising it does not accept, the same rule as ``mailroom-labels verify --strict``; a scorer
that must not run on unreviewed warnings asks for it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from mailroom_labels.errors import LabelsError
from mailroom_labels.layers import corpus_index, read_layers
from mailroom_labels.models import DocumentLabels
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
        super().__init__(f"{len(report.errors)} quality error(s)"
                         + (f", {len(report.unknown_warnings)} unknown warning(s)" if strict else "")
                         + f": {', '.join(kinds)}")


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
        """Every resolved document, sorted by document id."""
        return iter(self._documents)

    def document(self, document_id: str) -> DocumentLabels | None:
        """One resolved document, or None when no snapshot names it."""
        return next((d for d in self._documents if d.document_id == document_id), None)

    def snapshot_refs(self) -> tuple[dict[str, str], ...]:
        """What a run records about the labels it was scored against."""
        return snapshot_refs(self.snapshots)

    def label_snapshot_ids(self) -> list[str]:
        """The run header's ``label_snapshot_ids`` value (``<id>@<manifest_hash>``, sorted)."""
        return label_snapshot_ids(self.snapshots)


def load_label_set(directories: Iterable[Path | str], profile: ClientProfile, *, strict: bool = False) -> LabelSet:
    """Load, check and resolve snapshot directories; raise ``QualityGateFailed`` on any error
    (a snapshot that fails to load is an error too). With ``strict`` (what ``mailroom-labels
    verify --strict`` checks) a warning that the snapshot raising it does not list in its
    ``known_warnings`` fails the gate too."""
    report, loaded = check_dirs(list(directories), profile)
    if verify(report, strict=strict) != 0:
        raise QualityGateFailed(report, strict=strict)
    read = read_layers(loaded)
    docs = resolve(read.records, profile, corpus_index(read.corpus))
    return LabelSet(snapshots=tuple(loaded), profile=profile, report=report, _documents=docs)
