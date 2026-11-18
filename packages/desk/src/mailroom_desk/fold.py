"""The queue fold: a document's review case derived from its events alone (ADR-0008).

The desk stores nothing beside the record. A document's *episode* is the run of
``kind="review"`` events that ends at its last review event and starts after the
non-review event before it (or at ``seq`` 0). If the last review event is the document's
head, the case is in review; otherwise the next event resolved it. This is exactly the
record index's rule (a document is in review when its head event is a ``review``), so the
fold and ``Index.review_queue()`` agree on every history; a property test checks it.

Within the episode each event's payload is read as a desk action (``models.parse_payload``)
and applied in ``seq`` order:

- ``enqueue`` opens the episode with its routing; ``reopen`` (re)sets the routing and
  clears assignment, claim and deferral. The opening time is the event's ``ts``; the SLA
  is due ``sla_hours`` after it.
- ``assign`` sets the assignee and drops a claim; ``claim`` records the claimant and the
  lease expiry and drops a deferral; ``release`` drops the claim and the assignee;
  ``escalate`` sets the priority (a new ``sla_hours`` counts from the escalation's ``ts``),
  queue and role when given, and drops the assignee and the claim; ``defer`` sets the
  deferral and drops the claim.

Leases and deferrals are judged at read time against the ``now`` given (the desk's
injectable clock): a lease holds while ``now < lease_expires_at``, a deferral while
``now < until``. A payload the desk cannot read, an ``enqueue`` inside an open episode, or
an action before any opening event is listed in ``anomalies`` (``seq`` and reason only,
never content) and the fold carries on: an unreadable case is never dropped from the queue.
An SLA due time past 9999-12-31 saturates at ``models.END_OF_TIME`` (never breached) and is
listed as an anomaly too, so one far-future event never raises out of the queue.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import ValidationError

from mailroom_desk.models import (
    END_OF_TIME,
    OPENING_ACTIONS,
    AssignPayload,
    CaseState,
    ClaimPayload,
    DeferPayload,
    EscalatePayload,
    ReleasePayload,
    ReopenPayload,
    Routing,
    due,
    parse_payload,
    parse_ts,
)
from mailroom_record.models import Event

__all__ = ["episode", "fold_case"]


def episode(events: Sequence[Event]) -> tuple[list[Event], Event | None] | None:
    """``(episode events, resolving event or None)`` of one document's ``seq``-ordered
    events, or ``None`` when it has no review event at all."""
    last = None
    for i in range(len(events) - 1, -1, -1):
        if events[i].kind == "review":
            last = i
            break
    if last is None:
        return None
    start = last
    while start > 0 and events[start - 1].kind == "review":
        start -= 1
    resolution = events[last + 1] if last + 1 < len(events) else None
    return list(events[start : last + 1]), resolution


def _why(exc: ValidationError) -> str:
    """Field locations and error types only: a payload may hold anything."""
    return "; ".join(".".join(str(x) for x in e.get("loc", ())) + ": " + str(e.get("type")) for e in exc.errors())


def _due(seq: int, at: datetime, hours: float, anomalies: list[str]) -> datetime:
    moment = due(at, hours)
    if moment == END_OF_TIME:
        anomalies.append(f"seq {seq}: SLA due time is past the last representable time; saturated")
    return moment


def fold_case(run_id: str, doc_id: str, events: Sequence[Event], now: datetime) -> CaseState | None:
    """Fold one document's events (in ``seq`` order) into its case as of ``now``.

    Returns ``None`` when the document has never been in review.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    found = episode(events)
    if found is None:
        return None
    eps, resolution = found

    routing: Routing | None = None
    requested_by: str | None = None
    priority = queue = required_role = None
    sla_hours: float | None = None
    sla_due: datetime | None = None
    opened_at: datetime | None = None
    assignee = claimed_by = None
    lease: datetime | None = None
    deferred: datetime | None = None
    anomalies: list[str] = []

    for e in eps:
        try:
            p = parse_payload(e.payload)
        except ValidationError as exc:
            anomalies.append(f"seq {e.seq}: not a desk review payload ({_why(exc)})")
            continue
        at = parse_ts(e.ts)
        if p.action in OPENING_ACTIONS:
            if p.action == "enqueue" and routing is not None:
                anomalies.append(f"seq {e.seq}: enqueue inside an open episode, ignored")
                continue
            routing = p
            requested_by = p.actor.id if isinstance(p, ReopenPayload) else p.requested_by
            priority, sla_hours, queue, required_role = p.priority, p.sla_hours, p.queue, p.required_role
            opened_at = at
            sla_due = _due(e.seq, at, p.sla_hours, anomalies)
            assignee = claimed_by = None
            lease = deferred = None
            continue
        if routing is None:
            anomalies.append(f"seq {e.seq}: {p.action} before the episode was opened")
        if isinstance(p, AssignPayload):
            assignee, claimed_by, lease = p.assignee, None, None
        elif isinstance(p, ClaimPayload):
            claimed_by, lease, deferred = p.actor.id, parse_ts(p.lease_expires_at), None
        elif isinstance(p, ReleasePayload):
            assignee, claimed_by, lease = None, None, None
        elif isinstance(p, EscalatePayload):
            priority = p.priority
            if p.sla_hours is not None:
                sla_hours, sla_due = p.sla_hours, _due(e.seq, at, p.sla_hours, anomalies)
            if p.queue is not None:
                queue = p.queue
            if p.required_role is not None:
                required_role = p.required_role
            assignee, claimed_by, lease = None, None, None
        elif isinstance(p, DeferPayload):
            deferred, claimed_by, lease = parse_ts(p.until), None, None

    if resolution is not None:
        status = "resolved"
    elif deferred is not None and now < deferred:
        status = "deferred"
    elif claimed_by is not None and lease is not None and now < lease:
        status = "claimed"
    elif assignee is not None:
        status = "assigned"
    else:
        status = "open"
    live_claim = status == "claimed"
    return CaseState(
        run_id=run_id,
        doc_id=doc_id,
        status=status,
        as_of=now,
        causes=() if routing is None else tuple(routing.causes),
        rule=None if routing is None else routing.rule,
        policy_name=None if routing is None else routing.policy_name,
        policy_hash=None if routing is None else routing.policy_hash,
        priority=priority,
        sla_hours=sla_hours,
        sla_due=sla_due,
        queue=queue,
        required_role=required_role,
        prefill_ref=None if routing is None else routing.prefill_ref,
        sampled=False if routing is None else routing.sampled,
        requested_by=requested_by,
        enqueued_at=opened_at,
        assignee=assignee,
        claimed_by=claimed_by if live_claim else None,
        lease_expires_at=lease if live_claim else None,
        deferred_until=deferred if status == "deferred" else None,
        opened_seq=eps[0].seq,
        head_seq=events[-1].seq,
        resolved_seq=None if resolution is None else resolution.seq,
        events=tuple(eps),
        anomalies=tuple(anomalies),
    )
