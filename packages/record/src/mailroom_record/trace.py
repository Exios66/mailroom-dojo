"""Trace a run or a document through the record (Task 9).

``trace_run(log, index, run_id)`` lists every document of a run: the nodes it visited in
order, its attempts, its last decision and cause, and whether it is in review.
``trace_doc(log, index, doc_id)`` lists every run that touched a document, with its steps
and version chain in each. Both are read-only views of the log; the index only speeds up
lookups.

What a trace shows, and what it never shows
-------------------------------------------
Hashes, roles, node and agent names, kinds, attempts, ``seq`` numbers, timestamps and
counts. Never blob content, never ``payload``, and never the values inside ``decision`` or
``cause``: those are free-form, so a trace shows a ``Digest`` of them instead (their shape,
the field names of a decision object, a size and the SHA-256 of their canonical JSON), which
is enough to see that two decisions differ, never what they say.

- ``nodes`` is the node of each event in ``seq`` order, with consecutive repeats collapsed
  (a retried node appears once; ``attempts`` counts it).
- ``attempts`` maps each node to its highest ``attempt`` plus one (attempts count from 0).
- ``last_decision`` is the latest event with a ``decision``; ``last_cause`` the latest with
  a ``cause``. ``in_review`` is the index's rule: the head event's kind is ``review``.
- Run-level counts (``events``, ``in_review``) are over what could be read.

Reading (see ``_reading`` for the exact rules)
----------------------------------------------
``trace_run`` reads the run once from the log with a tolerant scan (one pass is cheaper
than one lookup per document when every document is wanted) and holds one document's
events at a time. With an index it also compares each undamaged document's head with
``Index.documents`` and says when the index is behind the log (``index`` note).

``trace_doc`` makes the index ready (``Index.ensure``), then reads the document from every
run through ``Index.iter_events``, which reads only the document's byte ranges plus what
was written since the last rebuild; ``Index.runs_for_document`` gives the runs the index
already knows hold it, so a run the index lists but whose log no longer holds the document
is reported (``missing_from_log``). Without a usable index (none given, or it cannot be
built: a read-only store) every run is scanned from the log, and the ``index`` note says so.

Neither raises on damaged data: corrupt and invalid lines, gaps, forks, broken links, torn
tails, an unreadable run header or segment each become a ``Damage``, and what can be read
is still shown. Only ``BadRunId`` (a malformed run id) and ``UnknownRun`` (no such run
directory) raise from ``trace_run``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from mailroom_record._reading import (
    BadRunId,
    Damage,
    UnknownRun,
    chain_damage,
    iter_scan_documents,
    open_index,
    read_document,
    read_header,
    require_run,
    run_ids,
    seal_files,
    stable_scan,
)
from mailroom_record.hashing import hash_obj, sha256_hex
from mailroom_record.index import Index, IndexUnavailable
from mailroom_record.log import EventLog
from mailroom_record.models import Event, Run
from mailroom_record.versions import Version, versions_from_events

__all__ = [
    "BadRunId",
    "BlobUse",
    "Damage",
    "Digest",
    "DocRun",
    "DocSummary",
    "DocTrace",
    "Noted",
    "RunHeader",
    "RunTrace",
    "Step",
    "UnknownRun",
    "digest",
    "to_json",
    "trace_doc",
    "trace_run",
]


@dataclass(frozen=True)
class Digest:
    """A free-form value as a trace shows it: its shape, field names, size and hash.

    ``keys`` are the field names of an object (for a list, of the objects in it), sorted;
    they name fields, like column names, and are the only text taken from the value.
    ``size`` is the number of fields (object), items (list) or UTF-8 bytes (string).
    ``sha256`` is the SHA-256 of the value's canonical JSON (of the UTF-8 bytes for a string).
    """

    shape: Literal["object", "list", "string", "number", "boolean"]
    keys: tuple[str, ...]
    size: int
    sha256: str


def digest(value: Any) -> Digest | None:
    """The ``Digest`` of a decision or cause; ``None`` for ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        data = value.encode("utf-8", "surrogatepass")
        return Digest("string", (), len(data), sha256_hex(data))
    if isinstance(value, dict):
        return Digest("object", tuple(sorted(value)), len(value), hash_obj(value))
    if isinstance(value, list):
        keys = sorted({k for item in value if isinstance(item, dict) for k in item})
        return Digest("list", tuple(keys), len(value), hash_obj(value))
    shape: Literal["boolean", "number"] = "boolean" if isinstance(value, bool) else "number"
    return Digest(shape, (), 1, hash_obj(value))


@dataclass(frozen=True)
class BlobUse:
    role: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Step:
    """One event of a document, without its payload or the content of its decision and cause."""

    seq: int
    ts: str
    node: str
    agent: str | None
    kind: str
    attempt: int
    decision: Digest | None
    cause: Digest | None
    inputs: tuple[BlobUse, ...]
    outputs: tuple[BlobUse, ...]


@dataclass(frozen=True)
class Noted:
    """Where a decision or cause was last recorded, and its digest."""

    seq: int
    node: str
    digest: Digest


@dataclass(frozen=True)
class DocSummary:
    doc_id: str
    events: int
    head_seq: int | None
    head_hash: str | None
    first_ts: str | None
    last_ts: str | None
    nodes: tuple[str, ...]
    attempts: dict[str, int]
    last_node: str | None
    last_kind: str | None
    last_decision: Noted | None
    last_cause: Noted | None
    in_review: bool
    damage: tuple[Damage, ...]


@dataclass(frozen=True)
class RunHeader:
    kind: str
    origin: str
    backend: str
    created_at: str
    declared_documents: int


@dataclass(frozen=True)
class RunTrace:
    run_id: str
    header: RunHeader | None
    documents: tuple[DocSummary, ...]
    events: int
    in_review: int
    seal_files: int
    damage: tuple[Damage, ...]  # run-level: header, unreadable lines and segments, torn tails
    index: str

    @property
    def damaged(self) -> bool:
        """Whether anything but a torn tail could not be read cleanly."""
        return any(d.kind != "torn_tail" for d in self.damage) or any(s.damage for s in self.documents)


@dataclass(frozen=True)
class DocRun:
    run_id: str
    header: RunHeader | None
    summary: DocSummary
    steps: tuple[Step, ...]
    versions: tuple[Version, ...]
    damage: tuple[Damage, ...]  # run-level damage met while reading this document
    source: Literal["index", "log"]


@dataclass(frozen=True)
class DocTrace:
    doc_id: str
    runs: tuple[DocRun, ...] = field(default_factory=tuple)
    index: str = ""

    @property
    def damaged(self) -> bool:
        return any(any(d.kind != "torn_tail" for d in r.damage) or r.summary.damage for r in self.runs)


def to_json(obj: Any) -> str:
    """A trace (or any of these dataclasses) as JSON, for tools that consume the CLI."""
    return json.dumps(asdict(obj), sort_keys=True, indent=2)


# --- building ---------------------------------------------------------------------


def _header(run: Run | None) -> RunHeader | None:
    if run is None:
        return None
    return RunHeader(run.kind, run.origin, run.backend, run.created_at, len(run.document_ids))


def _uses(refs: Iterable[Any]) -> tuple[BlobUse, ...]:
    return tuple(BlobUse(r.role, r.sha256, r.size) for r in refs)


def _step(e: Event) -> Step:
    return Step(
        e.seq, e.ts, e.node, e.agent, e.kind, e.attempt, digest(e.decision), digest(e.cause), _uses(e.inputs), _uses(e.outputs)
    )


def _summary(doc_id: str, events: tuple[Event, ...], damage: Iterable[Damage]) -> DocSummary:
    nodes: list[str] = []
    attempts: dict[str, int] = {}
    last_decision = last_cause = None
    for e in events:
        if not nodes or nodes[-1] != e.node:
            nodes.append(e.node)
        attempts[e.node] = max(attempts.get(e.node, 0), e.attempt + 1)
        if e.decision is not None:
            last_decision = Noted(e.seq, e.node, digest(e.decision))  # type: ignore[arg-type]
        if e.cause is not None:
            last_cause = Noted(e.seq, e.node, digest(e.cause))  # type: ignore[arg-type]
    head = events[-1] if events else None
    return DocSummary(
        doc_id=doc_id,
        events=len(events),
        head_seq=None if head is None else head.seq,
        head_hash=None if head is None else head.hash,
        first_ts=events[0].ts if events else None,
        last_ts=None if head is None else head.ts,
        nodes=tuple(nodes),
        attempts=attempts,
        last_node=None if head is None else head.node,
        last_kind=None if head is None else head.kind,
        last_decision=last_decision,
        last_cause=last_cause,
        in_review=head is not None and head.kind == "review",
        damage=tuple(damage),
    )


def trace_run(log: EventLog, index: Index | None, run_id: str) -> RunTrace:
    """Every document of ``run_id`` with its path through the run (see the module doc)."""
    rid = require_run(log, run_id)
    run, header_damage = read_header(log, rid)
    damage: list[Damage] = [] if header_damage is None else [header_damage]
    docs: list[DocSummary] = []
    try:
        scan = stable_scan(log, rid)
    except OSError as exc:
        damage.append(Damage("unreadable", None, None, f"a segment could not be read: {type(exc).__name__}"))
        scan = None
    if scan is not None:
        for doc_id, events in iter_scan_documents(scan, damage):
            docs.append(_summary(doc_id, events, chain_damage(doc_id, events)))
    note = _index_note(log, index, rid, docs)
    return RunTrace(
        run_id=rid,
        header=_header(run),
        documents=tuple(docs),
        events=sum(d.events for d in docs),
        in_review=sum(d.in_review for d in docs),
        seal_files=seal_files(log, rid),
        damage=tuple(damage),
        index=note,
    )


def _index_note(log: EventLog, index: Index | None, run_id: str, docs: list[DocSummary]) -> str:
    usable, note = open_index(log, index)
    if usable is None:
        return note
    try:
        indexed = {d.doc_id: d.head_hash for d in usable.documents(run_id)}
    except (IndexUnavailable, OSError, ValueError) as exc:
        return f"unavailable ({type(exc).__name__})"
    clean = [d for d in docs if not d.damage]
    behind = sum(1 for d in clean if indexed.get(d.doc_id) != d.head_hash)
    gone = len(set(indexed) - {d.doc_id for d in docs})
    if behind or gone:
        parts = [f"{behind} document(s) differ from the log"] if behind else []
        parts += [f"{gone} indexed document(s) not in the log"] if gone else []
        return f"{note}; behind the log: {', '.join(parts)} (run reindex)"
    return note


def trace_doc(log: EventLog, index: Index | None, doc_id: str) -> DocTrace:
    """Every run that touched ``doc_id``, with its steps and version chain (module doc)."""
    usable, note = open_index(log, index)
    listed: set[str] = set()
    if usable is not None:
        try:
            listed = {r.run_id for r in usable.runs_for_document(doc_id)}
        except (IndexUnavailable, OSError, ValueError) as exc:
            usable, note = None, f"unavailable ({type(exc).__name__}), read from the log"
    found: list[tuple[tuple[int, str, str], DocRun]] = []
    for rid in run_ids(log):
        read = read_document(log, usable, rid, doc_id)
        doc_damage = [d for d in read.damage if d.doc_id == doc_id]
        run_damage = [d for d in read.damage if d.doc_id != doc_id]
        if not read.events and not doc_damage:
            if rid not in listed:
                continue
            doc_damage.append(
                Damage("missing_from_log", doc_id, None, "the index lists this run, but its log holds no readable event of the document")
            )
        run, header_damage = read_header(log, rid)
        if header_damage is not None:
            run_damage.insert(0, header_damage)
        doc_run = DocRun(
            run_id=rid,
            header=_header(run),
            summary=_summary(doc_id, read.events, doc_damage),
            steps=tuple(_step(e) for e in read.events),
            versions=tuple(versions_from_events(read.events, log.blobs)),
            damage=tuple(run_damage),
            source=read.source,
        )
        order = (0, run.created_at, rid) if run is not None else (1, "", rid)
        found.append((order, doc_run))
    found.sort(key=lambda pair: pair[0])
    return DocTrace(doc_id, tuple(r for _, r in found), note)
