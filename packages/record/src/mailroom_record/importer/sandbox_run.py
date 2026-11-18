"""Import a sandbox run folder into a record (Task 10, ADR-0006).

``import_sandbox_run(folder, log, blobs)`` reads one run folder written by the sandbox
(``dataset.jsonl``, ``nodes.jsonl``, ``items.jsonl``, ``events.jsonl``, ``spec.lock.json``,
``prompt.lock.json``, ``checkpoint.json`` and ``spend-authorization.json``; only
``dataset.jsonl`` is required) and writes one run with ``origin="imported_sandbox"``,
sealed at the end.

Mapping
-------
- The run: ``kind`` is ``full_pipeline`` when ``nodes.jsonl`` has a ``classify`` row, else
  ``specialist_only``; ``backend`` is the lock's ``engine.kind``; ``agent_models``;
  ``prompt_hashes`` from ``prompt.lock.json`` (``agents.<name>.sha256``); ``prompt_set``
  from the lock's ``prompt.default``; ``code_version`` from ``git.commit`` (``-dirty``
  appended when the tree was dirty); ``signed_by`` from the spend receipt's initials;
  ``policy_name="legacy"``, ``policy_hash=None``; ``source`` holds the folder name,
  ``spec_hash``, ``source_run_id``, ``engine_model`` and ``profile`` (each when present).
  ``created_at`` is the lock's ``created_at``, else the earliest time in the folder, so the
  header is the same on every import of the same folder.
- A document's ``doc_id`` is its ``content_sha256``. Its first event (``node="dataset"``,
  ``kind="handoff"``) outputs the text as an ``original`` blob and keeps, in ``payload``,
  the dataset row id (``source_id``), ``expected_doc_class`` as ``draw_class``, ``split``
  and ``source_revision``, each scrubbed (below). No other ``expected_*`` field is imported: labels belong to the
  labels package.
- Each ``done``, ``paused``, ``error`` or ``failed`` row of ``nodes.jsonl`` is one event,
  its kind decided in this order: ``review`` for ``human_review``; ``error`` for ``error``
  or ``failed``; ``decision`` when the row has decisions; else ``handoff``. ``decision`` is
  the row's decision list minus each ``options`` entry (model output text), scrubbed as a
  payload is: a ``question`` or ``answer`` that is not a short token (free text) is stored as
  its SHA-256 and length.
  ``latency_ms`` is the row's ``ms``; ``ts`` is the finish time; ``attempt`` counts the
  document's earlier finished rows of the same node. ``cause`` is the error's type name,
  never its message.
- Floor replay (spec R7). A finishing row is paired with the first open ``running`` row
  of the same ``(doc, node)`` in file order (and the same ``step`` when the finishing row
  names one; the sandbox writes ``step`` on ``running`` rows only). It is not an event of
  its own: its time is kept on the finishing event as ``payload.started_ts`` and its step
  as ``payload.step`` (``payload.finish_step`` is ``true`` when the finishing row named the
  step itself). A ``running`` row with no finish (in flight when the run stopped)
  becomes one ``handoff`` event with ``payload.status="running"``. Every node event keeps
  ``status``, ``step`` and ``stage`` when present, and ``order`` (its row's 0-based place
  in ``nodes.jsonl``; ``started_order`` for the paired start), so the timeline can be
  rebuilt in the source's order (``mailroom_record.replay``).
- Each ``items.jsonl`` row is one event on its document: ``kind="output"``, or ``"error"``
  when the item failed (``ok`` false). ``decision`` holds ``doc_type``, ``stage``, both
  confidences, ``review_causes``, ``review_verdict`` and ``escalation_reason``;
  ``pred.extracted_data`` without ``_report`` is an ``extraction`` blob and ``_report`` a
  ``report`` blob; ``payload`` keeps ``usage_by_agent``, the sandbox ``score`` dict as
  ``legacy_score``, and the item's token totals.
- ``events.jsonl`` rows are ``_run`` events (``kind="handoff"``, ``node=<event name>``), and
  ``checkpoint.json`` a last ``_run`` event (``node="checkpoint"``) with ``state``,
  ``cursor`` and ``total``.
- What the import skipped (``ImportResult.warnings``: a torn last line of a JSONL file, as a
  run stopped mid-write leaves) is kept too: a first ``_run`` event (``node="import"``,
  ``kind="handoff"``) with ``payload.skipped_torn_lines``, a list of ``{"file", "bytes"}``
  (the file name and the bytes skipped, never their content). A folder with nothing
  skipped has no such event.
- Every payload carries ``floor`` (``draw``, ``node``, ``item``, ``lifecycle``,
  ``checkpoint`` or ``import``): what the event is to the floor replay.
- Strings in ``payload`` and ``decision`` other than short tokens (ids, names, labels; never
  an email, a URL or a path) are stored as their SHA-256 and length (``_folder.scrub``).
  The fields the schema keeps strings are held to the same rule (``_folder.named``): an
  event's ``node``, ``agent`` and ``model``, and the run header's ``backend``, the names and
  values of ``agent_models``, the names of ``prompt_hashes``, ``prompt_set``,
  ``code_version``, ``signed_by`` and every ``source`` value are stored as they are when
  they are tokens, else as ``sha256:<hex>:<chars>``. Only the run id is kept as given: it
  must match ``[A-Za-z0-9][A-Za-z0-9._-]{0,199}`` (else the folder name is used, and the
  import refuses a folder with neither), the record's run id pattern. So no document or
  model free text enters the log. Text that is not valid UTF-8 (a lone
  surrogate in ``doc_text`` or a ``_report``) raises ``SandboxFolderInvalid`` naming the file
  and line.
- A field the folder did not capture is ``None`` and named in ``absent_fields``; a row
  without a usable time takes the time of the event before it and says so with
  ``payload.ts_absent``.

Idempotent and resumable
------------------------
Every event is a pure function of the folder, so its hash is known before anything is
written. A second import of the same folder finds every chain head and a seal over them
and returns ``already_imported`` without writing anything. An import that died part way
(any prefix of each chain on disk, sealed or not) is finished by the next one, which checks
that each stored head is the expected event at that place and appends the rest. A stored
event that differs, a different run header, or a document the folder does not hold raises
``ImportConflict`` and writes nothing. A run that is pruned, or part way through a prune
(``prune.json`` is there), raises ``RunPruned`` (an ``ImportConflict``) naming its archive,
and writes nothing: its segments may be gone, so the import cannot check them against the
folder, and reporting ``already_imported`` would claim what it has not checked. A prune
that finishes after that check is caught by the store itself: the writer and ``seal_run``
refuse a pruned run under the maintenance lock (``log.refuse_if_pruned``), and the import
raises the same ``RunPruned``. After ``unpack_run`` the same folder is
``already_imported`` (or finished, if the import had stopped part way).

Write order: the writer is opened first, then every blob is put, then the events are
appended, then the run is sealed. The open writer holds the store's maintenance lock
shared, so a concurrent ``prune_run`` cannot delete a blob this import has put but not yet
named.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mailroom_record.blobs import BlobStore
from mailroom_record.hashing import canonical_json
from mailroom_record.importer._folder import (
    SandboxFolderInvalid,
    cause_token,
    epoch,
    iso,
    named,
    read_json,
    read_jsonl,
    scrub,
    shown,
    token,
)
from mailroom_record.log import EventLog, RunExists, pruned_archive
from mailroom_record.log import RunPruned as StoreRunPruned
from mailroom_record.models import GENESIS, BlobRef, Event, Run, validation_detail
from mailroom_record.seal import list_seals, seal_run

__all__ = ["ImportConflict", "ImportResult", "RunPruned", "SandboxFolderInvalid", "import_sandbox_run"]

WRITER_ID = "import"
RUN_DOC = "_run"
# What the sandbox did not capture, for every folder (ADR-0006).
ALWAYS_ABSENT = (
    "tokens_in per call",
    "tokens_out per call",
    "tokens_cached per call",
    "prompt_id per call",
    "price_basis per call",
    "extraction per attempt",
)
# The legacy sandbox format's own names (the importer reads that format; the record does not).
_REVIEW_NODE = "human_review"
_CLASSIFY_NODE = "classify"
_ERROR_STATUSES = frozenset({"error", "failed"})
_OUTCOME_KEYS = (
    "doc_type",
    "stage",
    "classification_confidence",
    "extraction_confidence",
    "review_causes",
    "review_verdict",
    "escalation_reason",
)
_PRED_EXTRAS = ("reviewer_doc_type", "reviewer_confidence", "offline_fallback")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_EPOCH_ZERO = "1970-01-01T00:00:00.000000Z"


class ImportConflict(RuntimeError):
    """The store already holds this run id with a different header or different events."""


class RunPruned(ImportConflict, StoreRunPruned):
    """The store holds this run pruned (or part way through a prune): its segments may be
    gone, so the import cannot check them against the folder. Unpack the archive first.
    Both an ``ImportConflict`` and the store's ``log.RunPruned``."""


def _refuse_if_pruned(log: EventLog, run_id: str) -> None:
    archive = pruned_archive(log, run_id)
    if archive is not None:
        raise RunPruned(
            f"run {run_id!r} is pruned in this store, so its events cannot be checked against the folder; "
            f"unpack {archive} first (mailroom-record unpack), then import again"
        )


@contextlib.contextmanager
def _pruned_is_a_conflict() -> Iterator[None]:
    """The store refused to write or seal a pruned run (a prune finished after the import's
    own check): raise the importer's ``RunPruned``, an ``ImportConflict``."""
    try:
        yield
    except RunPruned:
        raise
    except StoreRunPruned as exc:
        raise RunPruned(f"{exc}, then import again") from None


@dataclass(frozen=True)
class ImportResult:
    """What one import did. ``events`` counts every event of the run (``_run`` included)."""

    run_id: str
    status: Literal["imported", "already_imported"]
    documents: int
    events: int
    absent_fields: list[str]
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Plan:
    run: Run
    chains: dict[str, list[dict[str, Any]]]  # doc_id -> append() keyword arguments, in chain order
    hashes: dict[str, list[str]]  # doc_id -> the hash each of those events will have
    blobs: dict[str, tuple[bytes, str]]  # sha256 -> (bytes, role)
    absent: list[str]
    warnings: list[str]
    documents: int


def import_sandbox_run(folder: Path, log: EventLog, blobs: BlobStore) -> ImportResult:
    """Import one sandbox run folder (see the module doc). Raises ``SandboxFolderInvalid``
    for a folder that cannot be read and ``ImportConflict`` for a clash with the store."""
    plan = _build_plan(Path(folder))
    run_id = plan.run.run_id
    total = sum(len(c) for c in plan.chains.values())

    def result(status: Literal["imported", "already_imported"]) -> ImportResult:
        return ImportResult(run_id, status, plan.documents, total, list(plan.absent), list(plan.warnings))

    heads = _stored_heads(log, plan)
    if heads is None:
        try:
            log.create_run(plan.run)
            heads = {}
        except RunExists:  # another import created it meanwhile: check it like any stored run
            heads = _stored_heads(log, plan) or {}
    finals = {d: h[-1] for d, h in plan.hashes.items()}
    complete = all(heads.get(d, (-1, ""))[1] == h for d, h in finals.items())
    if complete:
        seals = list_seals(log, run_id)
        if seals and seals[-1].doc_heads == finals:
            return result("already_imported")
    else:
        # The writer refuses a pruned run under its shared maintenance lock (log.refuse_if_pruned),
        # so a prune that finished since _stored_heads is caught before anything is written.
        with _pruned_is_a_conflict():
            writer = log.open_writer(run_id, WRITER_ID, flush_every=256)
        with writer:
            for data, role in plan.blobs.values():
                blobs.put(data, role)
            for doc_id, chain in plan.chains.items():
                start = heads.get(doc_id, (-1, ""))[0] + 1
                for i in range(start, len(chain)):
                    event = writer.append(**chain[i])
                    if event.hash != plan.hashes[doc_id][i]:  # never expected: the plan is the writer's input
                        raise ImportConflict(f"run {run_id!r}: document {doc_id} seq {i} hashed differently")
    with _pruned_is_a_conflict():  # seal_run refuses a pruned run under its locks
        seal_run(log, run_id)
    return result("imported")


def _stored_heads(log: EventLog, plan: _Plan) -> dict[str, tuple[int, str]] | None:
    """The stored heads when the run exists and agrees with the plan; ``None`` when it does not
    exist. Raises ``ImportConflict`` when it exists and disagrees."""
    run_id = plan.run.run_id
    if not (log.run_dir(run_id) / "run.json").is_file():
        return None
    stored = log.read_run(run_id)
    if stored != plan.run:
        diff = sorted(k for k in Run.model_fields if getattr(stored, k) != getattr(plan.run, k))
        raise ImportConflict(f"run {run_id!r} is already stored with a different header ({', '.join(diff)})")
    _refuse_if_pruned(log, run_id)
    heads = log.doc_head_seqs(run_id)
    for doc_id, (seq, h) in heads.items():
        expected = plan.hashes.get(doc_id)
        if expected is None:
            raise ImportConflict(f"run {run_id!r} holds document {shown(doc_id)}, which this folder does not")
        if seq >= len(expected) or expected[seq] != h:
            raise ImportConflict(
                f"run {run_id!r}: stored document {shown(doc_id)} seq {seq} is not the event this folder gives"
            )
    return heads


# --- planning -------------------------------------------------------------------------


@dataclass
class _Doc:
    source_id: str
    doc_id: str
    row: dict[str, Any]
    line: int
    original: BlobRef | None = None
    times: list[float] = field(default_factory=list)
    events: list[tuple[float, dict[str, Any]]] = field(default_factory=list)  # (sort key, fields)


def _build_plan(folder: Path) -> _Plan:
    if not folder.is_dir():
        raise SandboxFolderInvalid(f"{folder.name}: not a directory")
    warnings: list[str] = []
    torn: list[dict[str, Any]] = []
    absent: list[str] = list(ALWAYS_ABSENT)
    dataset = read_jsonl(folder / "dataset.jsonl", warnings, torn)
    if dataset is None:
        raise SandboxFolderInvalid(f"{folder.name}: no dataset.jsonl")
    nodes = read_jsonl(folder / "nodes.jsonl", warnings, torn)
    items = read_jsonl(folder / "items.jsonl", warnings, torn)
    run_events = read_jsonl(folder / "events.jsonl", warnings, torn)
    lock = read_json(folder / "spec.lock.json")
    prompt_lock = read_json(folder / "prompt.lock.json")
    checkpoint = read_json(folder / "checkpoint.json")
    receipt = read_json(folder / "spend-authorization.json")
    for name, value in (
        ("nodes", nodes), ("items", items), ("events", run_events), ("spec_lock", lock), ("checkpoint", checkpoint)
    ):
        if value is None:
            absent.append(name)

    blob_map: dict[str, tuple[bytes, str]] = {}

    def put(data: bytes, role: str) -> BlobRef:
        sha = hashlib.sha256(data).hexdigest()
        blob_map.setdefault(sha, (data, role))
        return BlobRef(sha256=sha, role=role, size=len(data))

    docs = _documents(dataset, absent, put)
    by_source = {d.source_id: d for d in docs}

    def doc_for(source: Any, where: str) -> _Doc:
        doc = by_source.get(source if isinstance(source, str) else "")
        if doc is None:
            raise SandboxFolderInvalid(f"{where}: names document {shown(source)}, which dataset.jsonl does not hold")
        return doc

    node_rows = nodes or []
    for line, row in node_rows:
        t = epoch(row.get("ts"))
        if t is not None:
            doc_for(row.get("doc"), f"nodes.jsonl line {line}").times.append(t)
    for line, row in items or []:
        t = epoch(row.get("ts"))
        if t is not None:
            doc_for(row.get("item_id") or row.get("id"), f"items.jsonl line {line}").times.append(t)

    lock = lock or {}
    all_times = [t for d in docs for t in d.times]
    all_times += [t for _, r in run_events or [] if (t := epoch(r.get("ts"))) is not None]
    created = epoch(lock.get("created_at"))
    if created is None and all_times:
        created = min(all_times)
    created_at = iso(created) if created is not None else None
    if created_at is None:
        absent.append("created_at")
        created_at = _EPOCH_ZERO

    _node_events(node_rows, doc_for)
    _item_events(items or [], len(node_rows), doc_for, put)
    run = _run_header(folder, lock, prompt_lock, receipt, node_rows, docs, created_at, absent)

    chains: dict[str, list[dict[str, Any]]] = {}
    for d in docs:
        first = min(d.times) if d.times else created
        draw_payload = {
            "floor": "draw",
            "source_id": scrub(d.source_id),
            "draw_class": scrub(d.row.get("expected_doc_class")),
            "split": scrub(d.row.get("split")),
            "source_revision": scrub(d.row.get("source_revision")),
        }
        draw = _fields(
            d.doc_id, "dataset", "handoff", ts=first, outputs=[d.original] if d.original else [],
            payload={k: v for k, v in draw_payload.items() if v is not None},
        )
        chain = [draw] + [f for _, f in sorted(d.events, key=lambda p: p[0])]
        chains[d.doc_id] = _fill_times(chain, created_at, f"dataset.jsonl line {d.line}")
    run_chain = _run_events(run_events or [], checkpoint)
    if torn:  # what the import skipped, first on _run (the checkpoint stays last)
        mark = _fields(RUN_DOC, "import", "handoff", ts=created, payload={"floor": "import", "skipped_torn_lines": torn})
        run_chain = [mark, *run_chain]
    if run_chain:
        chains[RUN_DOC] = _fill_times(run_chain, created_at, "events.jsonl")

    hashes = {d: _hashes(run.run_id, d, chain) for d, chain in chains.items()}
    return _Plan(run, chains, hashes, blob_map, sorted(set(absent), key=absent.index), warnings, len(docs))


def _documents(dataset: list[tuple[int, dict[str, Any]]], absent: list[str], put: Any) -> list[_Doc]:
    docs: list[_Doc] = []
    seen_source: set[str] = set()
    seen_doc: dict[str, int] = {}
    derived = False
    for line, row in dataset:
        source = row.get("id")
        if not isinstance(source, str) or not source:
            raise SandboxFolderInvalid(f"dataset.jsonl line {line}: no document id")
        if source in seen_source:
            raise SandboxFolderInvalid(f"dataset.jsonl line {line}: document {shown(source)} is listed twice")
        text = row.get("doc_text")
        sha = row.get("content_sha256")
        if not (isinstance(sha, str) and _SHA_RE.fullmatch(sha)):
            if not isinstance(text, str):
                raise SandboxFolderInvalid(f"dataset.jsonl line {line}: neither content_sha256 nor doc_text")
            sha, derived = hashlib.sha256(_utf8(text, f"dataset.jsonl line {line}: doc_text")).hexdigest(), True
        if sha in seen_doc:
            raise SandboxFolderInvalid(
                f"dataset.jsonl line {line}: document {shown(source)} has the content of line {seen_doc[sha]}"
            )
        seen_source.add(source)
        seen_doc[sha] = line
        doc = _Doc(source, sha, row, line)
        if isinstance(text, str):
            doc.original = put(_utf8(text, f"dataset.jsonl line {line}: doc_text"), "original")
        elif "doc_text" not in absent:
            absent.append("doc_text")
        docs.append(doc)
    if derived:
        absent.append("content_sha256 (doc id is the SHA-256 of doc_text)")
    return docs


def _node_events(rows: list[tuple[int, dict[str, Any]]], doc_for: Any) -> None:
    open_rows: dict[tuple[str, str], list[tuple[int, int, dict[str, Any]]]] = defaultdict(list)
    visits: dict[tuple[str, str], int] = defaultdict(int)
    for order, (line, row) in enumerate(rows):
        where = f"nodes.jsonl line {line}"
        doc = doc_for(row.get("doc"), where)
        node = row.get("node")
        if not isinstance(node, str) or not node:
            raise SandboxFolderInvalid(f"{where}: no node name")
        status = row.get("status")
        status = status if isinstance(status, str) else ""
        key = (doc.source_id, node)
        if status == "running":
            open_rows[key].append((order, line, row))
            continue
        start = _pair(open_rows[key], row.get("step"))
        attempt = visits[key]
        visits[key] += 1
        decisions = row.get("decisions")
        decisions = [scrub(_strip_options(d)) for d in decisions] if isinstance(decisions, list) else []
        if node == _REVIEW_NODE:
            kind = "review"
        elif status in _ERROR_STATUSES:
            kind = "error"
        elif decisions:
            kind = "decision"
        else:
            kind = "handoff"
        # The row's own status (None when it has none, as the sandbox's replay shows it).
        payload: dict[str, Any] = {"floor": "node", "status": scrub(row.get("status")), "order": order}
        step = row.get("step") if row.get("step") is not None else (start[2].get("step") if start else None)
        _put_if(payload, "step", scrub(step))
        if row.get("step") is not None:  # the finish named its step: the replay pairs by it, as the sandbox does
            payload["finish_step"] = True
        _put_if(payload, "stage", scrub(row.get("stage")))
        if start is not None:
            payload["started_order"] = start[0]
            started = epoch(start[2].get("ts"))
            started_ts = iso(started) if started is not None else None
            if started_ts is not None:
                payload["started_ts"] = started_ts
            else:
                payload["started_ts_absent"] = True
        fields = _fields(
            doc.doc_id, node, kind, ts=epoch(row.get("ts")), agent=row.get("agent"), model=row.get("model"),
            attempt=attempt, latency_ms=_ms(row.get("ms")), decision=decisions or None,
            cause=cause_token(row.get("error")) if row.get("error") or kind == "error" else None,
            payload=payload, where=where,
        )
        doc.events.append((order, fields))
    for (source, node), pending in open_rows.items():
        for order, line, row in pending:  # in flight when the run stopped
            doc = doc_for(source, f"nodes.jsonl line {line}")
            payload = {"floor": "node", "status": "running", "order": order}
            _put_if(payload, "step", scrub(row.get("step")))
            _put_if(payload, "stage", scrub(row.get("stage")))
            fields = _fields(
                doc.doc_id, node, "handoff", ts=epoch(row.get("ts")), agent=row.get("agent"), model=row.get("model"),
                attempt=visits[(source, node)], payload=payload, where=f"nodes.jsonl line {line}",
            )
            doc.events.append((order, fields))


def _pair(pending: list[tuple[int, int, dict[str, Any]]], step: Any) -> tuple[int, int, dict[str, Any]] | None:
    """Take the first open ``running`` row (with the same step when the finish names one)."""
    for i, entry in enumerate(pending):
        if step is None or entry[2].get("step") == step:
            return pending.pop(i)
    return None


def _strip_options(decision: Any) -> Any:
    if isinstance(decision, dict):
        return {k: v for k, v in decision.items() if k != "options"}
    return decision


def _item_events(rows: list[tuple[int, dict[str, Any]]], offset: int, doc_for: Any, put: Any) -> None:
    for index, (line, row) in enumerate(rows):
        where = f"items.jsonl line {line}"
        doc = doc_for(row.get("item_id") or row.get("id"), where)
        pred = row.get("pred") if isinstance(row.get("pred"), dict) else {}
        ok = row.get("ok")
        failed = ok is False or (ok is None and bool(row.get("error")))
        outputs: list[BlobRef] = []
        extracted = pred.get("extracted_data")
        if isinstance(extracted, dict):
            body = {k: v for k, v in extracted.items() if k != "_report"}
            outputs.append(put(_json_bytes(body, where), "extraction"))
            report = extracted.get("_report")
            if report is not None:
                data = _utf8(report, f"{where}: _report") if isinstance(report, str) else _json_bytes(report, where)
                outputs.append(put(data, "report"))
        payload: dict[str, Any] = {"floor": "item", "order": offset + index, "ok": not failed}
        for key, value in (
            ("usage_by_agent", row.get("usage_by_agent")),
            ("legacy_score", row.get("score")),
            ("prompt_tokens", row.get("prompt_tokens")),
            ("completion_tokens", row.get("completion_tokens")),
            *((k, pred.get(k)) for k in _PRED_EXTRAS),
        ):
            _put_if(payload, key, scrub(value))
        fields = _fields(
            doc.doc_id, "item", "error" if failed else "output", ts=epoch(row.get("ts")),
            latency_ms=_ms(row.get("latency_ms")),
            decision={k: scrub(pred.get(k)) for k in _OUTCOME_KEYS},
            cause=cause_token(row.get("error")) if row.get("error") else None,
            inputs=[doc.original] if doc.original else [], outputs=outputs, payload=payload, where=where,
        )
        doc.events.append((offset + index, fields))


def _run_events(rows: list[tuple[int, dict[str, Any]]], checkpoint: dict[str, Any] | None) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    for line, row in rows:
        name = token(row.get("event")) or "event"
        payload: dict[str, Any] = {"floor": "lifecycle"}
        _put_if(payload, "level", scrub(row.get("level")))
        _put_if(payload, "detail", scrub(row.get("detail")))
        chain.append(
            _fields(RUN_DOC, name, "handoff", ts=epoch(row.get("ts")), payload=payload, where=f"events.jsonl line {line}")
        )
    if checkpoint is not None:
        payload = {"floor": "checkpoint"}
        for key in ("state", "cursor", "total"):
            payload[key] = scrub(checkpoint.get(key))
        chain.append(
            _fields(RUN_DOC, "checkpoint", "handoff", ts=epoch(checkpoint.get("updated_at")), payload=payload,
                    where="checkpoint.json")
        )
    return chain


def _run_header(
    folder: Path,
    lock: dict[str, Any],
    prompt_lock: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    node_rows: list[tuple[int, dict[str, Any]]],
    docs: list[_Doc],
    created_at: str,
    absent: list[str],
) -> Run:
    run_id = lock.get("run_id") if isinstance(lock.get("run_id"), str) else None
    if run_id is None or not _RUN_ID_RE.fullmatch(run_id):
        run_id = folder.name
        if not _RUN_ID_RE.fullmatch(run_id):
            raise SandboxFolderInvalid("neither spec.lock.json's run_id nor the folder name is a usable run id")
        absent.append("run_id (the folder name is used)")
    engine = lock.get("engine")
    kind_ = engine.get("kind") if isinstance(engine, dict) else engine
    backend = named(kind_)
    if backend is None:
        absent.append("backend")
    models = lock.get("agent_models")
    agent_models: dict[str, str] = {}
    for name, model in models.items() if isinstance(models, dict) else ():
        key, value = named(name), named(model)
        if key is not None and value is not None:
            agent_models[key] = value
    if not agent_models:
        absent.append("agent_models")
    agents = (prompt_lock or {}).get("agents")
    prompt_hashes: dict[str, str] = {}
    if isinstance(agents, dict):
        for name, entry in agents.items():
            sha = entry.get("sha256") if isinstance(entry, dict) else None
            key = named(name)
            if key is not None and isinstance(sha, str) and _SHA_RE.fullmatch(sha):
                prompt_hashes[key] = sha
    if not prompt_hashes:
        absent.append("prompt_hashes")
    prompt = lock.get("prompt")
    prompt_set = prompt.get("default") if isinstance(prompt, dict) else prompt
    prompt_set = named(prompt_set)
    if prompt_set is None:
        absent.append("prompt_set")
    git = lock.get("git")
    commit = git.get("commit") if isinstance(git, dict) else None
    code_version = None
    if isinstance(commit, str) and commit:
        code_version = named(commit + ("-dirty" if isinstance(git, dict) and git.get("dirty") is True else ""))
    else:
        absent.append("code_version")
    initials = (receipt or {}).get("initials")
    signed_by = named(initials)
    if signed_by is None:
        absent.append("signed_by")
    source = {"folder": named(folder.name) or ""}
    for key, value in (
        ("spec_hash", lock.get("spec_hash")),
        ("source_run_id", lock.get("run_id")),
        ("engine_model", engine.get("model") if isinstance(engine, dict) else None),
        ("profile", lock.get("profile")),
    ):
        stored = named(value)
        if stored is not None:
            source[key] = stored
        elif key == "spec_hash":
            absent.append("spec_hash")
    kind = "full_pipeline" if any(r.get("node") == _CLASSIFY_NODE for _, r in node_rows) else "specialist_only"
    try:
        return Run(
            run_id=run_id, kind=kind, backend=backend or "unknown", origin="imported_sandbox",
            policy_name="legacy", policy_hash=None, prompt_set=prompt_set, prompt_hashes=prompt_hashes,
            agent_models=agent_models, code_version=code_version, label_snapshot_ids=[],
            document_ids=[d.doc_id for d in docs], caps={}, signed_by=signed_by, created_at=created_at, source=source,
        )
    except ValueError as exc:
        raise SandboxFolderInvalid(f"spec.lock.json: not a usable run header ({validation_detail(exc)})") from None


# --- event fields ---------------------------------------------------------------------


def _fields(
    doc_id: str,
    node: str,
    kind: str,
    *,
    ts: float | None,
    agent: Any = None,
    model: Any = None,
    attempt: int = 0,
    latency_ms: float | None = None,
    decision: Any = None,
    cause: str | None = None,
    inputs: list[BlobRef] | None = None,
    outputs: list[BlobRef] | None = None,
    payload: dict[str, Any] | None = None,
    where: str = "",
) -> dict[str, Any]:
    """The keyword arguments of one ``LogWriter.append`` call (``ts`` still in epoch seconds)."""
    return {
        "doc_id": doc_id,
        "node": named(node) or node,
        "kind": kind,
        "agent": named(agent),
        "attempt": attempt,
        "model": named(model),
        "prompt_id": None,
        "tokens_in": None,
        "tokens_out": None,
        "tokens_cached": None,
        "price_basis": None,
        "latency_ms": latency_ms,
        "decision": decision,
        "cause": cause,
        "inputs": list(inputs or []),
        "outputs": list(outputs or []),
        "payload": dict(payload or {}),
        "ts": ts,
        "_where": where,
    }


def _fill_times(chain: list[dict[str, Any]], fallback: str, where: str) -> list[dict[str, Any]]:
    """Turn epoch times into record timestamps; a missing one takes the previous event's."""
    last = fallback
    out = []
    for f in chain:
        ts = iso(f["ts"]) if f["ts"] is not None else None
        if ts is None:
            f = f | {"payload": f["payload"] | {"ts_absent": True}}
            ts = last
        last = ts
        out.append(f | {"ts": ts})
    return out


def _hashes(run_id: str, doc_id: str, chain: list[dict[str, Any]]) -> list[str]:
    """Each event's hash, and the chain's arguments made ready for ``append`` (in place)."""
    out: list[str] = []
    prev = GENESIS
    for seq, f in enumerate(chain):
        where = f.pop("_where", "") or f"document {shown(doc_id)}"
        try:
            event = Event.build(**f, run_id=run_id, seq=seq, prev_hash=prev)
        except ValueError as exc:  # a pydantic error's text quotes the input: name fields only
            raise SandboxFolderInvalid(f"{where}: not a valid event ({validation_detail(exc) or 'invalid'})") from None
        out.append(event.hash)
        prev = event.hash
    return out


def _ms(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if v >= 0 and v == v and v != float("inf") else None


def _put_if(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


def _utf8(text: str, what: str) -> bytes:
    """``text`` as UTF-8; a lone surrogate (which JSON can carry) is a folder error naming ``what``."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise SandboxFolderInvalid(f"{what} is not valid UTF-8 (a lone surrogate)") from None


def _json_bytes(value: Any, where: str) -> bytes:
    try:
        return canonical_json(value)
    except (TypeError, ValueError, RecursionError):
        raise SandboxFolderInvalid(f"{where}: extracted data is not JSON-safe") from None
