"""Typed errors raised by the desk.

Every refusal has its own exception, so a caller (the node, the CLI, the HTTP API) can say
exactly what happened, and a test can assert it. A refused action never appends: an error
raised here means the record did not change. Messages name runs, documents, tokens and
file positions only, never document text. The one exception is ``RecordWriteFailed``: a
write failed part way, so the record may have changed (its doc says how to find out).

A value the caller passed in is shown with ``repr`` when it is a string and by its type
alone otherwise, never with a ``repr`` or ``str`` that can raise (a huge integer, a hostile
``__repr__``), so building the error never raises something else in its place. The
attribute keeps the original value.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

__all__ = [
    "AlreadyInReview",
    "BlobNotPresent",
    "ConfigError",
    "DeskError",
    "DeskLockTimeout",
    "NoCase",
    "OutageNotReviewable",
    "PolicyError",
    "RecordBusy",
    "RecordDamaged",
    "RecordWriteFailed",
    "RegistryError",
    "RoleNotHeld",
    "RunArchived",
    "RunChainNotReviewable",
    "RunClosed",
    "TokenRejected",
    "UnknownCause",
    "UnknownReviewer",
    "UnknownRun",
]


def _said(value: object) -> str:
    """A caller's value for a message: a string's ``repr`` (an ordinary id is named whole,
    a long one shortened), anything else by its type; never raises."""
    if isinstance(value, str):
        text = repr(str.__str__(value))  # a str subclass's own __repr__ is not trusted
        return text if len(text) <= 210 else text[:207] + "..."
    return f"<{type(value).__name__}>"


class DeskError(Exception):
    """Base class of every error the desk raises on purpose."""


class ConfigError(DeskError):
    """A desk configuration file is unreadable or invalid. ``problems`` lists every problem."""

    what = "configuration"

    def __init__(self, path: Path | None, problems: Sequence[str]) -> None:
        self.path = None if path is None else Path(path)
        self.problems = tuple(problems)
        where = "" if self.path is None else f" {self.path}"
        listed = "; ".join(self.problems)
        super().__init__(f"invalid {self.what}{where}: {listed}")


class PolicyError(ConfigError):
    """A review policy file (``config/review/<name>.yaml``) is invalid."""

    what = "review policy"


class RegistryError(ConfigError):
    """A reviewer registry file (``config/review/reviewers.yaml``) is invalid."""

    what = "reviewer registry"


class UnknownRun(DeskError):
    """The run has no ``run.json`` under the record root (or its id is not a run id)."""

    def __init__(self, run_id: object) -> None:
        super().__init__(f"no run {_said(run_id)} in the record")
        self.run_id = run_id


class RunClosed(DeskError):
    """The run is a sealed experiment run: it is finished and the desk never appends to it.

    Same rule as ``mailroom_record.verify``: a run is closed when its header's ``kind`` is
    not ``service_period`` and it has at least one seal file (``seals/<n>.json``).
    """

    def __init__(self, run_id: str, kind: str) -> None:
        super().__init__(f"run {_said(run_id)} ({kind}) is sealed and closed; the desk does not write to it")
        self.run_id = run_id
        self.kind = kind


class RunArchived(DeskError):
    """The run is pruned (``mailroom_record.pack.prune_run``): its events are in its archive,
    not in this store, so the desk neither reads nor writes it. ``queue()`` over every run
    skips it; naming it raises this. Unpack the archive (``mailroom-record unpack``) first.
    ``archive`` is the archive the record names (not shown in the message)."""

    def __init__(self, run_id: str, archive: str | None = None) -> None:
        super().__init__(f"run {_said(run_id)} is pruned: its events are in its archive; unpack it first")
        self.run_id = run_id
        self.archive = archive


class RunChainNotReviewable(DeskError):
    """The request names the run-level chain (``doc_id`` ``"_run"``): it holds the run's own
    events, not a document, so it is never put in review."""

    def __init__(self, run_id: str, doc_id: str) -> None:
        super().__init__(
            f"{_said(doc_id)} of run {_said(run_id)} is the run-level chain, not a document; it is never reviewed"
        )
        self.run_id = run_id
        self.doc_id = doc_id


class AlreadyInReview(DeskError):
    """The document's head event is a ``review`` event: its case is already open."""

    def __init__(self, run_id: str, doc_id: str, seq: int) -> None:
        super().__init__(f"document {_said(doc_id)} of run {_said(run_id)} is already in review (head seq {seq})")
        self.run_id = run_id
        self.doc_id = doc_id
        self.seq = seq


class OutageNotReviewable(DeskError):
    """A cause names a provider outage: an outage makes a document wait, never park (R1)."""

    def __init__(self, cause: str) -> None:
        super().__init__(f"cause {_said(cause)} is a provider outage; outages wait, they are never reviewed")
        self.cause = cause


class UnknownCause(DeskError):
    """A cause is not in the policy's cause catalog."""

    def __init__(self, cause: str, policy_name: str) -> None:
        super().__init__(f"cause {_said(cause)} is not in the cause catalog of policy {_said(policy_name)}")
        self.cause = cause
        self.policy_name = policy_name


class NoCase(DeskError):
    """The document has never been in review in this run."""

    def __init__(self, run_id: str, doc_id: object) -> None:
        super().__init__(f"document {_said(doc_id)} of run {_said(run_id)} has no review case")
        self.run_id = run_id
        self.doc_id = doc_id


class DeskLockTimeout(DeskError):
    """A lock the desk needs was not acquired in time: the run's desk lock (another desk
    action holds it), or the run's seal lock (``seal_run`` is sealing it). ``lock`` names it."""

    def __init__(self, run_id: str, timeout: float, lock: str = "desk") -> None:
        super().__init__(f"the {lock} lock of run {_said(run_id)} was not acquired within {timeout:g} s")
        self.run_id = run_id
        self.timeout = timeout
        self.lock = lock


class BlobNotPresent(DeskError):
    """A blob the request shows the reviewer is not in the blob store (never stored, or
    tombstoned). Blob before event: the desk never records a reference to absent content."""

    def __init__(self, sha256: str, role: str, state: str) -> None:
        super().__init__(f"{role} blob {sha256} is {state}, not present; store it before enqueueing")
        self.sha256 = sha256
        self.role = role
        self.state = state


class RecordBusy(DeskError):
    """Another live writer in this process holds the document's chain (``DocumentInUse``)
    or the desk's writer id (``WriterInUse``). The desk does not append; the holder must
    close its writer first (the engine never writes to a document it hands to review)."""

    def __init__(self, run_id: str, doc_id: str, cause: BaseException) -> None:
        super().__init__(
            f"document {_said(doc_id)} of run {_said(run_id)} is held by another writer ({type(cause).__name__})"
        )
        self.run_id = run_id
        self.doc_id = doc_id
        self.cause = cause


class RecordDamaged(DeskError):
    """The record cannot be read safely (a corrupt line, a forked chain, an unreadable
    header). The desk refuses to act on it; run ``mailroom_record.verify``."""

    def __init__(self, run_id: str, cause: BaseException) -> None:
        super().__init__(f"run {_said(run_id)} cannot be read safely ({type(cause).__name__}); run verify")
        self.run_id = run_id
        self.cause = cause


class RecordWriteFailed(DeskError):
    """Writing the event to disk failed (a full disk, an I/O error): the original
    ``OSError`` is the ``__cause__``. The event may be absent, torn or (if only the fsync
    failed) present but not durable, so the record may have changed: read the case again
    (``Desk.case``) before retrying. A torn line is skipped by readers and reported by
    ``verify_run``; the next desk writer continues in a new segment."""

    def __init__(self, run_id: str, doc_id: str, cause: BaseException) -> None:
        super().__init__(
            f"writing the review event of document {_said(doc_id)} of run {_said(run_id)} failed "
            f"({type(cause).__name__})"
        )
        self.run_id = run_id
        self.doc_id = doc_id
        self.cause = cause


class UnknownReviewer(DeskError):
    """A strict registry does not list this reviewer id (or no reviewer holds this token)."""

    def __init__(self, reviewer: object) -> None:
        super().__init__(f"reviewer {_said(reviewer)} is not in the registry")
        self.reviewer = reviewer


class TokenRejected(DeskError):
    """An API token was given and does not match the reviewer's registered token hash."""

    def __init__(self, reviewer: str) -> None:
        super().__init__(f"the token given for reviewer {_said(reviewer)} does not match")
        self.reviewer = reviewer


class RoleNotHeld(DeskError):
    """The registry does not give the reviewer the role asked for."""

    def __init__(self, reviewer: object, role: object) -> None:
        super().__init__(f"reviewer {_said(reviewer)} does not hold role {_said(role)}")
        self.reviewer = reviewer
        self.role = role
