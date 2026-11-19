"""Task 12: the package's public surface is exactly these 19 names, each importable."""

from __future__ import annotations

import mailroom_record

EXPECTED = [
    "Run",
    "Event",
    "Seal",
    "BlobRef",
    "BlobStore",
    "EventLog",
    "Index",
    "seal_run",
    "verify_run",
    "version_chain",
    "trace_run",
    "trace_doc",
    "import_sandbox_run",
    "audit_sandbox_run",
    "replay_timeline",
    "pack_run",
    "unpack_run",
    "prune_run",
    "GENESIS",
]


def test_public_surface_is_exactly_this() -> None:
    assert len(EXPECTED) == 19
    assert sorted(mailroom_record.__all__) == sorted(EXPECTED)


def test_every_public_name_resolves_to_its_module_object() -> None:
    from mailroom_record import blobs, importer, index, log, models, pack, replay, seal, trace, verify, versions
    from mailroom_record.importer import audit, sandbox_run

    assert importer.__name__ == "mailroom_record.importer"
    homes = {
        "Run": models, "Event": models, "Seal": models, "BlobRef": models, "GENESIS": models,
        "BlobStore": blobs, "EventLog": log, "Index": index, "seal_run": seal, "verify_run": verify,
        "version_chain": versions, "trace_run": trace, "trace_doc": trace,
        "import_sandbox_run": sandbox_run, "audit_sandbox_run": audit, "replay_timeline": replay,
        "pack_run": pack, "unpack_run": pack, "prune_run": pack,
    }
    for name in EXPECTED:
        assert getattr(mailroom_record, name) is getattr(homes[name], name), name
