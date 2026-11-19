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

import copy
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
        """A read-only view, so nobody can add, drop or replace a resolved label after the
        gate passed. A list or dict ``value`` stays a plain JSON value; ``LabelSet`` hands
        out copies of those, so a caller cannot change the stored one either."""
        return MappingProxyType(dict(value))

    @field_serializer("fields", "untrusted")
    def _as_dict(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)

    @field_serializer("scorable_fields")
    def _sorted_fields(self, value: frozenset[str]) -> list[str]:
        """Sorted, so a dump is the same bytes whatever the process's hash seed."""
        return sorted(value)

    # A ``MappingProxyType`` neither pickles nor deep-copies, so the state carries plain
    # dicts and ``__setstate__`` wraps them in read-only views again.
    def __getstate__(self) -> dict[Any, Any]:
        state = super().__getstate__()
        values = dict(state["__dict__"])
        for name in _READ_ONLY:
            values[name] = dict(values[name])
        return {**state, "__dict__": values}

    def __setstate__(self, state: dict[Any, Any]) -> None:
        values = dict(state["__dict__"])
        for name in _READ_ONLY:
            values[name] = MappingProxyType(dict(values[name]))
        super().__setstate__({**state, "__dict__": values})

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> DocumentLabels:
        twin = type(self).__new__(type(self))
        twin.__setstate__(copy.deepcopy(self.__getstate__(), memo))
        return twin


_READ_ONLY = ("fields", "untrusted")


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

    ``source`` and ``known_warnings`` hold JSON lists and objects, which a frozen model does
    not protect on its own. They are stored deep read-only under ``stored_source`` and
    ``stored_known_warnings`` (validated from the ``source`` and ``known_warnings`` keys):
    every object is a read-only view and every list a tuple, so nobody can edit them in
    place. ``source`` and ``known_warnings`` hand out fresh plain copies, so a caller that
    edits what it was handed never changes what the layer readers and the quality gate see,
    and the manifest stays the one ``manifest_hash`` pins. A dump (``by_alias=True`` gives
    back the ``source`` and ``known_warnings`` keys) has plain dicts and lists again, and the
    model copies (``copy.copy``, ``copy.deepcopy``) and pickles. As for every frozen model
    here, a deliberate write through ``__dict__`` or ``object.__setattr__`` is not defended
    against.
    """

    model_config = _FROZEN

    manifest_schema: str
    snapshot_id: str = Field(pattern=SNAPSHOT_ID_PATTERN)
    kind: Layer
    layer_schema: str
    stored_source: Mapping[str, JsonValue] = Field(alias="source", repr=False)
    builder: str
    files: tuple[ManifestFile, ...]
    stored_known_warnings: tuple[Mapping[str, str | None], ...] = Field(default=(), alias="known_warnings")
    manifest_hash: str

    @field_validator("stored_source", "stored_known_warnings", mode="after")
    @classmethod
    def _deep_read_only(cls, value: Any) -> Any:
        return _deep_frozen(value)

    @field_serializer("stored_source")
    def _source_plain(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _thawed_json(value)  # type: ignore[no-any-return]

    @field_serializer("stored_known_warnings")
    def _warnings_plain(
        self, value: tuple[Mapping[str, str | None], ...]
    ) -> tuple[dict[str, str | None], ...]:
        return tuple(dict(w) for w in value)

    @property
    def source(self) -> dict[str, JsonValue]:
        """A copy of the manifest's ``source`` object."""
        return _thawed_json(self.stored_source)  # type: ignore[no-any-return]

    @property
    def known_warnings(self) -> tuple[dict[str, str | None], ...]:
        """A copy of the manifest's ``known_warnings`` entries."""
        return tuple(dict(w) for w in self.stored_known_warnings)

    # A ``MappingProxyType`` neither pickles nor deep-copies, so the state carries plain
    # JSON values and ``__setstate__`` freezes them again.
    def __getstate__(self) -> dict[Any, Any]:
        state = super().__getstate__()
        values = dict(state["__dict__"])
        for name in _MANIFEST_READ_ONLY:
            values[name] = _thawed_json(values[name])
        return {**state, "__dict__": values}

    def __setstate__(self, state: dict[Any, Any]) -> None:
        values = dict(state["__dict__"])
        for name in _MANIFEST_READ_ONLY:
            values[name] = _deep_frozen(values[name])
        super().__setstate__({**state, "__dict__": values})

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> SnapshotManifest:
        twin = type(self).__new__(type(self))
        twin.__setstate__(copy.deepcopy(self.__getstate__(), memo))
        return twin


_MANIFEST_READ_ONLY = ("stored_source", "stored_known_warnings")


def _deep_frozen(value: Any) -> Any:
    """``value`` with every JSON object a read-only view and every list a tuple."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: _deep_frozen(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_frozen(v) for v in value)
    return value


def _thawed_json(value: Any) -> Any:
    """A fresh plain JSON value (dicts and lists) from a deep-frozen one."""
    if isinstance(value, Mapping):
        return {k: _thawed_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thawed_json(v) for v in value]
    return value
