"""Run, Event, Seal and BlobRef models, and the schema upgrade path."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError
from record_testkit import (
    blob_ref,
    make_event,
    run_fields,
    seal_fields,
    stored_v0_event_dict,
    v1_event_dict,
    v1_event_fields,
)

from mailroom_record.hashing import canonical_json, hash_obj
from mailroom_record.models import (
    BLOB_ROLES,
    GENESIS,
    BlobRef,
    Event,
    Run,
    Seal,
    event_hash,
    seal_hash,
    utc_now,
)
from mailroom_record.upgrade import (
    CURRENT_EVENT_VERSION,
    UnknownSchemaVersion,
    upgrade_event,
    upgrade_run,
    upgrade_seal,
)

# --- the plan's tests -------------------------------------------------------------


def test_event_hash_covers_every_field_but_hash() -> None:
    e = make_event(seq=0, prev_hash=GENESIS)
    assert e.hash == event_hash(e.model_dump(exclude={"hash"}))
    assert make_event(seq=0, prev_hash=GENESIS, cause="x").hash != e.hash


def test_newer_schema_version_is_refused() -> None:
    with pytest.raises(UnknownSchemaVersion):
        upgrade_event({"schema_version": 99})


def test_version_0_event_upgrades() -> None:  # Review Focus 5
    raw = v1_event_dict() | {"schema_version": 0}
    raw.pop("tokens_cached")  # the field v0 lacked
    assert upgrade_event(raw)["schema_version"] == 1
    assert upgrade_event(raw)["tokens_cached"] is None


def test_absent_fields_stay_none() -> None:
    assert make_event(seq=0, prev_hash=GENESIS, model=None).model is None


# --- hashing covers every field ---------------------------------------------------

EVENT_FIELDS = (
    "schema_version run_id doc_id seq ts node agent attempt kind model prompt_id tokens_in "
    "tokens_out tokens_cached price_basis latency_ms decision cause inputs outputs payload "
    "prev_hash hash"
).split()

# One different, valid value per hashed field (schema_version is covered separately:
# the model accepts only the current version).
CHANGED: dict[str, Any] = {
    "run_id": "r2",
    "doc_id": "_run",
    "seq": 1,
    "ts": "2026-11-18T12:00:01Z",
    "node": "extract",
    "agent": None,
    "attempt": 1,
    "kind": "output",
    "model": "other-model",
    "prompt_id": None,
    "tokens_in": 121,
    "tokens_out": None,
    "tokens_cached": 5,
    "price_basis": None,
    "latency_ms": 812.25,
    "decision": None,
    "cause": "timeout",
    "inputs": [],
    "outputs": [{"sha256": "e" * 64, "role": "extraction", "size": 4}],
    "payload": {"route": "review"},
    "prev_hash": "f" * 64,
}


def test_event_field_list_is_the_plans() -> None:
    assert list(Event.model_fields) == EVENT_FIELDS


@pytest.mark.parametrize("field", sorted(CHANGED))
def test_changing_any_field_changes_the_hash(field: str) -> None:
    base = make_event()
    changed = make_event(**{field: CHANGED[field]})
    assert getattr(changed, field) != getattr(base, field)
    assert changed.hash != base.hash


def test_every_hashed_field_has_a_change_case() -> None:
    assert set(CHANGED) == set(EVENT_FIELDS) - {"schema_version", "hash"}


def test_schema_version_is_inside_the_hash() -> None:
    raw = v1_event_fields()
    assert event_hash(raw) != event_hash(raw | {"schema_version": 0})


def test_event_hash_ignores_a_hash_key_and_equals_hash_obj() -> None:
    raw = v1_event_dict()
    without = {k: v for k, v in raw.items() if k != "hash"}
    assert event_hash(raw) == event_hash(without) == hash_obj(without) == raw["hash"]


# --- stored form round trip -------------------------------------------------------


def test_stored_line_round_trips_with_the_same_hash() -> None:
    e = make_event()
    line = canonical_json(e.model_dump(mode="json"))
    raw = json.loads(line)
    assert event_hash(raw) == raw["hash"] == e.hash
    assert Event.from_stored(raw) == e


def test_build_hashes_the_validated_form_not_the_input() -> None:
    # latency 812 (int) is coerced to 812.0; the hash must match what is stored.
    e = make_event(latency_ms=812)
    raw = json.loads(canonical_json(e.model_dump(mode="json")))
    assert raw["latency_ms"] == 812.0
    assert event_hash(raw) == e.hash


def test_build_rejects_a_caller_supplied_hash() -> None:
    with pytest.raises(TypeError):
        Event.build(**v1_event_fields(), hash="0" * 64)


def test_model_does_not_check_the_hash_on_construction() -> None:
    # Verify (Task 7) checks hashes on the stored form; the model only checks shape.
    e = Event(**v1_event_fields(), hash="1" * 64)
    assert e.hash == "1" * 64


def test_models_are_frozen() -> None:
    e = make_event()
    with pytest.raises(ValidationError):
        e.seq = 5  # type: ignore[misc]


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_event(extra_field=1)


# --- groundwork for Task 7: verify hashes the stored (un-upgraded) form -----------


def test_upgraded_v0_event_keeps_a_verifiable_stored_hash() -> None:
    """ADR-0005: the hash of a v0 line is over its v0 form, never over the upgrade.

    Task 7 checks ``event_hash(raw_line_dict) == raw_line_dict["hash"]`` on the parsed
    line BEFORE ``upgrade_event``; this test pins why that is the only correct order.
    """
    raw = stored_v0_event_dict()
    stored = copy.deepcopy(raw)
    event = Event.from_stored(raw)
    assert raw == stored  # upgrading never mutates the stored dict
    assert event.schema_version == CURRENT_EVENT_VERSION and event.tokens_cached is None
    assert event_hash(raw) == raw["hash"] == event.hash  # stored form verifies
    assert event_hash(event.model_dump(mode="json")) != event.hash  # upgraded form does not


def test_upgrade_does_not_mutate_its_input() -> None:
    raw = stored_v0_event_dict()
    before = copy.deepcopy(raw)
    upgrade_event(raw)
    assert raw == before


def test_current_version_upgrade_is_a_copy() -> None:
    raw = v1_event_dict()
    out = upgrade_event(raw)
    assert out == raw and out is not raw


@pytest.mark.parametrize(
    "raw",
    [{}, {"schema_version": None}, {"schema_version": "1"}, {"schema_version": True},
     {"schema_version": -1}, {"schema_version": 2}],
)
def test_bad_or_future_versions_are_refused(raw: dict[str, Any]) -> None:
    with pytest.raises(UnknownSchemaVersion):
        upgrade_event(raw)
    with pytest.raises(UnknownSchemaVersion):
        upgrade_run(raw)
    with pytest.raises(UnknownSchemaVersion):
        upgrade_seal(raw)


def test_unknown_schema_version_is_a_value_error() -> None:
    assert issubclass(UnknownSchemaVersion, ValueError)


def test_model_accepts_only_the_current_version() -> None:
    with pytest.raises(ValidationError):
        Event(**v1_event_fields(schema_version=0), hash="1" * 64)


# --- runs -------------------------------------------------------------------------


def test_run_round_trips() -> None:
    run = Run(**run_fields())
    assert Run.from_stored(json.loads(canonical_json(run.model_dump(mode="json")))) == run


def test_version_0_run_upgrades_with_signed_by_absent() -> None:
    raw = run_fields(schema_version=0)
    raw.pop("signed_by")
    out = upgrade_run(raw)
    assert out["schema_version"] == 1 and out["signed_by"] is None
    assert Run.from_stored(raw).signed_by is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "batch"},
        {"origin": "imported"},
        {"created_at": "2026-11-18T12:00:00+00:00"},
        {"created_at": "2026-11-18 12:00:00Z"},
        {"created_at": "2026-13-18T12:00:00Z"},
        {"policy_hash": "ABC"},
        {"prompt_hashes": {"sorter": "C" * 64}},
        {"caps": {"usd": float("inf")}},
        {"run_id": ""},
        {"run_id": "../escape"},
        {"run_id": "a/b"},
    ],
)
def test_run_rejects_bad_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Run(**run_fields(**overrides))


# --- seals ------------------------------------------------------------------------


def test_seal_build_hashes_every_field_but_hash() -> None:
    seal = Seal.build(**seal_fields())
    assert seal.hash == seal_hash(seal.model_dump(exclude={"hash"})) == seal_hash(
        seal.model_dump(mode="json")
    )
    assert Seal.build(**seal_fields(event_count=4)).hash != seal.hash
    assert Seal.from_stored(seal.model_dump(mode="json")) == seal


def test_seal_rejects_a_bad_head() -> None:
    with pytest.raises(ValidationError):
        Seal.build(**seal_fields(doc_heads={"d1": "not-a-hash"}))


# --- blob refs, timestamps and JSON-safe payloads ---------------------------------


def test_blob_roles_are_the_eight() -> None:
    assert BLOB_ROLES == (
        "original", "transcript", "window", "extraction", "verdict",
        "archived_record", "human_correction", "report",
    )
    for role in BLOB_ROLES:
        assert blob_ref(role=role).role == role


@pytest.mark.parametrize(
    "overrides",
    [{"role": "notes"}, {"size": -1}, {"sha256": "A" * 64}, {"sha256": "a" * 63},
     {"sha256": "../" + "a" * 61}],
)
def test_blob_ref_rejects_bad_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        blob_ref(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"ts": "2026-11-18T12:00:00"},
        {"ts": "2026-11-18T07:00:00-05:00"},
        {"prev_hash": "0" * 63},
        {"hash_typo": 1},
        {"seq": -1},
        {"seq": True},
        {"seq": "3"},
        {"latency_ms": "1.5"},
        {"attempt": -1},
        {"tokens_in": -1},
        {"latency_ms": float("nan")},
        {"latency_ms": -1.0},
        {"kind": "note"},
        {"node": ""},
        {"doc_id": ""},
        {"payload": {"x": float("nan")}},
        {"payload": {"x": {1, 2}}},
        {"payload": {"x": b"bytes"}},
        {"payload": {1: "non-string key"}},
        {"decision": {"x": object()}},
    ],
)
def test_event_rejects_bad_values(overrides: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, TypeError)):
        make_event(**overrides)


def test_payload_is_normalised_to_plain_json() -> None:
    e = make_event(payload={"path": ("a", "b"), "n": 1, "ok": True, "none": None})
    assert e.payload == {"path": ["a", "b"], "n": 1, "ok": True, "none": None}
    assert event_hash(json.loads(canonical_json(e.model_dump(mode="json")))) == e.hash


@pytest.mark.parametrize("missing", ["payload", "inputs", "outputs", "tokens_cached", "cause"])
def test_a_missing_field_is_rejected_not_defaulted(missing: str) -> None:
    fields = v1_event_fields()
    fields.pop(missing)
    with pytest.raises(ValidationError):
        Event.build(**fields)


def test_utc_now_is_a_valid_timestamp() -> None:
    ts = utc_now()
    assert ts.endswith("Z")
    assert make_event(ts=ts).ts == ts


def test_blob_ref_is_hashable_and_frozen() -> None:
    ref = blob_ref()
    assert isinstance(ref, BlobRef) and hash(ref) == hash(blob_ref())
    with pytest.raises(ValidationError):
        ref.size = 9  # type: ignore[misc]
