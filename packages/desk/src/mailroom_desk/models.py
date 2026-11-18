"""The desk's data model: requests, reviewer references, review action payloads, case state.

Every model is a frozen pydantic v2 model with strict validation and unknown keys refused,
like ``mailroom_record``'s: a stored payload either validates as written or fails.
Collections are tuples; a list given by a caller is turned into a tuple before strict
validation, nothing else is coerced.

Domain-free (R4): nothing here names a document class, a field or a pipeline node. A class
is an opaque string a caller passes as a hint, and a cause is a token from the policy's
catalog (``config/review/<name>.yaml``).

Tokens
------
A token (cause, rule name, queue, role, reason) is lowercase ``:``-separated segments of
``[a-z0-9_]``, for example ``sorter_low_confidence`` or ``decision_point:slot_a:judge_miss``
(the plan's ``[a-z0-9_:]+``, without empty segments or a leading or trailing ``:``). A
catalog entry may also be a *pattern* whose segments may be ``*`` (one whole segment), see
``policy.py``.

References
----------
A reference (``requested_by``, ``class_hint``, ``prefill_ref``) names something outside
the desk: a node or service, a class of the client profile, a prefill. It is ASCII letters,
digits and ``_ . : - /``, starting with a letter or digit, at most 128 characters: no
whitespace, no control, format or bidi character, nothing outside ASCII. A payload is
never erasable (ADR-0007), so a field that could carry free text would carry it forever;
a reference cannot.

Review actions
--------------
Every desk action is one ``kind="review"`` event whose payload validates as exactly one
of the payload models below, chosen by its ``action``. An episode (the review events of a
document since its last non-review event) is opened by ``enqueue`` or ``reopen``; both
carry the full routing (causes, rule, priority, SLA, queue, policy hash). ``assign``,
``claim``, ``release``, ``escalate`` and ``defer`` change the case and keep it in the queue.
A resolution is the document's next non-review event (Task D2), so it is not a payload here.
Free text never goes in a payload: notes and corrections are ``human_correction`` blobs.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
)

from mailroom_record.models import BlobRef, Event

__all__ = [
    "ACTIONS",
    "CASE_STATUSES",
    "PRIORITIES",
    "AssignPayload",
    "CaseState",
    "ClaimPayload",
    "DeferPayload",
    "EnqueuePayload",
    "EscalatePayload",
    "ReleasePayload",
    "ReopenPayload",
    "ReviewPayload",
    "ReviewRequest",
    "ReviewerRef",
    "Routing",
    "format_ts",
    "is_ref",
    "is_token",
    "parse_payload",
    "parse_ts",
    "priority_rank",
]

#: The last representable moment; an SLA due time past it saturates here (``due``).
END_OF_TIME = datetime.max.replace(tzinfo=timezone.utc)

#: The node every desk event is recorded under.
NODE = "human_review"

ACTIONS: tuple[str, ...] = ("enqueue", "assign", "claim", "release", "escalate", "defer", "reopen")
#: Actions that open an episode (they carry the routing).
OPENING_ACTIONS: frozenset[str] = frozenset({"enqueue", "reopen"})

Priority = Literal["high", "normal", "low"]
PRIORITIES: tuple[str, ...] = ("high", "normal", "low")  # most urgent first

CaseStatus = Literal["open", "assigned", "claimed", "deferred", "resolved"]
CASE_STATUSES: tuple[str, ...] = ("open", "assigned", "claimed", "deferred", "resolved")

TOKEN_RE = re.compile(r"[a-z0-9_]+(?::[a-z0-9_]+)*")
REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_REVIEWER_ID_RE = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
_RUN_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"  # the record's run id rule
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")
_MAX_TOKEN = 200


def is_token(value: object) -> bool:
    """True when ``value`` is a token (see the module doc)."""
    return isinstance(value, str) and len(value) <= _MAX_TOKEN and TOKEN_RE.fullmatch(value) is not None


def _check_token(value: str) -> str:
    if not is_token(value):
        raise ValueError(f"not a token ([a-z0-9_] segments joined by ':'): {value!r}")
    return value


def is_ref(value: object) -> bool:
    """True when ``value`` is a reference (see the module doc)."""
    return isinstance(value, str) and REF_RE.fullmatch(value) is not None


def _check_ref(value: str) -> str:
    if not is_ref(value):
        raise ValueError("not a reference (ASCII letters, digits and _ . : - /, at most 128 characters)")
    return value


def _as_tuple(value: Any) -> Any:
    return tuple(value) if isinstance(value, list) else value


def _unique(value: tuple[Any, ...]) -> tuple[Any, ...]:
    if len(set(value)) != len(value):
        raise ValueError("lists an entry twice")
    return value


def parse_ts(value: str) -> datetime:
    """Parse a record timestamp (UTC ISO-8601 ending in ``Z``) as an aware datetime."""
    if not isinstance(value, str) or not _TS_RE.fullmatch(value):
        raise ValueError(f"timestamp must be UTC ISO-8601 ending in Z, got {value!r}")
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ"
    return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)


def format_ts(moment: datetime) -> str:
    """Format an aware datetime the way the record stores timestamps (microseconds, ``Z``;
    the year always has four digits, so year 5 is ``0005``)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("a desk time must be timezone-aware")
    m = moment.astimezone(timezone.utc)
    return f"{m.year:04d}-{m.month:02d}-{m.day:02d}T{m.hour:02d}:{m.minute:02d}:{m.second:02d}.{m.microsecond:06d}Z"


def _check_ts(value: str) -> str:
    parse_ts(value)
    return value


def priority_rank(priority: str | None) -> int:
    """Sort rank of a priority: ``high`` 0, ``normal`` 1, ``low`` 2; unknown (``None``) -1.

    A case the desk cannot read the priority of sorts first, so a person looks at it.
    """
    return -1 if priority is None else PRIORITIES.index(priority)


Token = Annotated[str, AfterValidator(_check_token)]
Ref = Annotated[str, AfterValidator(_check_ref)]
ReviewerId = Annotated[str, StringConstraints(pattern=_REVIEWER_ID_RE)]
RunId = Annotated[str, StringConstraints(pattern=_RUN_ID_RE)]
DocId = Annotated[str, StringConstraints(min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
TsStr = Annotated[str, AfterValidator(_check_ts)]
Hours = Annotated[float, Field(gt=0, le=24 * 366, allow_inf_nan=False)]
Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Tokens = Annotated[tuple[Token, ...], BeforeValidator(_as_tuple), AfterValidator(_unique)]
Blobs = Annotated[tuple[BlobRef, ...], BeforeValidator(_as_tuple)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ReviewerRef(_Frozen):
    """Who acted: a reviewer id, the role they acted in, and how their identity is known.

    ``authenticated``: the reviewer proved a registered token. ``asserted``: the id was
    stated, not proved (open registry mode, or a known reviewer acting without a token).
    """

    id: ReviewerId
    role: Token
    identity: Literal["authenticated", "asserted"]


class ReviewRequest(_Frozen):
    """A request to put one document of one run in front of a person.

    ``causes`` are catalog tokens, in the caller's order (the first is the event's
    ``cause``). An empty ``causes`` asks only for the spot-check sample: the document enters
    review only if the policy's sample selects it. ``class_hint`` and ``confidence_hint``
    are plain data the rules may test (``class_in``, ``confidence_below``); the desk never
    interprets a class. ``shown`` are blobs already in the store that the reviewer is shown
    (recorded as the event's ``inputs``).
    """

    run_id: RunId
    doc_id: DocId
    causes: Tokens = ()
    requested_by: Ref
    class_hint: Ref | None = None
    confidence_hint: Confidence | None = None
    shown: Blobs = ()


# --- payloads of review events ------------------------------------------------------


class Routing(_Frozen):
    """What the policy decided when an episode opened (``enqueue`` or ``reopen``)."""

    causes: Annotated[Tokens, Field(min_length=1)]
    rule: Token
    priority: Priority
    sla_hours: Hours
    queue: Token
    required_role: Token | None = None
    prefill_ref: Ref | None = None
    sampled: bool
    policy_name: Token
    policy_hash: Sha256


class EnqueuePayload(Routing):
    """``enqueue``: the document enters review (opens an episode)."""

    action: Literal["enqueue"]
    requested_by: Ref
    class_hint: Ref | None = None
    confidence_hint: Confidence | None = None


class ReopenPayload(Routing):
    """``reopen``: a person puts the document back in review with fresh routing.

    It opens a new episode after a resolution, or resets an open case (routing replaced,
    assignment, claim and deferral cleared).
    """

    action: Literal["reopen"]
    actor: ReviewerRef
    reason: Token | None = None


class AssignPayload(_Frozen):
    """``assign``: the case is given to ``assignee`` (any live claim is dropped)."""

    action: Literal["assign"]
    actor: ReviewerRef
    assignee: ReviewerId


class ClaimPayload(_Frozen):
    """``claim``: ``actor`` works on the case until ``lease_expires_at`` (or a release)."""

    action: Literal["claim"]
    actor: ReviewerRef
    lease_expires_at: TsStr


class ReleasePayload(_Frozen):
    """``release``: the claim and the assignment are dropped; the case is open again."""

    action: Literal["release"]
    actor: ReviewerRef
    reason: Token | None = None


class EscalatePayload(_Frozen):
    """``escalate``: new priority (and optionally SLA, queue, role); assignment and claim drop.

    A new ``sla_hours`` counts from the escalation's own time.
    """

    action: Literal["escalate"]
    actor: ReviewerRef
    priority: Priority
    sla_hours: Hours | None = None
    queue: Token | None = None
    required_role: Token | None = None
    reason: Token | None = None


class DeferPayload(_Frozen):
    """``defer``: the case is set aside until ``until``; a live claim is dropped."""

    action: Literal["defer"]
    actor: ReviewerRef
    until: TsStr
    reason: Token | None = None


ReviewPayload = Annotated[
    Union[EnqueuePayload, ReopenPayload, AssignPayload, ClaimPayload, ReleasePayload, EscalatePayload, DeferPayload],
    Field(discriminator="action"),
]
_PAYLOAD = TypeAdapter(ReviewPayload)


def parse_payload(payload: Any) -> Any:
    """Validate a review event's payload as one of the action payload models.

    Raises ``pydantic.ValidationError`` when it is not one (an unknown action, a missing
    or unknown key, a bad value).
    """
    return _PAYLOAD.validate_python(payload, strict=True)


# --- the fold result ----------------------------------------------------------------


class CaseState(BaseModel):
    """A document's review case as folded from its events at the moment ``as_of``.

    ``status`` is ``resolved`` when the document's head event is not a ``review`` event
    (the case then shows its last episode). Otherwise the case is in the queue and its
    status is, in this order: ``deferred`` while a deferral holds, ``claimed`` while a
    claim's lease holds, ``assigned`` with an assignee, else ``open``. A lapsed lease or
    deferral is not shown (``claimed_by``, ``lease_expires_at`` and ``deferred_until`` are
    ``None``). ``sla_due`` is the opening (or escalation) time plus ``sla_hours``.

    ``anomalies`` lists review events of the episode the desk could not read as one of
    its payloads (by ``seq`` and reason, never content); such a case is still in the queue
    (the index's rule is the kind of the head event, nothing else), with whatever routing
    could be read (``None`` where none could).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True)

    run_id: str
    doc_id: str
    status: CaseStatus
    as_of: datetime
    causes: tuple[str, ...]
    rule: str | None
    policy_name: str | None
    policy_hash: str | None
    priority: Priority | None
    sla_hours: float | None
    sla_due: datetime | None
    queue: str | None
    required_role: str | None
    prefill_ref: str | None
    sampled: bool
    requested_by: str | None
    enqueued_at: datetime | None
    assignee: str | None
    claimed_by: str | None
    lease_expires_at: datetime | None
    deferred_until: datetime | None
    opened_seq: int
    head_seq: int
    resolved_seq: int | None
    events: tuple[Event, ...]
    anomalies: tuple[str, ...]

    @property
    def in_review(self) -> bool:
        return self.status != "resolved"

    def breached(self, now: datetime) -> bool:
        """True when the case is in review and ``now`` is past its ``sla_due``."""
        return self.in_review and self.sla_due is not None and now > self.sla_due

    def sort_key(self) -> tuple[Any, ...]:
        """Queue order: priority (unknown first, then high, normal, low), earliest SLA due,
        earliest opened, then run and document id (a total order)."""
        far = END_OF_TIME
        return (
            priority_rank(self.priority),
            self.sla_due or far,
            self.enqueued_at or far,
            self.run_id,
            self.doc_id,
        )


def due(start: datetime, hours: float) -> datetime:
    """``start`` plus ``hours`` (finite, positive), saturating at ``END_OF_TIME``: a start
    near 9999-12-31 never raises ``OverflowError`` (the fold lists such a case as an
    anomaly)."""
    if not math.isfinite(hours):
        raise ValueError("hours must be finite")
    try:
        return start + timedelta(hours=hours)
    except OverflowError:
        return END_OF_TIME
