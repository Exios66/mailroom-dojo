"""Typed errors raised by the desk.

Every refusal has its own exception, so a caller (the node, the CLI, the HTTP API) can say
exactly what happened, and a test can assert it. A refused action never appends: an error
raised here means the record did not change. Messages name runs, documents, tokens and
file positions only, never document text.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

__all__ = [
    "AlreadyInReview",
    "ConfigError",
    "DeskError",
    "DeskLockTimeout",
    "NoCase",
    "OutageNotReviewable",
    "PolicyError",
    "RecordDamaged",
    "RegistryError",
    "RoleNotHeld",
    "RunClosed",
    "TokenRejected",
    "UnknownCause",
    "UnknownReviewer",
    "UnknownRun",
]


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
        super().__init__(f"no run {run_id!r} in the record")
        self.run_id = run_id


class RunClosed(DeskError):
    """The run is a sealed experiment run: it is finished and the desk never appends to it.

    Same rule as ``mailroom_record.verify``: a run is closed when its header's ``kind`` is
    not ``service_period`` and it has at least one seal file.
    """

    def __init__(self, run_id: str, kind: str) -> None:
        super().__init__(f"run {run_id!r} ({kind}) is sealed and closed; the desk does not write to it")
        self.run_id = run_id
        self.kind = kind


class AlreadyInReview(DeskError):
    """The document's head event is a ``review`` event: its case is already open."""

    def __init__(self, run_id: str, doc_id: str, seq: int) -> None:
        super().__init__(f"document {doc_id!r} of run {run_id!r} is already in review (head seq {seq})")
        self.run_id = run_id
        self.doc_id = doc_id
        self.seq = seq


class OutageNotReviewable(DeskError):
    """A cause names a provider outage: an outage makes a document wait, never park (R1)."""

    def __init__(self, cause: str) -> None:
        super().__init__(f"cause {cause!r} is a provider outage; outages wait, they are never reviewed")
        self.cause = cause


class UnknownCause(DeskError):
    """A cause is not in the policy's cause catalog."""

    def __init__(self, cause: str, policy_name: str) -> None:
        super().__init__(f"cause {cause!r} is not in the cause catalog of policy {policy_name!r}")
        self.cause = cause
        self.policy_name = policy_name


class NoCase(DeskError):
    """The document has never been in review in this run."""

    def __init__(self, run_id: str, doc_id: str) -> None:
        super().__init__(f"document {doc_id!r} of run {run_id!r} has no review case")
        self.run_id = run_id
        self.doc_id = doc_id


class DeskLockTimeout(DeskError):
    """The run's desk lock was not acquired in time (another desk action holds it)."""

    def __init__(self, run_id: str, timeout: float) -> None:
        super().__init__(f"the desk lock of run {run_id!r} was not acquired within {timeout:g} s")
        self.run_id = run_id
        self.timeout = timeout


class RecordDamaged(DeskError):
    """The record cannot be read safely (a corrupt line, a forked chain, an unreadable
    header). The desk refuses to act on it; run ``mailroom_record.verify``."""

    def __init__(self, run_id: str, cause: BaseException) -> None:
        super().__init__(f"run {run_id!r} cannot be read safely ({type(cause).__name__}); run verify")
        self.run_id = run_id
        self.cause = cause


class UnknownReviewer(DeskError):
    """A strict registry does not list this reviewer id (or no reviewer holds this token)."""

    def __init__(self, reviewer: str) -> None:
        super().__init__(f"reviewer {reviewer!r} is not in the registry")
        self.reviewer = reviewer


class TokenRejected(DeskError):
    """An API token was given and does not match the reviewer's registered token hash."""

    def __init__(self, reviewer: str) -> None:
        super().__init__(f"the token given for reviewer {reviewer!r} does not match")
        self.reviewer = reviewer


class RoleNotHeld(DeskError):
    """The registry does not give the reviewer the role asked for."""

    def __init__(self, reviewer: str, role: str) -> None:
        super().__init__(f"reviewer {reviewer!r} does not hold role {role!r}")
        self.reviewer = reviewer
        self.role = role
