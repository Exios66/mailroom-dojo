"""The record's data model: Run, Event, Seal and BlobRef (frozen pydantic v2 models).

Hashing rule (ADR-0002, ADR-0005). An event's ``hash`` is ``hash_obj`` of every field
except ``hash``, taken over the event *as it is stored*. ``Event.build`` computes it from
the validated, JSON-mode dump, which is exactly what the log writes, so a stored line
re-hashes to its own ``hash``. ``Seal.hash`` follows the same rule.

Models check shape, not hashes: constructing an ``Event`` does not compare ``hash`` with
``event_hash(...)``. That is deliberate. A stored v0 event, once upgraded, has fields its
hash never covered (``tokens_cached``), so its hash can only be checked on the stored
dict. Verification (Task 7) must therefore run ``event_hash(raw) == raw["hash"]`` on the
parsed line *before* ``upgrade_event`` / ``Event.from_stored``, never on the model.

``payload`` (an object) and ``decision`` (an object, or a list such as a node's decisions)
must be JSON-safe (``canonical_json`` accepts them); they are normalised to plain JSON on
validation (tuples become lists), so the model holds what the log will store. They must never hold document text or model free text: those are blobs.
Frozen models still hold plain dicts and lists; treat them as read-only.

Validation is strict (no ``"3"`` -> ``3`` or ``True`` -> ``1`` coercion; an int is still
accepted for a float field) and unknown fields are rejected, so a stored line either
validates as written or fails.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from mailroom_record.hashing import canonical_json, hash_obj
from mailroom_record.upgrade import (
    CURRENT_EVENT_VERSION,
    CURRENT_RUN_VERSION,
    CURRENT_SEAL_VERSION,
    upgrade_event,
    upgrade_run,
    upgrade_seal,
)

__all__ = [
    "BLOB_ROLES",
    "GENESIS",
    "BlobRef",
    "BlobRole",
    "Event",
    "EventKind",
    "Run",
    "RunKind",
    "Seal",
    "event_hash",
    "seal_hash",
    "utc_now",
]

GENESIS = "0" * 64

RunKind = Literal["specialist_only", "full_pipeline", "service_period"]
EventKind = Literal["handoff", "model_call", "decision", "output", "review", "error", "serving"]
BlobRole = Literal[
    "original",
    "transcript",
    "window",
    "extraction",
    "verdict",
    "archived_record",
    "human_correction",
    "report",
]
BLOB_ROLES: tuple[str, ...] = BlobRole.__args__  # type: ignore[attr-defined]

# --- field types ------------------------------------------------------------------
# Pattern constraints are checked by pydantic-core's Rust regex engine, where ``$`` matches
# only at the very end of the string, so a trailing newline is rejected (tested).

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]
# A run id names a directory (runs/<run_id>/), so it must be path-safe.
RunId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")]
Count = Annotated[int, Field(ge=0)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]

# Used with fullmatch: ``$`` with ``match`` would accept a trailing newline.
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")


def _check_ts(value: str) -> str:
    if not _TS_RE.fullmatch(value):
        raise ValueError(f"timestamp must be UTC ISO-8601 ending in Z, got {value!r}")
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ"
    datetime.strptime(value, fmt)  # rejects impossible dates such as month 13
    return value


Ts = Annotated[str, AfterValidator(_check_ts)]


def _json_dict(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"expected a JSON object, got {type(value).__name__}")
    try:
        return json.loads(canonical_json(dict(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not JSON-safe: {exc}") from exc


JsonDict = Annotated[dict[str, Any], BeforeValidator(_json_dict)]


def _json_dict_or_list(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        value = dict(value)
    elif isinstance(value, (list, tuple)):
        value = list(value)
    else:
        raise ValueError(f"expected a JSON object or list, got {type(value).__name__}")
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not JSON-safe: {exc}") from exc


# ``decision`` holds one decision object, or a node's list of decisions (Task 10).
JsonDecision = Annotated[dict[str, Any] | list[Any], BeforeValidator(_json_dict_or_list)]


def _version(current: int) -> Any:
    return Annotated[int, Field(ge=current, le=current, strict=True)]


def utc_now() -> str:
    """The current time as a UTC ISO-8601 string with microseconds and a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _without_hash(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if k != "hash"}


def event_hash(fields_without_hash: Mapping[str, Any]) -> str:
    """``hash_obj`` of every field except ``hash`` (a ``hash`` key, if present, is ignored).

    Pass the stored dict as parsed from the log line, not an upgraded one.
    """
    return hash_obj(_without_hash(fields_without_hash))


def seal_hash(fields_without_hash: Mapping[str, Any]) -> str:
    """``hash_obj`` of every seal field except ``hash`` (same rule as ``event_hash``)."""
    return hash_obj(_without_hash(fields_without_hash))


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


# --- models -----------------------------------------------------------------------


class BlobRef(_Frozen):
    """A pointer from an event to content in the blob store. ``size`` is the raw byte count."""

    sha256: Sha256
    role: BlobRole
    size: Count


class Run(_Frozen):
    """The immutable run header (``runs/<run_id>/run.json``)."""

    schema_version: _version(CURRENT_RUN_VERSION) = CURRENT_RUN_VERSION  # type: ignore[valid-type]
    run_id: RunId
    kind: RunKind
    backend: NonEmpty
    origin: Literal["native", "imported_sandbox"]
    policy_name: str | None
    policy_hash: Sha256 | None
    prompt_set: str | None
    prompt_hashes: dict[str, Sha256]
    agent_models: dict[str, str]
    code_version: str | None
    label_snapshot_ids: list[str]
    document_ids: list[str]
    caps: dict[str, FiniteFloat]
    signed_by: str | None
    created_at: Ts
    source: dict[str, str]

    @classmethod
    def from_stored(cls, raw: Mapping[str, Any]) -> Run:
        """Validate a stored run header after upgrading it to the current version."""
        return cls.model_validate(upgrade_run(raw))


class Event(_Frozen):
    """One step in one document's chain. ``doc_id`` is the content SHA-256, or ``"_run"``."""

    schema_version: _version(CURRENT_EVENT_VERSION) = CURRENT_EVENT_VERSION  # type: ignore[valid-type]
    run_id: RunId
    doc_id: NonEmpty
    seq: Count
    ts: Ts
    node: NonEmpty
    agent: str | None
    attempt: Count
    kind: EventKind
    model: str | None
    prompt_id: str | None
    tokens_in: Count | None
    tokens_out: Count | None
    tokens_cached: Count | None
    price_basis: str | None
    latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    decision: JsonDecision | None
    cause: str | None
    inputs: list[BlobRef]
    outputs: list[BlobRef]
    payload: JsonDict
    prev_hash: Sha256
    hash: Sha256

    @classmethod
    def build(cls, **fields: Any) -> Event:
        """Validate ``fields`` (everything but ``hash``) and return the Event with its hash.

        The hash is computed over the validated JSON-mode dump, the form the log stores.
        """
        if "hash" in fields:
            raise TypeError("Event.build computes the hash; do not pass one")
        draft = cls.model_validate(fields | {"hash": GENESIS})
        stored = draft.model_dump(mode="json", exclude={"hash"})
        return cls.model_validate(stored | {"hash": event_hash(stored)})

    @classmethod
    def from_stored(cls, raw: Mapping[str, Any]) -> Event:
        """Validate a stored event after upgrading it. Does not check the hash (see module doc)."""
        return cls.model_validate(upgrade_event(raw))


class Seal(_Frozen):
    """A commitment to every document chain head of a run up to a point (``seals/<n>.json``)."""

    schema_version: _version(CURRENT_SEAL_VERSION) = CURRENT_SEAL_VERSION  # type: ignore[valid-type]
    run_id: RunId
    n: Count
    sealed_at: Ts
    doc_heads: dict[NonEmpty, Sha256]
    document_ids: list[str]
    event_count: Count
    prev_seal_hash: Sha256
    hash: Sha256

    @classmethod
    def build(cls, **fields: Any) -> Seal:
        """Validate ``fields`` (everything but ``hash``) and return the Seal with its hash."""
        if "hash" in fields:
            raise TypeError("Seal.build computes the hash; do not pass one")
        draft = cls.model_validate(fields | {"hash": GENESIS})
        stored = draft.model_dump(mode="json", exclude={"hash"})
        return cls.model_validate(stored | {"hash": seal_hash(stored)})

    @classmethod
    def from_stored(cls, raw: Mapping[str, Any]) -> Seal:
        """Validate a stored seal after upgrading it. Does not check the hash."""
        return cls.model_validate(upgrade_seal(raw))
