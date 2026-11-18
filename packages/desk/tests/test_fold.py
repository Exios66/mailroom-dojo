"""The queue fold: episodes, statuses, leases and deferrals against an injected clock, SLA
breach, queue order, and agreement with ``Index.review_queue()`` on random histories."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from desk_testkit import T0, engine_append, log_of, make_run, policy, request
from hypothesis import example, given
from hypothesis import strategies as st

from mailroom_desk import AlreadyInReview, Desk, RunChainNotReviewable, fold_case
from mailroom_desk.fold import episode
from mailroom_desk.models import (
    NODE,
    AssignPayload,
    ClaimPayload,
    DeferPayload,
    EscalatePayload,
    ReleasePayload,
    ReopenPayload,
    ReviewerRef,
    format_ts,
)
from mailroom_record.index import Index
from mailroom_record.models import Event

US = timedelta(microseconds=1)
ACTOR = ReviewerRef(id="rv-a", role="reviewer", identity="asserted")


def desk_event(env: SimpleNamespace, doc_id: str, payload: Any, at: datetime, run_id: str = "r1") -> Event:
    """Append a desk action event the way the desk records one (writer ``desk``, node
    ``human_review``). D1 has no assign/claim API yet; these tests write the events the
    fold must read."""
    raw = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    with log_of(env.root).open_writer(run_id, "desk") as w:
        return w.append(doc_id=doc_id, node=NODE, kind="review", payload=raw, ts=format_ts(at))


def enqueue(env: SimpleNamespace, doc_id: str, *causes: str, at: datetime = T0, **kw: Any):
    env.clock.moment = at
    return env.desk.enqueue(request(doc_id, *causes, **kw), env.policy)


# --- statuses, leases and deferrals -----------------------------------------------------


def test_claim_holds_until_the_lease_expires_exactly(env: SimpleNamespace) -> None:
    enqueue(env, "d1", "flag_low")
    lease = T0 + timedelta(hours=1)
    desk_event(env, "d1", ClaimPayload(action="claim", actor=ACTOR, lease_expires_at=format_ts(lease)), T0 + timedelta(minutes=1))
    held = env.desk.case("r1", "d1", now=lease - US)
    assert (held.status, held.claimed_by, held.lease_expires_at) == ("claimed", "rv-a", lease)
    lapsed = env.desk.case("r1", "d1", now=lease)
    assert (lapsed.status, lapsed.claimed_by, lapsed.lease_expires_at) == ("open", None, None)


def test_injected_clock_drives_lease_expiry_in_the_queue(env: SimpleNamespace) -> None:
    enqueue(env, "d1", "flag_low")
    lease = T0 + timedelta(minutes=30)
    desk_event(env, "d1", AssignPayload(action="assign", actor=ACTOR, assignee="rv-b"), T0)
    desk_event(env, "d1", ClaimPayload(action="claim", actor=ACTOR, lease_expires_at=format_ts(lease)), T0)
    env.clock.moment = lease - US
    assert [c.status for c in env.desk.queue("r1")] == ["claimed"]
    assert env.desk.queue("r1", statuses=["claimed"])[0].as_of == lease - US
    env.clock.advance(microseconds=1)
    case = env.desk.queue("r1")[0]
    assert (case.status, case.assignee, case.claimed_by) == ("assigned", "rv-b", None)
    assert env.desk.queue("r1", statuses=["claimed"]) == []


def test_release_escalate_defer_and_reopen(env: SimpleNamespace) -> None:
    enqueue(env, "d1", "flag_low")
    t1 = T0 + timedelta(hours=1)
    lease = format_ts(T0 + timedelta(days=1))
    desk_event(env, "d1", AssignPayload(action="assign", actor=ACTOR, assignee="rv-b"), T0)
    desk_event(env, "d1", ClaimPayload(action="claim", actor=ACTOR, lease_expires_at=lease), T0)
    desk_event(env, "d1", ReleasePayload(action="release", actor=ACTOR), T0)
    c = env.desk.case("r1", "d1", now=t1)
    assert (c.status, c.assignee, c.claimed_by) == ("open", None, None)

    desk_event(env, "d1", AssignPayload(action="assign", actor=ACTOR, assignee="rv-b"), T0)
    desk_event(env, "d1", EscalatePayload(action="escalate", actor=ACTOR, priority="high", sla_hours=2, queue="senior"), t1)
    c = env.desk.case("r1", "d1", now=t1)
    assert (c.status, c.priority, c.queue, c.assignee) == ("open", "high", "senior", None)
    assert (c.sla_hours, c.sla_due) == (2.0, t1 + timedelta(hours=2))  # counted from the escalation
    assert c.enqueued_at == T0

    until = t1 + timedelta(hours=3)
    desk_event(env, "d1", DeferPayload(action="defer", actor=ACTOR, until=format_ts(until)), t1)
    assert env.desk.case("r1", "d1", now=until - US).status == "deferred"
    assert env.desk.case("r1", "d1", now=until - US).deferred_until == until
    assert env.desk.case("r1", "d1", now=until).status == "open"

    t2 = t1 + timedelta(hours=4)
    reopen = ReopenPayload(
        action="reopen", actor=ACTOR, causes=("flag_normal",), rule="default", priority="normal", sla_hours=24,
        queue="main", sampled=False, policy_name="synthetic", policy_hash=env.policy.hash,
    )
    desk_event(env, "d1", reopen, t2)
    c = env.desk.case("r1", "d1", now=t2)
    assert (c.status, c.priority, c.queue, c.causes, c.requested_by) == ("open", "normal", "main", ("flag_normal",), "rv-a")
    assert (c.enqueued_at, c.sla_due, c.anomalies) == (t2, t2 + timedelta(hours=24), ())


def test_reopen_clears_assignment_claim_and_deferral(env: SimpleNamespace) -> None:
    enqueue(env, "d1", "flag_low")
    far = format_ts(T0 + timedelta(days=5))
    desk_event(env, "d1", AssignPayload(action="assign", actor=ACTOR, assignee="rv-b"), T0)
    desk_event(env, "d1", ClaimPayload(action="claim", actor=ACTOR, lease_expires_at=far), T0)
    desk_event(env, "d1", DeferPayload(action="defer", actor=ACTOR, until=far), T0)
    before = env.desk.case("r1", "d1", now=T0)
    assert (before.status, before.assignee, before.deferred_until) == ("deferred", "rv-b", T0 + timedelta(days=5))
    reopen = ReopenPayload(
        action="reopen", actor=ACTOR, causes=("flag_normal",), rule="default", priority="normal", sla_hours=24,
        queue="main", sampled=False, policy_name="synthetic", policy_hash=env.policy.hash,
    )
    desk_event(env, "d1", reopen, T0 + timedelta(hours=1))
    c = env.desk.case("r1", "d1", now=T0 + timedelta(hours=1))
    assert (c.status, c.assignee, c.claimed_by, c.lease_expires_at, c.deferred_until) == ("open", None, None, None, None)


def test_claim_clears_a_deferral(env: SimpleNamespace) -> None:
    enqueue(env, "d1", "flag_low")
    until = T0 + timedelta(days=2)
    lease = T0 + timedelta(hours=1)
    desk_event(env, "d1", DeferPayload(action="defer", actor=ACTOR, until=format_ts(until)), T0)
    assert env.desk.case("r1", "d1", now=T0).status == "deferred"
    desk_event(env, "d1", ClaimPayload(action="claim", actor=ACTOR, lease_expires_at=format_ts(lease)), T0)
    held = env.desk.case("r1", "d1", now=T0)
    assert (held.status, held.claimed_by, held.deferred_until) == ("claimed", "rv-a", None)
    lapsed = env.desk.case("r1", "d1", now=lease)  # the deferral does not come back
    assert (lapsed.status, lapsed.deferred_until) == ("open", None)


def test_escalate_without_sla_keeps_the_due_time(env: SimpleNamespace) -> None:
    enqueue(env, "d1", "flag_low")
    desk_event(env, "d1", EscalatePayload(action="escalate", actor=ACTOR, priority="high"), T0 + timedelta(hours=5))
    c = env.desk.case("r1", "d1", now=T0)
    assert (c.priority, c.sla_due) == ("high", T0 + timedelta(hours=72))


def test_resolution_ends_the_episode(env: SimpleNamespace) -> None:
    first = enqueue(env, "d1", "flag_high")
    engine_append(env.root, "r1", "d1", "output")
    c = env.desk.case("r1", "d1", now=T0 + timedelta(days=30))
    assert (c.status, c.in_review, c.resolved_seq, c.opened_seq) == ("resolved", False, 1, 0)
    assert not c.breached(T0 + timedelta(days=30))  # a resolved case is never breached
    assert c.events == (first.event,)
    assert env.desk.queue("r1") == []


def test_episode_is_review_events_since_the_last_non_review_event(env: SimpleNamespace) -> None:
    enqueue(env, "d1", "flag_low")  # seq 0
    engine_append(env.root, "r1", "d1", "decision")  # seq 1 resolves it
    second = enqueue(env, "d1", "flag_high", at=T0 + timedelta(hours=1))  # seq 2
    desk_event(env, "d1", AssignPayload(action="assign", actor=ACTOR, assignee="rv-b"), T0)  # seq 3
    c = env.desk.case("r1", "d1", now=T0)
    assert [e.seq for e in c.events] == [2, 3]
    assert (c.opened_seq, c.causes, c.priority, c.assignee) == (2, ("flag_high",), "high", "rv-b")
    events = list(log_of(env.root).iter_events("r1", "d1"))
    eps, resolution = episode(events)
    assert [e.seq for e in eps] == [2, 3] and resolution is None
    assert episode(events[:2]) == ([events[0]], events[1])
    assert episode([e for e in events if e.kind != "review"]) is None
    assert second.case.opened_seq == 2


def test_unreadable_and_out_of_place_payloads_are_anomalies_not_drops(env: SimpleNamespace) -> None:
    engine_append(env.root, "r1", "d1", "review", payload={"note": "not a desk payload"})
    desk_event(env, "d1", AssignPayload(action="assign", actor=ACTOR, assignee="rv-b"), T0)
    c = env.desk.case("r1", "d1", now=T0)
    assert c.in_review and c.priority is None and c.rule is None and c.assignee == "rv-b"
    assert len(c.anomalies) == 2
    assert c.anomalies[0].startswith("seq 0: not a desk review payload")
    assert "not a desk payload" not in " ".join(c.anomalies)  # never the payload's content
    assert c.anomalies[1] == "seq 1: assign before the episode was opened"

    enqueue(env, "d2", "flag_low")
    second = request("d2", "flag_high")
    raw = {
        "action": "enqueue", "causes": ["flag_high"], "rule": "default", "priority": "high", "sla_hours": 4,
        "queue": "main", "sampled": False, "policy_name": "synthetic", "policy_hash": env.policy.hash,
        "requested_by": second.requested_by,
    }
    desk_event(env, "d2", raw, T0)
    c2 = env.desk.case("r1", "d2", now=T0)
    assert c2.causes == ("flag_low",) and c2.anomalies == ("seq 1: enqueue inside an open episode, ignored",)


def test_fold_needs_an_aware_now(env: SimpleNamespace) -> None:
    done = enqueue(env, "d1", "flag_low")
    with pytest.raises(ValueError):
        fold_case("r1", "d1", [done.event], datetime(2026, 1, 1))
    assert fold_case("r1", "d1", [], T0) is None
    bad = Desk(env.root, clock=lambda: datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        bad.queue("r1")


# --- SLA and order --------------------------------------------------------------------


def test_sla_breach_boundaries(env: SimpleNamespace) -> None:
    c = enqueue(env, "d1", "flag_high").case  # 4 hours
    due = T0 + timedelta(hours=4)
    assert c.sla_due == due
    assert not c.breached(due - US)
    assert not c.breached(due)  # due exactly is not yet late
    assert c.breached(due + US)


def test_queue_order_priority_then_due_then_opened_then_id(env: SimpleNamespace) -> None:
    pol = policy(sample={"rate": 0.0, "seed": ""})
    env.policy = pol
    enqueue(env, "low-early", "flag_low", at=T0)  # low, due T0+72h
    enqueue(env, "normal", "flag_normal", at=T0 + timedelta(hours=1))  # normal, due +25h
    enqueue(env, "high-late", "flag_high", at=T0 + timedelta(hours=2))  # high, due +6h
    enqueue(env, "high-early", "flag_high", at=T0 + timedelta(hours=1))  # high, due +5h
    enqueue(env, "conf", "flag_conf", at=T0)  # low, due T0+48h: earlier than low-early
    enqueue(env, "conf-late", "flag_conf", at=T0 + timedelta(hours=1))  # low, due +49h: opened later, due sooner
    enqueue(env, "b-tie", "flag_high", at=T0 + timedelta(hours=1))  # ties high-early on time
    engine_append(env.root, "r1", "unreadable", "review", payload={"x": 1})  # unknown priority
    order = [c.doc_id for c in env.desk.queue("r1", now=T0)]
    assert order == ["unreadable", "b-tie", "high-early", "high-late", "normal", "conf", "conf-late", "low-early"]
    assert order.index("conf-late") < order.index("low-early")  # due time before opened time
    assert [c.doc_id for c in env.desk.queue("r1", now=T0, queue="senior")] == ["b-tie", "high-early", "high-late"]
    assert [c.doc_id for c in env.desk.queue(now=T0, statuses=["open"])] == order


def test_queue_spans_every_run_when_none_is_named(env: SimpleNamespace) -> None:
    make_run(env.root, "r2")
    enqueue(env, "d1", "flag_low")
    env.clock.moment = T0
    env.desk.enqueue(request("d1", "flag_high", run_id="r2"), env.policy)
    assert [(c.run_id, c.doc_id) for c in env.desk.queue()] == [("r2", "d1"), ("r1", "d1")]


# --- agreement with Index.review_queue() ------------------------------------------------

DOCS = ("d0", "d1", "d2", "d3")
RUN_CHAIN = "_run"  # the run-level chain: run events only, never put in review
RUNS = ("r1", "r2")
NON_REVIEW = ("handoff", "model_call", "decision", "output", "error", "serving")
FOREIGN = ("garbage", "empty", "assign", "claim_lapsed", "claim_live", "defer", "release")
HOUR = timedelta(hours=1)

_op = st.one_of(
    st.tuples(st.just("engine"), st.sampled_from(RUNS), st.sampled_from((*DOCS, RUN_CHAIN)), st.sampled_from(NON_REVIEW)),
    st.tuples(st.just("foreign_review"), st.sampled_from(RUNS), st.sampled_from(DOCS), st.sampled_from(FOREIGN)),
    st.tuples(
        st.just("desk"), st.sampled_from(RUNS), st.sampled_from((*DOCS, RUN_CHAIN)), st.sampled_from(("flag_low", "flag_high", "slot:a:b"))
    ),
)


def _payload(kind: str) -> dict[str, Any]:
    """A review payload another writer appends (D1 has no API for these actions yet)."""
    payloads: dict[str, Any] = {
        "garbage": {"action": "teleport", "where": "elsewhere"},
        "empty": {},
        "assign": AssignPayload(action="assign", actor=ACTOR, assignee="rv-b"),
        "claim_lapsed": ClaimPayload(action="claim", actor=ACTOR, lease_expires_at=format_ts(T0)),  # lapsed at T0
        "claim_live": ClaimPayload(action="claim", actor=ACTOR, lease_expires_at=format_ts(T0 + HOUR)),
        "defer": DeferPayload(action="defer", actor=ACTOR, until=format_ts(T0 + HOUR)),
        "release": ReleasePayload(action="release", actor=ACTOR),
    }
    raw = payloads[kind]
    return raw.model_dump(mode="json") if hasattr(raw, "model_dump") else raw


class _Model:
    """The expected case of one document, kept by the test from the operations alone (an
    oracle independent of ``fold_case``): ``None`` when not in review."""

    def __init__(self) -> None:
        self.cases: dict[tuple[str, str], dict[str, Any] | None] = {}

    def in_review(self, key: tuple[str, str]) -> bool:
        return self.cases.get(key) is not None

    def open(self, key: tuple[str, str]) -> dict[str, Any]:
        case = self.cases.get(key)
        if case is None:
            case = self.cases[key] = {"assignee": False, "claimed": False, "deferred": False}
        return case

    def foreign(self, key: tuple[str, str], kind: str) -> None:
        case = self.open(key)  # any review event on a resolved document opens an episode
        if kind == "assign":
            case.update(assignee=True, claimed=False)
        elif kind in ("claim_lapsed", "claim_live"):
            case.update(claimed=kind == "claim_live", deferred=False)
        elif kind == "defer":
            case.update(deferred=True, claimed=False)
        elif kind == "release":
            case.update(assignee=False, claimed=False)

    def status(self, key: tuple[str, str]) -> str:
        case = self.cases[key]
        assert case is not None
        if case["deferred"]:
            return "deferred"
        if case["claimed"]:
            return "claimed"
        return "assigned" if case["assignee"] else "open"


@given(ops=st.lists(_op, max_size=24))
@example(
    ops=[
        ("desk", "r1", "d0", "flag_low"),  # in review
        ("desk", "r1", "d0", "flag_high"),  # refused: already in review
        ("engine", "r1", "d0", "output"),  # resolved
        ("desk", "r1", "d0", "flag_high"),  # a second episode
        ("foreign_review", "r1", "d0", "garbage"),  # unreadable action, still in review
        ("foreign_review", "r2", "d1", "claim_lapsed"),  # a claim with no opening event
        ("engine", "r2", "d2", "decision"),  # never reviewed
        ("desk", "r2", "d2", "slot:a:b"),
        ("foreign_review", "r2", "d2", "assign"),
        ("foreign_review", "r2", "d2", "claim_live"),
        ("desk", "r2", "d3", "flag_high"),
        ("foreign_review", "r2", "d3", "defer"),
        ("engine", "r1", "_run", "handoff"),  # the run-level chain
        ("desk", "r1", "_run", "flag_low"),  # refused: not a document
        ("engine", "r2", "d2", "handoff"),
    ]
)
def test_fold_agrees_with_the_index_on_random_histories(tmp_path_factory: pytest.TempPathFactory, ops: list) -> None:
    """After every step: the plain (log) queue and the index-backed queue are the same list
    (same cases, statuses and order); their membership is ``Index.review_queue()`` of an
    applied and of a rebuilt index; each status is the one the test's own model expects;
    the queue is in ``sort_key`` order; the desk refuses exactly when the model says the
    document is in review (or is ``_run``), and never writes a review event on a review head."""
    root = tmp_path_factory.mktemp("history")
    for run_id in RUNS:
        make_run(root, run_id)
    log = log_of(root)
    applied = Index(root / "applied.sqlite")
    applied.reindex(log)  # registers both (empty) runs; every later event is applied
    plain = Desk(root, clock=lambda: T0)
    indexed = Desk(root, index=applied, clock=lambda: T0)
    pol = policy(sample={"rate": 0.0, "seed": ""})
    model = _Model()

    for step, (op, run_id, doc_id, arg) in enumerate(ops):
        key = (run_id, doc_id)
        if op == "engine":
            applied.apply(engine_append(root, run_id, doc_id, arg))
            model.cases[key] = None
        elif op == "foreign_review":
            applied.apply(engine_append(root, run_id, doc_id, "review", payload=_payload(arg)))
            model.foreign(key, arg)
        else:
            before = len(list(log.iter_events(run_id, doc_id)))
            refusal = RunChainNotReviewable if doc_id == RUN_CHAIN else AlreadyInReview if model.in_review(key) else None
            if refusal is not None:
                with pytest.raises(refusal):
                    indexed.enqueue(request(doc_id, arg, run_id=run_id), pol)
            else:
                done = indexed.enqueue(request(doc_id, arg, run_id=run_id), pol)
                assert done is not None and done.index_error is None
                model.open(key)
            assert len(list(log.iter_events(run_id, doc_id))) - before == (0 if refusal else 1)

        rebuilt = Index(root / f"rebuilt-{step}.sqlite")
        rebuilt.reindex(log)
        want = {(r.run_id, r.doc_id) for r in rebuilt.review_queue()}
        assert {(r.run_id, r.doc_id) for r in applied.review_queue()} == want
        assert want == {k for k, case in model.cases.items() if case is not None}
        from_log = plain.queue()
        assert indexed.queue() == from_log  # same cases, statuses and order on both paths
        assert {(c.run_id, c.doc_id) for c in from_log} == want
        assert [c.status for c in from_log] == [model.status((c.run_id, c.doc_id)) for c in from_log]
        keys = [c.sort_key() for c in from_log]
        assert keys == sorted(keys) and len(set(keys)) == len(keys)
        for run_id in RUNS:
            assert plain.queue(run_id) == [c for c in from_log if c.run_id == run_id]
            for row in rebuilt.documents(run_id):
                events = list(log.iter_events(run_id, row.doc_id))
                case = fold_case(run_id, row.doc_id, events, T0)
                in_review = case is not None and case.in_review
                assert in_review == row.in_review == (events[-1].kind == "review")
                for prev, ev in itertools.pairwise(events):  # the desk never stacks on a review head
                    assert not (ev.node == NODE and prev.kind == "review"), (run_id, row.doc_id, ev.seq)
        assert applied.drift(log) == []
