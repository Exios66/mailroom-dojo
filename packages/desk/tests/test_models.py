"""The action vocabulary and payload models: what a stored review payload may hold."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from mailroom_desk import ACTIONS, EnqueuePayload, parse_payload
from mailroom_desk.models import CaseState, format_ts, is_token, parse_ts, priority_rank

ACTOR = {"id": "rv-a", "role": "reviewer", "identity": "authenticated"}
ROUTING = {
    "causes": ["flag_low"],
    "rule": "default",
    "priority": "low",
    "sla_hours": 72,
    "queue": "main",
    "sampled": False,
    "policy_name": "synthetic",
    "policy_hash": "a" * 64,
}
VALID = {
    "enqueue": {"action": "enqueue", **ROUTING, "requested_by": "node:x"},
    "reopen": {"action": "reopen", **ROUTING, "actor": ACTOR, "reason": "new_evidence"},
    "assign": {"action": "assign", "actor": ACTOR, "assignee": "rv-b"},
    "claim": {"action": "claim", "actor": ACTOR, "lease_expires_at": "2026-11-18T13:00:00Z"},
    "release": {"action": "release", "actor": ACTOR},
    "escalate": {"action": "escalate", "actor": ACTOR, "priority": "high", "sla_hours": 2},
    "defer": {"action": "defer", "actor": ACTOR, "until": "2026-11-19T00:00:00.5Z"},
}


def test_the_vocabulary_is_exactly_the_payload_models() -> None:
    assert set(VALID) == set(ACTIONS)
    for action, raw in VALID.items():
        assert parse_payload(raw).action == action


@pytest.mark.parametrize(
    "action, change",
    [
        ("enqueue", {"action": "teleport"}),
        ("enqueue", {"note": "free text is never a payload key"}),
        ("enqueue", {"causes": []}),
        ("enqueue", {"causes": ["flag_low", "flag_low"]}),
        ("enqueue", {"priority": "urgent"}),
        ("enqueue", {"sla_hours": 0}),
        ("enqueue", {"sla_hours": "4"}),
        ("enqueue", {"sampled": "false"}),
        ("enqueue", {"policy_hash": "A" * 64}),
        ("enqueue", {"rule": "Not A Token"}),
        ("reopen", {"actor": {**ACTOR, "identity": "claimed"}}),
        ("assign", {"assignee": "RV B"}),
        ("claim", {"lease_expires_at": "2026-11-18 13:00"}),
        ("claim", {"lease_expires_at": "2026-13-18T13:00:00Z"}),
        ("defer", {"until": None}),
        ("escalate", {"priority": None}),
    ],
)
def test_bad_payloads_are_refused(action: str, change: dict) -> None:
    with pytest.raises(ValidationError):
        parse_payload(VALID[action] | change)
    raw = dict(VALID[action])
    raw.pop("action")
    with pytest.raises(ValidationError):
        parse_payload(raw)


def test_enqueue_payload_round_trips_through_json() -> None:
    p = EnqueuePayload.model_validate({**VALID["enqueue"], "causes": ("flag_low",)})
    assert parse_payload(p.model_dump(mode="json")) == p


def test_tokens_and_times() -> None:
    for good in ("a", "a_b", "a:b:c", "x9"):
        assert is_token(good)
    for bad in ("", "A", "a:", ":a", "a::b", "a b", "a\n", "x" * 201, 3, None):
        assert not is_token(bad)
    moment = parse_ts("2026-11-18T12:00:00.000001Z")
    assert format_ts(moment) == "2026-11-18T12:00:00.000001Z"
    assert format_ts(moment.astimezone(timezone(timedelta(hours=5)))) == "2026-11-18T12:00:00.000001Z"
    with pytest.raises(ValueError):
        format_ts(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        parse_ts("2026-11-18T12:00:00+00:00")
    assert [priority_rank(p) for p in (None, "high", "normal", "low")] == [-1, 0, 1, 2]


def test_case_state_is_frozen_and_breach_needs_a_due_time() -> None:
    fields = dict(
        run_id="r1",
        doc_id="d1",
        status="open",
        as_of=parse_ts("2026-11-18T12:00:00Z"),
        causes=(),
        rule=None,
        policy_name=None,
        policy_hash=None,
        priority=None,
        sla_hours=None,
        sla_due=None,
        queue=None,
        required_role=None,
        prefill_ref=None,
        sampled=False,
        requested_by=None,
        enqueued_at=None,
        assignee=None,
        claimed_by=None,
        lease_expires_at=None,
        deferred_until=None,
        opened_seq=0,
        head_seq=0,
        resolved_seq=None,
        events=(),
        anomalies=(),
    )
    case = CaseState(**fields)
    assert not case.breached(parse_ts("2100-01-01T00:00:00Z"))
    with pytest.raises(ValidationError):
        case.status = "resolved"  # type: ignore[misc]


# --- references: requested_by, class_hint, prefill_ref -------------------------------

BAD_REFS = [
    "has space",
    "nul\x00byte",
    "bidi\u202eoverride",
    "line separator",
    "para separator",
    "c1\x85control",
    "tab\there",
    "esc\x1bseq",
    "zero\u200bwidth",
    "café",
    "-leading-dash",
    "",
    "x" * 129,
]


def test_is_ref_accepts_only_short_ascii_tokens() -> None:
    from mailroom_desk.models import is_ref

    for good in ("node:classify", "rv-a", "kind_b", "prefill-combo", "Contract.v2", "a/b:c-d_e.f", "x" * 128):
        assert is_ref(good), good
    for bad in [*BAD_REFS, "trailing\n", None, 3]:
        assert not is_ref(bad), bad


@pytest.mark.parametrize("bad", BAD_REFS)
def test_free_text_never_enters_a_reference_field(bad: str) -> None:
    from mailroom_desk import ReviewRequest

    base = {"run_id": "r1", "doc_id": "d1", "causes": ["flag_low"], "requested_by": "node:x"}
    for key in ("requested_by", "class_hint"):
        with pytest.raises(ValidationError):
            ReviewRequest(**(base | {key: bad}))
    for key in ("requested_by", "class_hint", "prefill_ref"):
        with pytest.raises(ValidationError):
            parse_payload(VALID["enqueue"] | {key: bad})
    with pytest.raises(ValidationError):
        parse_payload(VALID["reopen"] | {"prefill_ref": bad})
