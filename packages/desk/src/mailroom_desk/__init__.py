"""mailroom_desk: the human review desk, event-sourced on the run record (ADR-0007).

Documents enter review by configured rules (``config/review/<name>.yaml``), and the queue
is a fold over each document's ``review`` events: the desk stores nothing beside the
record. This package imports ``mailroom_record`` and ``mailroom_labels`` and nothing else
of the workspace (a test enforces it), and names no document class, field or pipeline node.
"""

from __future__ import annotations

from mailroom_desk.desk import WRITER_ID, Desk, Enqueued
from mailroom_desk.errors import (
    AlreadyInReview,
    BlobNotPresent,
    ConfigError,
    DeskError,
    DeskLockTimeout,
    NoCase,
    OutageNotReviewable,
    PolicyError,
    RecordBusy,
    RecordDamaged,
    RecordWriteFailed,
    RegistryError,
    RoleNotHeld,
    RunChainNotReviewable,
    RunClosed,
    TokenRejected,
    UnknownCause,
    UnknownReviewer,
    UnknownRun,
)
from mailroom_desk.fold import fold_case
from mailroom_desk.lock import desk_lock, seal_guard
from mailroom_desk.models import (
    ACTIONS,
    AssignPayload,
    CaseState,
    ClaimPayload,
    DeferPayload,
    EnqueuePayload,
    EscalatePayload,
    ReleasePayload,
    ReopenPayload,
    ReviewerRef,
    ReviewRequest,
    parse_payload,
)
from mailroom_desk.policy import OUTAGE_TOKENS, SPOT_CHECK, Policy, load_policy, parse_policy
from mailroom_desk.reviewers import ReviewerRegistry, load_registry, parse_registry

__version__ = "0.1.0"

__all__ = [
    "ACTIONS",
    "OUTAGE_TOKENS",
    "SPOT_CHECK",
    "WRITER_ID",
    "AlreadyInReview",
    "AssignPayload",
    "BlobNotPresent",
    "CaseState",
    "ClaimPayload",
    "ConfigError",
    "DeferPayload",
    "Desk",
    "DeskError",
    "DeskLockTimeout",
    "EnqueuePayload",
    "Enqueued",
    "EscalatePayload",
    "NoCase",
    "OutageNotReviewable",
    "Policy",
    "PolicyError",
    "RecordBusy",
    "RecordDamaged",
    "RecordWriteFailed",
    "RegistryError",
    "ReleasePayload",
    "ReopenPayload",
    "ReviewRequest",
    "ReviewerRef",
    "ReviewerRegistry",
    "RoleNotHeld",
    "RunChainNotReviewable",
    "RunClosed",
    "TokenRejected",
    "UnknownCause",
    "UnknownReviewer",
    "UnknownRun",
    "desk_lock",
    "fold_case",
    "load_policy",
    "load_registry",
    "parse_payload",
    "parse_policy",
    "parse_registry",
    "seal_guard",
]
