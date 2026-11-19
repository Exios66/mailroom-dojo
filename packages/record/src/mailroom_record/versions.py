"""The per-document version chain (Task 9): every blob a document's events name, in order.

``version_chain(log, run_id, doc_id)`` lists the blobs a document's events name, so a
reviewer sees the original, the transcript, every extraction attempt, what was archived
and what a person corrected, each once:

- Order follows the document's events in ``seq`` order; within one event its ``inputs``
  come before its ``outputs``. A blob appears once, at its first use: the ``role``,
  ``size``, ``node``, ``attempt``, ``ts`` and ``seq`` are those of that first use, and
  ``used_as`` says whether it was first named as an input or an output. ``order`` counts
  from 0.
- ``state`` is ``BlobStore.exists``: ``present``, ``tombstoned`` (deleted on request; the
  chain still names it) or ``missing`` (the file is gone without a tombstone: damage).
  A blob whose stored bytes are corrupt still shows as ``present``: checking that means
  reading and hashing the whole blob, which is ``verify_run``'s job (``corrupt_blob``).
- The chain holds hashes, roles, sizes and node names only, never blob content.

The document is read through the index when one is given and usable, else from the log,
and never raises on damaged data (see ``_reading``): lines that cannot be read are
skipped, a forked chain's events are all listed (both branches' blobs appear), and
``read_versions`` returns the damage next to the chain. Only a malformed run id
(``BadRunId``), an unknown run (``UnknownRun``) or a pruned run (``RunPruned``, naming the
archive that holds its events) raises. A document the run does not
hold gives an empty chain.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from mailroom_record._reading import Damage, open_index, read_document, require_run
from mailroom_record.blobs import BlobStore
from mailroom_record.index import Index
from mailroom_record.log import EventLog, refuse_if_pruned
from mailroom_record.models import Event

__all__ = ["Version", "VersionRead", "read_versions", "version_chain", "versions_from_events"]

BlobState = Literal["present", "tombstoned", "missing"]


@dataclass(frozen=True)
class Version:
    """One blob in a document's version chain, at its first use (see the module doc)."""

    order: int
    role: str
    sha256: str
    size: int
    node: str
    attempt: int
    ts: str
    state: BlobState
    seq: int
    used_as: Literal["input", "output"]


@dataclass(frozen=True)
class VersionRead:
    """A version chain plus what could not be read cleanly (empty when the chain is whole)."""

    versions: tuple[Version, ...]
    damage: tuple[Damage, ...]
    events: int


def versions_from_events(events: Iterable[Event], blobs: BlobStore) -> list[Version]:
    """The version chain of a document's events, given in ``seq`` order."""
    seen: set[str] = set()
    out: list[Version] = []
    for e in events:
        for used_as, refs in (("input", e.inputs), ("output", e.outputs)):
            for ref in refs:
                if ref.sha256 in seen:
                    continue
                seen.add(ref.sha256)
                out.append(
                    Version(
                        order=len(out),
                        role=ref.role,
                        sha256=ref.sha256,
                        size=ref.size,
                        node=e.node,
                        attempt=e.attempt,
                        ts=e.ts,
                        state=blobs.exists(ref.sha256),
                        seq=e.seq,
                        used_as=used_as,  # type: ignore[arg-type]
                    )
                )
    return out


def read_versions(log: EventLog, run_id: str, doc_id: str, *, index: Index | None = None) -> VersionRead:
    """``version_chain`` with the damage met while reading the document."""
    rid = require_run(log, run_id)
    refuse_if_pruned(log, rid, "read")  # its events are in the archive: never an empty chain
    usable, _ = open_index(log, index)
    read = read_document(log, usable, rid, doc_id)
    return VersionRead(tuple(versions_from_events(read.events, log.blobs)), read.damage, len(read.events))


def version_chain(log: EventLog, run_id: str, doc_id: str, *, index: Index | None = None) -> list[Version]:
    """Every blob ``doc_id``'s events in ``run_id`` name, in order, each once (module doc)."""
    return list(read_versions(log, run_id, doc_id, index=index).versions)
