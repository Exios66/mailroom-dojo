"""The desk over one record root: enqueue documents for review and read the queue.

``Desk(root)`` opens the record at ``root`` (``runs/`` and ``blobs/``, as
``mailroom_record`` lays them out) and, optionally, its derived ``Index``, which it
applies its events to and never reads to answer. It keeps no state of its own: every
answer is folded from the log (``fold.py``), and every action is
one ``kind="review"`` event appended under the run's desk lock (``lock.py``).

Enqueue
-------
``enqueue(request, policy)``:

1. Refuses the run-level chain ``_run`` (``RunChainNotReviewable``: it holds run events,
   not a document), an outage cause (``OutageNotReviewable``) and a cause the policy's
   catalog does not list (``UnknownCause``), an unknown run (``UnknownRun``), a closed run
   (``RunClosed``), a pruned run (``RunArchived``) and a shown blob that is not in the store
   (``BlobNotPresent``), before touching the record.
2. Decides the sample: ``policy.sample.selects(doc_id)``. A request with no causes enters
   only if sampled, with the cause ``spot_check``; otherwise ``enqueue`` returns ``None``
   and appends nothing. A request with causes enters with them, and ``sampled`` records
   whether the document is also in the sample (membership depends on the document only,
   so the sample stays an unbiased draw).
3. Routes the causes through the rules (first match wins).
4. Under the desk lock, and for an experiment run also under the record's seal lock (so
   ``seal_run`` cannot seal between the check and the append): checks again that the run
   exists and is not closed (an experiment run with a seal file, the rule ``verify``
   uses), re-reads the document's events and refuses if its head is already a ``review``
   event (``AlreadyInReview``), then opens the writer ``desk``, appends one event (node
   ``human_review``, kind ``review``, ``cause`` = the first cause, ``inputs`` = the blobs
   shown, the ``EnqueuePayload``), and closes the writer (flush and fsync) before the
   locks are released. With an index, the event is applied to it under the lock too,
   after the flush (write order: blob, event, index).

A record the desk cannot read safely (a corrupt line, a forked chain, an unreadable
header) raises ``RecordDamaged`` and nothing is appended. A torn tail is not damage: it is
skipped (it was never an event) and the writer continues in a new segment. Another writer
of this process holding the document raises ``RecordBusy``. A write or fsync that fails
(a full disk) raises ``RecordWriteFailed`` with the ``OSError`` as its cause: the record
may then hold the event (or a torn line), so read the case again before retrying. Once the
flush has succeeded ``enqueue`` returns the event and never raises: an index that refuses
the apply does not undo the durable event (the result's ``index_error`` says why, and a
``reindex`` repairs the index), and the case is folded from events already validated, with
an SLA due time past 9999-12-31 saturated, not overflowing.

The closed-run rule and the seal lock are read from the record's documented layout
(``runs/<run_id>/seals/<n>.json`` and ``seals/.lock``): the record has no public "is this
run closed" call or closing lock yet (plan follow-up 2). A test pins both.

Reading
-------
The log is the truth, and reading never needs the index. ``case(run_id, doc_id)`` folds
one document from its events in the log. ``queue(run_id=None)`` folds every document in
review, of one run or of all, ordered by ``CaseState.sort_key`` (priority, SLA due,
opened, ids): one ordered scan of each run's log, grouped by document and folded per
document, so the work grows with the run, never with the number of cases. A pruned run
(``mailroom_record.pack.prune_run``) keeps its header but its events are in its archive:
``queue()`` over every run skips it, and ``queue(run_id)``, ``case`` and ``enqueue``
naming it raise ``RunArchived`` (unpack it first). The index is only a target the desk applies its events to (for other readers of the record); a stale,
locked or broken index changes no answer and slows nothing down. ``index_drift()`` is a
diagnostic: it compares the index's document heads with the log's and returns the
problems it finds, never raising. Leases, deferrals and SLA breach are judged against
``now``, which defaults to the desk's clock (injectable).
"""

from __future__ import annotations

import contextlib
import itertools
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mailroom_desk.errors import (
    AlreadyInReview,
    BlobNotPresent,
    NoCase,
    OutageNotReviewable,
    RecordBusy,
    RecordDamaged,
    RecordWriteFailed,
    RunArchived,
    RunChainNotReviewable,
    RunClosed,
    UnknownCause,
    UnknownRun,
)
from mailroom_desk.fold import fold_case
from mailroom_desk.lock import DEFAULT_LOCK_TIMEOUT, check_timeout, desk_lock, seal_guard
from mailroom_desk.models import NODE, RUN_CHAIN, CaseState, EnqueuePayload, ReviewRequest, format_ts
from mailroom_desk.policy import SPOT_CHECK, Policy, is_outage
from mailroom_record.blobs import BlobStore
from mailroom_record.index import Index
from mailroom_record.log import (
    BlobNotStored,
    CorruptLine,
    DocumentInUse,
    EventLog,
    ForkedChain,
    RunPruned,
    WriterInUse,
    pruned_archive,
)
from mailroom_record.models import BlobRef, Event, Run

__all__ = ["WRITER_ID", "Desk", "Enqueued"]

#: The writer id of every desk append (one segment family, ``segments/desk*.jsonl``).
WRITER_ID = "desk"

_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_SEAL_NAME_RE = re.compile(r"(0|[1-9][0-9]*)\.json")


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Enqueued:
    """What ``enqueue`` did: the stored event, the case right after it, and why the index
    refused the event if it did (``None`` when there is no index or the apply succeeded)."""

    event: Event
    case: CaseState
    index_error: str | None = None


class Desk:
    """The review desk over the record at ``root`` (see the module doc)."""

    def __init__(
        self,
        root: Path | str,
        *,
        index: Index | None = None,
        clock: Callable[[], datetime] | None = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self.root = Path(root)
        self.blobs = BlobStore(self.root)
        self.log = EventLog(self.root, self.blobs)
        self.index = index
        self._clock = clock or _utc_now
        self.lock_timeout = check_timeout(lock_timeout)

    # --- time -----------------------------------------------------------------------

    def now(self) -> datetime:
        """The desk clock's current time (timezone-aware)."""
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("the desk clock must return a timezone-aware datetime")
        return moment

    # --- runs -----------------------------------------------------------------------

    def run(self, run_id: str) -> Run:
        """The run header. Raises ``UnknownRun`` or ``RecordDamaged``."""
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise UnknownRun(run_id)
        try:
            return self.log.read_run(run_id)
        except FileNotFoundError:
            raise UnknownRun(run_id) from None
        except (OSError, ValueError, TypeError) as exc:
            raise RecordDamaged(run_id, exc) from exc

    def is_closed(self, run_id: str) -> bool:
        """True for a sealed experiment run: ``kind`` is not ``service_period`` and at least
        one seal file exists (the rule ``mailroom_record.verify`` uses)."""
        run = self.run(run_id)
        if run.kind == "service_period":
            return False
        seals = self.log.run_dir(run_id) / "seals"
        try:
            return seals.is_dir() and any(_SEAL_NAME_RE.fullmatch(p.name) for p in seals.iterdir())
        except OSError as exc:
            raise RecordDamaged(run_id, exc) from exc

    def run_ids(self) -> list[str]:
        """Every run under the root that has a header, sorted."""
        runs = self.root / "runs"
        if not runs.is_dir():
            return []
        return sorted(p.name for p in runs.iterdir() if (p / "run.json").is_file())

    # --- reading events -------------------------------------------------------------

    def _events(self, run_id: str, doc_id: str) -> list[Event]:
        """One document's events, from the log (one scan of the run)."""
        try:
            return list(self.log.iter_events(run_id, doc_id))
        except RunPruned:
            raise RunArchived(run_id, pruned_archive(self.log, run_id)) from None
        except (CorruptLine, ValueError, TypeError, OSError) as exc:
            raise RecordDamaged(run_id, exc) from exc

    # --- enqueue --------------------------------------------------------------------

    def enqueue(self, request: ReviewRequest, policy: Policy) -> Enqueued | None:
        """Put ``request``'s document in review under ``policy`` (see the module doc).

        Returns ``None`` (and appends nothing) only for a request without causes whose
        document the sample does not select.
        """
        if not isinstance(request, ReviewRequest):
            raise TypeError(f"request must be a ReviewRequest, got {type(request).__name__}")
        if not isinstance(policy, Policy):
            raise TypeError(f"policy must be a Policy, got {type(policy).__name__}")
        if request.doc_id == RUN_CHAIN:
            raise RunChainNotReviewable(request.run_id, request.doc_id)
        for cause in request.causes:
            if is_outage(cause):
                raise OutageNotReviewable(cause)
        for cause in request.causes:
            if policy.cause_spec(cause) is None:
                raise UnknownCause(cause, policy.name)
        run_id, doc_id = request.run_id, request.doc_id
        # Refused whether or not the sample selects the document.
        self._check_open(run_id)
        self._check_shown(request.shown)
        sampled = policy.sample.selects(doc_id)
        causes: tuple[str, ...] = request.causes or ((SPOT_CHECK,) if sampled else ())
        if not causes:
            return None
        route = policy.route(causes, request.class_hint, request.confidence_hint)
        payload = EnqueuePayload(
            action="enqueue",
            causes=causes,
            rule=route.rule,
            priority=route.priority,
            sla_hours=route.sla_hours,
            queue=route.queue,
            required_role=route.required_role,
            prefill_ref=route.prefill_ref,
            sampled=sampled,
            policy_name=policy.name,
            policy_hash=policy.hash,
            requested_by=request.requested_by,
            class_hint=request.class_hint,
            confidence_hint=request.confidence_hint,
        )
        with self._locked(run_id):
            self._check_open(run_id)
            events = self._events(run_id, doc_id)
            if events and events[-1].kind == "review":
                raise AlreadyInReview(run_id, doc_id, events[-1].seq)
            now = self.now()
            event = self._append(
                run_id,
                doc_id,
                cause=causes[0],
                payload=payload.model_dump(mode="json"),
                inputs=request.shown,
                ts=format_ts(now),
            )
            index_error = self._apply(event)
        case = fold_case(run_id, doc_id, [*events, event], now)
        if case is None:  # pragma: no cover - the event just appended is a review event
            raise RuntimeError("the fold lost the event just appended")
        return Enqueued(event=event, case=case, index_error=index_error)

    def _check_open(self, run_id: str) -> Run:
        """The run header, unless the run is unknown (``UnknownRun``), pruned (``RunArchived``)
        or closed (``RunClosed``)."""
        run = self.run(run_id)
        archive = pruned_archive(self.log, run_id)
        if archive is not None:
            raise RunArchived(run_id, archive)
        if self.is_closed(run_id):
            raise RunClosed(run_id, run.kind)
        return run

    def _check_shown(self, shown: Sequence[BlobRef]) -> None:
        for ref in shown:
            state = self.blobs.exists(ref.sha256)
            if state != "present":
                raise BlobNotPresent(ref.sha256, ref.role, state)

    @contextlib.contextmanager
    def _locked(self, run_id: str) -> Iterator[None]:
        """The desk lock of ``run_id``, plus the record's seal lock for an experiment run
        (a service-period run is never closed, so its timer seals are not held up). The
        run header is immutable, so its kind may be read before the locks."""
        kind = self.run(run_id).kind
        with contextlib.ExitStack() as stack:
            stack.enter_context(desk_lock(self.root, run_id, self.lock_timeout))
            if kind != "service_period":
                try:
                    stack.enter_context(seal_guard(self.root, run_id, self.lock_timeout))
                except OSError as exc:
                    raise RecordDamaged(run_id, exc) from exc
            yield

    def _append(
        self, run_id: str, doc_id: str, *, cause: str, payload: dict, inputs: Sequence, ts: str
    ) -> Event:
        """Open the desk writer, append one review event, flush and close (under the lock)."""
        try:
            writer = self.log.open_writer(run_id, WRITER_ID)
        except FileNotFoundError:
            raise UnknownRun(run_id) from None
        except WriterInUse as exc:
            raise RecordBusy(run_id, doc_id, exc) from exc
        except RunPruned:
            raise RunArchived(run_id, pruned_archive(self.log, run_id)) from None
        except (CorruptLine, OSError) as exc:
            raise RecordDamaged(run_id, exc) from exc
        try:
            try:  # buffers the event (nothing written yet) after reading the chain's head
                event = writer.append(
                    doc_id=doc_id,
                    node=NODE,
                    kind="review",
                    cause=cause,
                    inputs=inputs,
                    payload=payload,
                    ts=ts,
                )
            except (CorruptLine, ForkedChain, OSError) as exc:
                raise RecordDamaged(run_id, exc) from exc
            except DocumentInUse as exc:
                raise RecordBusy(run_id, doc_id, exc) from exc
            except BlobNotStored as exc:  # tombstoned between the check and the append
                ref = next((r for r in inputs if self.blobs.exists(r.sha256) != "present"), None)
                if ref is None:
                    raise
                raise BlobNotPresent(ref.sha256, ref.role, self.blobs.exists(ref.sha256)) from exc
            try:  # writes and fsyncs it
                writer.flush()
            except OSError as exc:
                raise RecordWriteFailed(run_id, doc_id, exc) from exc
        finally:
            with contextlib.suppress(OSError):  # after a good flush a close loses nothing
                writer.close()
        return event

    def _apply(self, event: Event) -> str | None:
        if self.index is None:
            return None
        try:
            self.index.apply(event)
        except Exception as exc:  # noqa: BLE001 - the event is durable; the index is derived
            return f"{type(exc).__name__}: {exc}"
        return None

    # --- reading the queue ----------------------------------------------------------

    def case(self, run_id: str, doc_id: str, *, now: datetime | None = None) -> CaseState:
        """The document's case (its last episode), from the log. Raises ``NoCase`` if it was
        never in review."""
        self.run(run_id)
        state = fold_case(run_id, doc_id, self._events(run_id, doc_id), now or self.now())
        if state is None:
            raise NoCase(run_id, doc_id)
        return state

    def queue(
        self,
        run_id: str | None = None,
        *,
        now: datetime | None = None,
        queue: str | None = None,
        statuses: Iterable[str] | None = None,
    ) -> list[CaseState]:
        """Every case in review (of ``run_id``, or of every run), in queue order, from the
        log: one scan per run.

        ``queue`` keeps one named queue; ``statuses`` keeps those statuses only. Over every
        run, a pruned run is skipped (its events are in its archive); named, it raises
        ``RunArchived``.
        """
        moment = now or self.now()
        if run_id is not None:
            self.run(run_id)
        cases: list[CaseState] = []
        if run_id is not None:
            cases.extend(self._cases(run_id, moment))
        else:
            for rid in self.run_ids():
                try:
                    cases.extend(self._cases(rid, moment))
                except RunArchived:
                    continue
        keep = None if statuses is None else set(statuses)
        out = [
            c
            for c in cases
            if c.in_review and (queue is None or c.queue == queue) and (keep is None or c.status in keep)
        ]
        return sorted(out, key=CaseState.sort_key)

    def _cases(self, run_id: str, now: datetime) -> list[CaseState]:
        """Every case of the run (in review or resolved), from one ordered scan of its log
        (``iter_events`` yields each document's events together, in ``seq`` order)."""
        out: list[CaseState] = []
        try:
            stream = self.log.iter_events(run_id)
            for doc_id, group in itertools.groupby(stream, key=lambda e: e.doc_id):
                state = fold_case(run_id, doc_id, list(group), now)
                if state is not None:
                    out.append(state)
        except RunPruned:
            raise RunArchived(run_id, pruned_archive(self.log, run_id)) from None
        except (CorruptLine, ValueError, TypeError, OSError) as exc:
            raise RecordDamaged(run_id, exc) from exc
        return out

    # --- index diagnostics ----------------------------------------------------------

    def index_drift(self, run_id: str | None = None) -> list[str]:
        """Problems with the index, as ``"<run_id>: <what>"`` lines, sorted; ``[]`` when its
        document heads equal the log's for ``run_id`` (or every run), or without an index.

        A diagnostic only: nothing the desk answers depends on it, and it never raises. It
        reports an unknown run, an index it cannot read (the error's type and message), a
        run whose document heads differ from the log's (how many documents) and a log it
        cannot read (the error's type). ``Index.reindex`` repairs the index.
        """
        if self.index is None:
            return []
        if run_id is not None:
            try:
                self.run(run_id)
            except UnknownRun:
                return [f"{_show_id(run_id)}: unknown run"]
            except RecordDamaged as exc:
                return [f"{run_id}: run header unreadable ({type(exc.cause).__name__})"]
            run_ids = [run_id]
        else:
            try:
                run_ids = self.run_ids()
            except OSError as exc:
                return [f"runs: unreadable ({type(exc).__name__})"]
        problems: list[str] = []
        for rid in run_ids:
            problem = self._drift(rid)
            if problem is not None:
                problems.append(f"{rid}: {problem}")
        return sorted(problems)

    def _drift(self, run_id: str) -> str | None:
        assert self.index is not None
        try:
            indexed = {r.doc_id: r.head_hash for r in self.index.documents(run_id)}
        except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never raises
            return f"index unavailable ({type(exc).__name__}: {exc})"
        try:
            heads = self.log.doc_heads(run_id)
        except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never raises
            return f"log unreadable ({type(exc).__name__})"
        differ = {d for d in indexed.keys() | heads.keys() if indexed.get(d) != heads.get(d)}
        if differ:
            return f"{len(differ)} document head(s) differ from the log"
        return None


def _show_id(value: object) -> str:
    """A run id for a problem line: the id itself, or its type and a short repr."""
    if isinstance(value, str) and _RUN_ID_RE.fullmatch(value):
        return value
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."
