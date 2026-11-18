"""The labels data model: frozen pydantic v2 models, unknown fields rejected.

Vocabulary used everywhere in this package:

- A **layer** is one kind of label source: ``hub`` (the dataset's own labels and corpus
  index), ``interim`` (gate-backed fills and corrections), ``near_gold`` (Claude two-pass or
  Opus-reviewed labels) and ``review`` (review-desk and audit decisions).
- A **rank** names what a label is within its layer, because precedence is finer than the
  layer: an interim ``correction`` outranks the Hub while an interim ``gap_fill`` does not.
  ``RANKS`` lists them; the client profile orders them (highest first).
- A **status word** is what a resolved label tells a scorer about its trust: ``hub``,
  ``interim``, ``near_gold`` or ``gold``.

No class or field name appears here (R4): classes and fields are open strings that the
client profile defines.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer, field_validator

__all__ = [
    "DROPPED_SOURCE_KEYS",
    "LAYERS",
    "PRESENCES",
    "RANKS",
    "RANK_STATUS",
    "SEVERITIES",
    "SNAPSHOT_ID_PATTERN",
    "STATUS_WORDS",
    "UNTRUSTED_RANKS",
    "CorpusEntry",
    "DocumentLabels",
    "FieldLabel",
    "Finding",
    "LayerFact",
    "LayerRecord",
    "ManifestFile",
    "SnapshotManifest",
]

Layer = Literal["hub", "interim", "near_gold", "review"]
LAYERS: tuple[str, ...] = Layer.__args__  # type: ignore[attr-defined]

StatusWord = Literal["hub", "interim", "near_gold", "gold"]
STATUS_WORDS: tuple[str, ...] = StatusWord.__args__  # type: ignore[attr-defined]

Presence = Literal["populated", "schema_documented_absence", "not_applicable"]
PRESENCES: tuple[str, ...] = Presence.__args__  # type: ignore[attr-defined]

Severity = Literal["error", "warning"]
SEVERITIES: tuple[str, ...] = Severity.__args__  # type: ignore[attr-defined]

#: Manifest ``source`` keys where an importer lists source data it left out. A non-empty
#: one means the snapshot is incomplete: the importer refuses it unless told to keep it
#: for inspection, and the quality gate reports it as an error.
DROPPED_SOURCE_KEYS: tuple[str, ...] = (
    "rejected_lines",
    "unparseable_gt_fields",
    "not_imported",
    "unexplained_superseded",
    "unordered_undecided",
)

#: A snapshot id: lowercase letters, digits, ``.``, ``_`` and ``-``, starting with a letter
#: or digit. It never holds ``/`` (a finding location is ``<snapshot_id>/<file>:<line>``),
#: ``+`` (a label set id joins member ids with ``+``) or ``@`` (``<snapshot_id>@<hash>``).
SNAPSHOT_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"

#: Every rank a layer reader can emit, with the status word a winning label of that rank
#: carries. The profile's ``precedence`` orders a subset of these. The Hub has two ranks:
#: ``hub`` for a populated value and ``hub:absent`` for a documented absence or
#: ``not_applicable``, because the sandbox lets interim ``gap_fill`` / ``derived`` replace a
#: Hub absence (``interim_labels.apply_overlay`` skips only a populated Hub field) while a
#: near_gold label never does (``gold_labels.apply_near_gold``).
RANK_STATUS: dict[str, str] = {
    "review:gold": "gold",
    "interim:correction": "interim",
    "hub": "hub",
    "review:near_gold": "near_gold",
    "interim:gap_fill": "interim",
    "interim:derived": "interim",
    "hub:absent": "hub",
    "near_gold": "near_gold",
}
RANKS: tuple[str, ...] = tuple(RANK_STATUS)

#: Ranks that are recorded (so a reviewer can see them) but are never truth, whatever a
#: profile says. ``load_profile`` refuses a precedence that names one.
UNTRUSTED_RANKS: tuple[str, ...] = ("interim:disputed", "interim:flag_mismatch")

_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True)


class LayerFact(BaseModel):
    """One label a layer offered for a (document, field), kept for the audit trail."""

    model_config = _FROZEN

    rank: str
    layer: Layer
    native_status: str
    value: JsonValue
    presence: Presence
    source: str
    reviewer: str | None = None
    basis: str | None = None
    snapshot_id: str
    stale: bool = False


class LayerRecord(BaseModel):
    """One normalised label from one layer: the unit the resolver and the checks read.

    ``content_sha256`` is the document hash the label says it was read from (a full hex
    digest or a prefix of at least 16 hex digits; ``None`` when the layer records none).
    ``file`` and ``line`` say where the label came from, for findings and tie-breaks.
    """

    model_config = _FROZEN

    document_id: str
    field: str
    value: JsonValue
    presence: Presence
    rank: str
    layer: Layer
    native_status: str
    source: str
    reviewer: str | None = None
    basis: str | None = None
    content_sha256: str | None = None
    doc_class: str | None = None
    subclass: str | None = None
    snapshot_id: str
    file: str
    line: int

    def fact(self, *, stale: bool = False) -> LayerFact:
        """This record as a ``LayerFact``."""
        return LayerFact(
            rank=self.rank,
            layer=self.layer,
            native_status=self.native_status,
            value=self.value,
            presence=self.presence,
            source=self.source,
            reviewer=self.reviewer,
            basis=self.basis,
            snapshot_id=self.snapshot_id,
            stale=stale,
        )


class CorpusEntry(BaseModel):
    """One document of the pinned dataset, as the Hub snapshot indexes it.

    ``text_verified`` is False when the importer found that the Hub text does not hash to
    ``content_sha256`` (recorded once at import time; never re-fetched by tests).
    """

    model_config = _FROZEN

    document_id: str
    doc_class: str
    subclass: str | None = None
    split: str | None = None
    content_sha256: str | None = None
    text_verified: bool = True


class FieldLabel(BaseModel):
    """The effective label of one field: the winning layer's value, plus every layer seen."""

    model_config = _FROZEN

    value: JsonValue
    status: StatusWord
    presence: Presence
    source: str
    reviewer: str | None = None
    rank: str
    layers_seen: tuple[LayerFact, ...]


class DocumentLabels(BaseModel):
    """Every effective label of one document, and which of them may be scored.

    ``verified`` is False when the document is missing from the corpus index, its Hub text
    did not verify, a layer's content hash disagrees with the index, or a layer's class
    disagrees with it; ``unverified_reasons`` says which. An unverified document keeps its
    resolved labels (so a reviewer can see them) but ``scorable_fields`` is empty.
    ``untrusted`` holds fields that only untrusted or stale layers labelled.
    """

    model_config = _FROZEN

    document_id: str
    doc_class: str | None
    subclass: str | None = None
    content_sha256: str | None = None
    verified: bool
    unverified_reasons: tuple[str, ...] = ()
    fields: Mapping[str, FieldLabel]
    untrusted: Mapping[str, tuple[LayerFact, ...]] = {}
    scorable_fields: frozenset[str]

    @field_validator("fields", "untrusted", mode="after")
    @classmethod
    def _read_only(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        """A read-only view, so nobody can change resolved labels after the gate passed."""
        return MappingProxyType(dict(value))

    @field_serializer("fields", "untrusted")
    def _as_dict(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class Finding(BaseModel):
    """One data-quality problem. ``doc``, ``field``, ``layer`` and ``location`` are set
    when they apply; ``location`` is ``<snapshot>/<file>:<line>`` or a snapshot path."""

    model_config = _FROZEN

    kind: str
    severity: Severity
    doc: str | None = None
    field: str | None = None
    layer: str | None = None
    detail: str = ""
    location: str | None = None
    known: bool = False


class ManifestFile(BaseModel):
    """One data file of a snapshot: its path inside the snapshot, hash, row and byte count."""

    model_config = _FROZEN

    path: str
    sha256: str
    rows: int
    bytes: int


class SnapshotManifest(BaseModel):
    """The parsed ``manifest.json`` of a snapshot.

    ``layer_schema`` (for example ``mailroom-labels/hub/v1``) names the native record shape
    of the files; a reader upgrades older versions at read time (ADR-0005).
    ``known_warnings`` lists ``{kind, doc, field}`` warnings accepted when the snapshot was
    committed. ``manifest_hash`` is ``hash_obj`` of every other key.
    """

    model_config = _FROZEN

    manifest_schema: str
    snapshot_id: str = Field(pattern=SNAPSHOT_ID_PATTERN)
    kind: Layer
    layer_schema: str
    source: Mapping[str, JsonValue]
    builder: str
    files: tuple[ManifestFile, ...]
    known_warnings: tuple[Mapping[str, str | None], ...] = ()
    manifest_hash: str
