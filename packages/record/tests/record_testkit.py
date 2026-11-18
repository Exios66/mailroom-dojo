"""Plain helpers shared by the record tests (fixtures live in conftest.py).

Everything here is synthetic: no real document text, no network, no model call.
"""

from __future__ import annotations

from typing import Any

from mailroom_record.models import GENESIS, BlobRef, Event, event_hash

SENTINEL = "SECRET-DOCUMENT-TEXT"

DOC_A = "a" * 64
SHA_X = "b" * 64


def v1_event_fields(**overrides: Any) -> dict[str, Any]:
    """Every Event field except ``hash``, at schema version 1, with every optional set."""
    fields: dict[str, Any] = {
        "schema_version": 1,
        "run_id": "r1",
        "doc_id": DOC_A,
        "seq": 0,
        "ts": "2026-11-18T12:00:00.000000Z",
        "node": "classify",
        "agent": "sorter",
        "attempt": 0,
        "kind": "decision",
        "model": "test-model",
        "prompt_id": "sorter@v1",
        "tokens_in": 120,
        "tokens_out": 30,
        "tokens_cached": 0,
        "price_basis": "list",
        "latency_ms": 812.5,
        "decision": {"chosen": "contract", "confidence": 0.91},
        "cause": None,
        "inputs": [{"sha256": SHA_X, "role": "transcript", "size": 10}],
        "outputs": [],
        "payload": {"route": "extract"},
        "prev_hash": GENESIS,
    }
    fields.update(overrides)
    return fields


def make_event(**overrides: Any) -> Event:
    """A valid current-version Event whose hash is computed by ``Event.build``."""
    return Event.build(**v1_event_fields(**overrides))


def v1_event_dict(**overrides: Any) -> dict[str, Any]:
    """A stored v1 event line as a dict, ``hash`` included."""
    return make_event(**overrides).model_dump(mode="json")


def stored_v0_event_dict(**overrides: Any) -> dict[str, Any]:
    """A stored v0 event line: no ``tokens_cached``, hash computed over that v0 form."""
    raw = v1_event_fields(**overrides) | {"schema_version": 0}
    raw.pop("tokens_cached")
    raw["hash"] = event_hash(raw)
    return raw


def run_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "run_id": "r1",
        "kind": "specialist_only",
        "backend": "mock",
        "origin": "native",
        "policy_name": None,
        "policy_hash": None,
        "prompt_set": "deploy_v1",
        "prompt_hashes": {"sorter": "c" * 64},
        "agent_models": {"sorter": "test-model"},
        "code_version": None,
        "label_snapshot_ids": [],
        "document_ids": [DOC_A],
        "caps": {"usd": 1.5},
        "signed_by": None,
        "created_at": "2026-11-18T12:00:00Z",
        "source": {},
    }
    fields.update(overrides)
    return fields


def seal_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "run_id": "r1",
        "n": 0,
        "sealed_at": "2026-11-18T13:00:00Z",
        "doc_heads": {DOC_A: "d" * 64},
        "document_ids": [DOC_A],
        "event_count": 3,
        "prev_seal_hash": GENESIS,
    }
    fields.update(overrides)
    return fields


def blob_ref(**overrides: Any) -> BlobRef:
    return BlobRef(**({"sha256": SHA_X, "role": "original", "size": 3} | overrides))


def put_in_child(root: str, data: bytes, role: str, rounds: int) -> str:
    """Process target: put the same bytes ``rounds`` times into a store at ``root``."""
    from pathlib import Path

    from mailroom_record.blobs import BlobStore

    store = BlobStore(Path(root))
    sha = ""
    for _ in range(rounds):
        sha = store.put(data, role).sha256
    return sha
