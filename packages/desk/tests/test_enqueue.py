"""Desk.enqueue on real record roots: routing, sampling, idempotency and every refusal."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from desk_testkit import (
    T0,
    doc_events,
    engine_append,
    log_of,
    make_run,
    policy,
    request,
    review_events,
    seal_within,
    segment_files,
)
from mailroom_desk import (
    WRITER_ID,
    AlreadyInReview,
    BlobNotPresent,
    Desk,
    DeskLockTimeout,
    EnqueuePayload,
    NoCase,
    OutageNotReviewable,
    RecordBusy,
    RecordDamaged,
    RecordWriteFailed,
    ReviewRequest,
    RunChainNotReviewable,
    RunClosed,
    UnknownCause,
    UnknownRun,
    desk_lock,
    parse_payload,
    seal_guard,
)
from mailroom_desk.lock import seal_lock_path
from mailroom_desk.models import NODE, format_ts
from mailroom_record.index import Index
from mailroom_record.seal import seal_run
from mailroom_record.verify import verify_run


def verify_ok(env: SimpleNamespace, run_id: str = "r1") -> None:
    report = verify_run(env.log, env.blobs, run_id)
    failing = [p for p in report.problems if p.fails]
    assert report.ok and failing == [], failing


#: Tests that name a descriptor's file through ``/proc/self/fd`` (Linux only).
LINUX_ONLY = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/self/fd")


# --- the enqueue event --------------------------------------------------------------


def test_enqueue_appends_one_review_event_with_the_routing(env: SimpleNamespace) -> None:
    shown = env.blobs.put(b"synthetic text shown to the reviewer", "transcript")
    engine_append(env.root, "r1", "d1", "decision")
    req = request("d1", "flag_high", "flag_low", shown=[shown], class_hint="kind_c", confidence_hint=0.9)
    done = env.desk.enqueue(req, env.policy)
    assert done is not None and done.index_error is None
    e = done.event
    assert (e.kind, e.node, e.cause, e.seq, e.ts) == ("review", NODE, "flag_high", 1, format_ts(T0))
    assert e.inputs == [shown]
    p = parse_payload(e.payload)
    assert isinstance(p, EnqueuePayload)
    assert (p.causes, p.rule, p.priority, p.sla_hours, p.queue, p.required_role) == (
        ("flag_high", "flag_low"),
        "high_to_senior",
        "high",
        4.0,
        "senior",
        "senior",
    )
    assert (p.sampled, p.policy_name, p.policy_hash) == (False, "synthetic", env.policy.hash)
    assert (p.requested_by, p.class_hint, p.confidence_hint) == ("tester", "kind_c", 0.9)
    # The event is durable and in the desk's own segment.
    assert doc_events(env.root, "r1", "d1")[-1] == e
    assert any(name.startswith(WRITER_ID) for name in segment_files(env.root))
    case = done.case
    assert (case.status, case.priority, case.queue, case.enqueued_at, case.opened_seq) == (
        "open",
        "high",
        "senior",
        T0,
        1,
    )
    assert env.desk.case("r1", "d1") == case
    verify_ok(env)


def test_enqueue_of_a_document_with_no_events_starts_its_chain(env: SimpleNamespace) -> None:
    done = env.desk.enqueue(request("fresh", "flag_normal"), env.policy)
    assert done.event.seq == 0
    verify_ok(env)


def test_payload_holds_no_free_text(env: SimpleNamespace) -> None:
    sentinel = "SECRET-DOCUMENT-TEXT"
    shown = env.blobs.put(sentinel.encode(), "original")
    env.desk.enqueue(request("d1", "flag_low", shown=[shown]), env.policy)
    raw = b"".join(segment_files(env.root).values())
    assert sentinel.encode() not in raw
    assert shown.sha256.encode() in raw  # positive control: the reference is recorded


# --- refusals before the record is touched -------------------------------------------


@pytest.mark.parametrize(
    "cause", ["transient_exhausted", "provider_unavailable", "slot:x:provider_unavailable"]
)
def test_outage_causes_are_refused(env: SimpleNamespace, cause: str) -> None:
    before = segment_files(env.root)
    with pytest.raises(OutageNotReviewable) as info:
        env.desk.enqueue(request("d1", "flag_low", cause), env.policy)
    assert info.value.cause == cause
    assert segment_files(env.root) == before


def test_run_level_chain_is_refused(env: SimpleNamespace) -> None:
    """``_run`` holds the run's own events, not a document: never put in review, with
    causes or as a sample-only request the sample would select."""
    engine_append(env.root, "r1", "_run", "handoff")
    before = segment_files(env.root)
    everything = policy(sample={"rate": 1.0, "seed": "s"})
    for req, pol in ((request("_run", "flag_high"), env.policy), (request("_run"), everything)):
        with pytest.raises(RunChainNotReviewable) as info:
            env.desk.enqueue(req, pol)
        assert (info.value.run_id, info.value.doc_id) == ("r1", "_run")
    assert segment_files(env.root) == before
    assert review_events(env.root) == []
    assert env.desk.enqueue(request("d1"), everything)  # the same sample-only request for a document enters


def test_unknown_cause_is_refused(env: SimpleNamespace) -> None:
    with pytest.raises(UnknownCause) as info:
        env.desk.enqueue(request("d1", "flag_low", "never_listed"), env.policy)
    assert (info.value.cause, info.value.policy_name) == ("never_listed", "synthetic")
    assert review_events(env.root) == []


def test_unknown_run_is_refused(env: SimpleNamespace) -> None:
    with pytest.raises(UnknownRun):
        env.desk.enqueue(request("d1", "flag_low", run_id="r2"), env.policy)
    for bad in ("../r1", "", ".", "r1/x", "a" * 201, None):
        with pytest.raises(UnknownRun):
            env.desk.run(bad)  # type: ignore[arg-type]
    with pytest.raises(UnknownRun):
        env.desk.queue("r2")
    with pytest.raises(UnknownRun):
        env.desk.case("r2", "d1")
    assert not (env.root / "runs" / "r2").exists()
    assert not (env.root / "desk" / "r2.lock").exists()


def test_unknown_run_is_refused_even_when_nothing_is_sampled(env: SimpleNamespace) -> None:
    with pytest.raises(UnknownRun):
        env.desk.enqueue(request("d1", run_id="r2"), env.policy)


def test_missing_or_tombstoned_shown_blob_is_refused(env: SimpleNamespace) -> None:
    ghost = env.blobs.put(b"x", "transcript").model_copy(update={"sha256": "e" * 64})
    with pytest.raises(BlobNotPresent) as info:
        env.desk.enqueue(request("d1", "flag_low", shown=[ghost]), env.policy)
    assert (info.value.sha256, info.value.state) == ("e" * 64, "missing")
    gone = env.blobs.put(b"to be erased", "original")
    env.blobs.tombstone(gone.sha256, "erasure request")
    with pytest.raises(BlobNotPresent) as info:
        env.desk.enqueue(request("d1", "flag_low", shown=[gone]), env.policy)
    assert info.value.state == "tombstoned"
    assert review_events(env.root) == []
    # Nothing is held: the next valid enqueue proceeds at once.
    assert Desk(env.root, clock=env.clock, lock_timeout=0).enqueue(request("d1", "flag_low"), env.policy)
    verify_ok(env)


def test_blob_tombstoned_between_check_and_append_is_refused(env: SimpleNamespace) -> None:
    shown = env.blobs.put(b"erased while waiting", "original")

    def clock():  # read under the lock, after the check and before the append
        if env.blobs.exists(shown.sha256) == "present":
            env.blobs.tombstone(shown.sha256, "erasure request")
        return T0

    with pytest.raises(BlobNotPresent):
        Desk(env.root, clock=clock).enqueue(request("d1", "flag_low", shown=[shown]), env.policy)
    assert review_events(env.root) == []
    verify_ok(env)


def test_bad_arguments_are_type_errors(env: SimpleNamespace) -> None:
    with pytest.raises(TypeError):
        env.desk.enqueue({"run_id": "r1"}, env.policy)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        env.desk.enqueue(request("d1", "flag_low"), {"name": "x"})  # type: ignore[arg-type]


# --- sampling through enqueue -------------------------------------------------------


def test_sample_only_request_enters_only_when_sampled(env: SimpleNamespace) -> None:
    pol = policy(sample={"rate": 0.5, "seed": "s"})
    docs = [f"doc{i}" for i in range(40)]
    picked = [d for d in docs if pol.sample.selects(d)]
    assert 0 < len(picked) < len(docs)  # positive control: both outcomes occur
    for d in docs:
        out = env.desk.enqueue(request(d), pol)
        if d in picked:
            p = parse_payload(out.event.payload)
            assert (out.event.cause, p.causes, p.sampled, p.rule, p.queue) == (
                "spot_check",
                ("spot_check",),
                True,
                "sampled",
                "qa",
            )
        else:
            assert out is None
    assert sorted(c.doc_id for c in env.desk.queue("r1")) == sorted(picked)
    verify_ok(env)


def test_flagged_request_records_sample_membership_without_adding_spot_check(env: SimpleNamespace) -> None:
    pol = policy(sample={"rate": 0.5, "seed": "s"})
    inside = next(f"doc{i}" for i in range(100) if pol.sample.selects(f"doc{i}"))
    outside = next(f"doc{i}" for i in range(100) if not pol.sample.selects(f"doc{i}"))
    a = parse_payload(env.desk.enqueue(request(inside, "flag_low"), pol).event.payload)
    b = parse_payload(env.desk.enqueue(request(outside, "flag_low"), pol).event.payload)
    assert (a.causes, a.sampled, b.sampled) == (("flag_low",), True, False)


def test_sampling_off_puts_nothing_in_review(env: SimpleNamespace) -> None:
    assert all(env.desk.enqueue(request(f"d{i}"), env.policy) is None for i in range(50))
    assert review_events(env.root) == []


# --- idempotency --------------------------------------------------------------------


def test_second_enqueue_of_a_document_in_review_is_refused(env: SimpleNamespace) -> None:
    first = env.desk.enqueue(request("d1", "flag_low"), env.policy)
    before = segment_files(env.root)
    with pytest.raises(AlreadyInReview) as info:
        env.desk.enqueue(request("d1", "flag_high"), env.policy)
    assert (info.value.doc_id, info.value.seq) == ("d1", first.event.seq)
    assert segment_files(env.root) == before
    assert env.desk.case("r1", "d1").causes == ("flag_low",)


def test_a_document_reenters_review_after_a_non_review_event(env: SimpleNamespace) -> None:
    env.desk.enqueue(request("d1", "flag_low"), env.policy)
    engine_append(env.root, "r1", "d1", "output")  # a resolution: the head is no longer a review
    assert env.desk.case("r1", "d1").status == "resolved"
    again = env.desk.enqueue(request("d1", "flag_high"), env.policy)
    assert again.case.status == "open" and again.case.causes == ("flag_high",)
    assert again.case.opened_seq == 2
    verify_ok(env)


def test_engine_review_event_counts_as_in_review(env: SimpleNamespace) -> None:
    # A review event the desk did not write (any payload) still makes the head a review.
    engine_append(env.root, "r1", "d1", "review", payload={"note": "parked elsewhere"})
    with pytest.raises(AlreadyInReview):
        env.desk.enqueue(request("d1", "flag_low"), env.policy)


# --- sealed runs ----------------------------------------------------------------------


def test_sealed_experiment_run_is_refused(env: SimpleNamespace) -> None:
    engine_append(env.root, "r1", "d1", "decision")
    seal_within(env.log, "r1")
    before = segment_files(env.root)
    with pytest.raises(RunClosed) as info:
        env.desk.enqueue(request("d1", "flag_low"), env.policy)
    assert (info.value.run_id, info.value.kind) == ("r1", "specialist_only")
    with pytest.raises(RunClosed):  # refused even when the sample would not select it
        env.desk.enqueue(request("d1"), env.policy)
    assert segment_files(env.root) == before
    assert env.desk.is_closed("r1")
    verify_ok(env)


def test_unsealed_experiment_and_sealed_service_runs_stay_open(tmp_path: Path) -> None:
    make_run(tmp_path, "svc", "service_period")
    make_run(tmp_path, "exp", "full_pipeline")
    log = log_of(tmp_path)
    engine_append(tmp_path, "svc", "d1")
    seal_within(log, "svc")
    desk = Desk(tmp_path)
    pol = policy(sample={"rate": 0.0, "seed": ""})
    assert not desk.is_closed("svc") and not desk.is_closed("exp")
    # A service-period run is never closed, so the desk does not take its seal lock: a
    # timer seal in progress does not hold the desk up (and the desk never holds it up).
    with seal_guard(tmp_path, "svc", timeout=1):
        assert Desk(tmp_path, lock_timeout=0.1).enqueue(request("d1", "flag_low", run_id="svc"), pol)
    assert desk.enqueue(request("d1", "flag_low", run_id="exp"), pol)
    seal_within(log, "svc")
    assert verify_run(log, log.blobs, "svc").ok


def test_seal_cannot_land_between_the_closed_check_and_the_append(env: SimpleNamespace) -> None:
    """The desk holds the record's seal lock while it checks and appends: a seal_run started
    meanwhile waits, then seals the review event, and the next enqueue is refused."""
    engine_append(env.root, "r1", "d0")
    entered = threading.Event()
    sealed: list[object] = []

    def clock():
        entered.set()
        time.sleep(0.4)  # seal_run starts now and must wait for the desk
        return T0

    def sealer() -> None:
        entered.wait(5)
        sealed.append(seal_run(env.log, "r1"))

    t = threading.Thread(target=sealer, daemon=True)
    t.start()
    done = Desk(env.root, clock=clock).enqueue(request("d1", "flag_low"), env.policy)
    t.join(10)
    assert sealed and done is not None
    seal = sealed[0]
    assert seal.doc_heads["d1"] == done.event.hash  # the seal came after the append
    with pytest.raises(RunClosed):
        env.desk.enqueue(request("d2", "flag_low"), env.policy)
    verify_ok(env)


def test_seal_landing_while_waiting_for_the_desk_lock_is_seen_under_the_lock(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run is open at the first check, then sealed while the enqueue waits for the desk
    lock (another desk action holds it; the seal lock is still free). The check repeated
    under the locks must refuse it: nothing is appended to the sealed run."""
    import mailroom_desk.desk as desk_module

    engine_append(env.root, "r1", "d0")
    waiting = threading.Event()
    real_lock = desk_module.desk_lock

    def announcing_lock(*args, **kwargs):  # called after the first check, before the wait
        waiting.set()
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(desk_module, "desk_lock", announcing_lock)
    outcome: list[BaseException | object] = []

    def enqueue() -> None:
        try:
            outcome.append(
                Desk(env.root, clock=env.clock, lock_timeout=30).enqueue(
                    request("d1", "flag_low"), env.policy
                )
            )
        except BaseException as exc:  # noqa: BLE001 - asserted below
            outcome.append(exc)

    with desk_lock(env.root, "r1", timeout=1):
        t = threading.Thread(target=enqueue, daemon=True)
        t.start()
        assert waiting.wait(10), "the enqueue never reached the desk lock"
        seal_within(env.log, "r1")  # the enqueue holds no lock yet, so the seal proceeds
        assert t.is_alive()  # still waiting for the desk lock held here
        before = segment_files(env.root)
    t.join(30)
    assert len(outcome) == 1 and isinstance(outcome[0], RunClosed), outcome
    assert segment_files(env.root) == before
    assert review_events(env.root) == []
    verify_ok(env)


def test_seal_guard_is_the_lock_seal_run_takes(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the record layout the desk relies on: ``seal_run`` flocks the very file
    ``seal_guard`` holds, so it waits while the guard is held. No sleep: the test waits
    (with a deadline) until ``seal_run`` reaches its ``flock`` and compares the inode it
    locks with the guard's, and a non-blocking ``flock`` from a fresh descriptor proves
    the guard holds an exclusive lock on that inode."""
    import fcntl

    import mailroom_record.seal as seal_module

    engine_append(env.root, "r1", "d1")
    reached = threading.Event()
    locked_inodes: list[tuple[int, int]] = []

    class Spy:
        def __getattr__(self, name: str):
            return getattr(fcntl, name)

        def flock(self, fd: int, op: int) -> None:
            st = os.fstat(fd)
            locked_inodes.append((st.st_dev, st.st_ino))
            reached.set()
            fcntl.flock(fd, op)

    monkeypatch.setattr(seal_module, "fcntl", Spy())
    done = threading.Event()
    with seal_guard(env.root, "r1", timeout=1):
        guard_file = seal_lock_path(env.root, "r1")
        probe = os.open(guard_file, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):  # the guard holds it exclusively
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            st = os.fstat(probe)
        finally:
            os.close(probe)
        t = threading.Thread(target=lambda: (seal_run(env.log, "r1"), done.set()), daemon=True)
        t.start()
        assert reached.wait(10), "seal_run never reached its flock"
        assert locked_inodes == [(st.st_dev, st.st_ino)]  # the same file: seal_run is blocked on the guard
        assert not done.is_set()
    t.join(10)
    assert done.is_set()


@LINUX_ONLY
def test_seal_guard_creates_the_seals_directory_durably(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``seal_run`` fsyncs the run directory only when it creates ``seals/`` itself, so the
    guard must do it when it creates the directory first: ``seals/`` and the run directory
    are fsynced after the ``mkdir`` and before the lock is taken, on every call. What a test
    cannot show is the durability itself (that needs a power cut); this pins the calls."""
    import mailroom_desk.lock as lock_module

    run_dir = env.root / "runs" / "r1"
    seals = run_dir / "seals"
    assert not seals.exists()
    synced: list[tuple[str, bool]] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        synced.append((str(path.relative_to(env.root)), seals.is_dir()))
        real_fsync(fd)

    monkeypatch.setattr(lock_module.os, "fsync", spy_fsync)
    with seal_guard(env.root, "r1", timeout=1):
        pass
    assert synced == [("runs/r1/seals", True), ("runs/r1", True)]
    synced.clear()
    with seal_guard(env.root, "r1", timeout=1):  # an existing directory is fsynced again
        pass
    assert synced == [("runs/r1/seals", True), ("runs/r1", True)]
    monkeypatch.undo()
    assert env.desk.enqueue(request("d1", "flag_low"), env.policy)
    seal_within(env.log, "r1")
    verify_ok(env)


def test_seal_guard_on_a_missing_run_directory_creates_nothing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError), seal_guard(tmp_path, "nope", timeout=0):
        pass
    assert list(tmp_path.iterdir()) == []


def test_seal_lock_held_too_long_times_out(env: SimpleNamespace) -> None:
    with seal_guard(env.root, "r1", timeout=1), pytest.raises(DeskLockTimeout) as info:
        Desk(env.root, lock_timeout=0.1).enqueue(request("d1", "flag_low"), env.policy)
    assert info.value.lock == "seal"
    assert review_events(env.root) == []


# --- damaged and busy records ---------------------------------------------------------


def test_torn_tail_in_the_desk_segment_is_skipped_and_writing_continues(env: SimpleNamespace) -> None:
    env.desk.enqueue(request("d1", "flag_low"), env.policy)
    seg = env.root / "runs" / "r1" / "segments" / "desk.jsonl"
    whole = seg.read_bytes()
    # A crash mid-append of d2's enqueue: half a line, no newline.
    torn = json.dumps({"run_id": "r1", "doc_id": "d2", "seq": 0, "kind": "review"}).encode()[:30]
    with seg.open("ab") as f:
        f.write(torn)
    assert env.log.torn_tails("r1")
    assert [c.doc_id for c in env.desk.queue("r1")] == ["d1"]  # the torn line is not an event
    done = env.desk.enqueue(request("d2", "flag_low"), env.policy)
    assert done.event.seq == 0
    assert seg.read_bytes() == whole + torn  # the torn bytes stay where they are
    assert (seg.parent / "desk.1.jsonl").is_file()
    assert sorted(c.doc_id for c in env.desk.queue("r1")) == ["d1", "d2"]
    report = verify_run(env.log, env.blobs, "r1")
    assert report.ok
    assert [p.kind for p in report.problems] == ["torn_tail"]


def test_torn_tail_in_another_writers_segment_does_not_block_the_desk(env: SimpleNamespace) -> None:
    engine_append(env.root, "r1", "d1")
    with (env.root / "runs" / "r1" / "segments" / "w0.jsonl").open("ab") as f:
        f.write(b'{"run_id": "r1", "doc_id": "d1", "se')
    done = env.desk.enqueue(request("d1", "flag_low"), env.policy)
    assert done.event.seq == 1
    assert verify_run(env.log, env.blobs, "r1").ok


def test_corrupt_line_is_refused_as_damage(env: SimpleNamespace) -> None:
    engine_append(env.root, "r1", "d1")
    with (env.root / "runs" / "r1" / "segments" / "w0.jsonl").open("ab") as f:
        f.write(b"this is not json\n")
    before = segment_files(env.root)
    with pytest.raises(RecordDamaged) as info:
        env.desk.enqueue(request("d2", "flag_low"), env.policy)
    assert info.value.run_id == "r1"
    with pytest.raises(RecordDamaged):
        env.desk.queue("r1")
    with pytest.raises(RecordDamaged):
        env.desk.case("r1", "d1")
    assert segment_files(env.root) == before
    # The lock was released: taking it now does not wait.
    with desk_lock(env.root, "r1", timeout=0):
        pass


def test_unreadable_run_header_is_damage(env: SimpleNamespace) -> None:
    (env.root / "runs" / "r1" / "run.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RecordDamaged):
        env.desk.enqueue(request("d1", "flag_low"), env.policy)


def test_forked_chain_is_refused_as_damage(env: SimpleNamespace) -> None:
    engine_append(env.root, "r1", "d1")
    seg = env.root / "runs" / "r1" / "segments"
    line = (seg / "w0.jsonl").read_bytes()
    (seg / "w9.jsonl").write_bytes(line)  # a second owner wrote seq 0 of d1
    with pytest.raises(RecordDamaged) as info:
        env.desk.enqueue(request("d1", "flag_low"), env.policy)
    assert type(info.value.cause).__name__ == "ForkedChain"


def test_document_held_by_a_live_writer_in_this_process_is_busy(env: SimpleNamespace) -> None:
    with env.log.open_writer("r1", "engine") as w:
        w.append(doc_id="d1", node="step", kind="decision")
        w.flush()
        with pytest.raises(RecordBusy) as info:
            env.desk.enqueue(request("d1", "flag_low"), env.policy)
        assert info.value.doc_id == "d1"
    assert env.desk.enqueue(request("d1", "flag_low"), env.policy)  # after the engine closed
    verify_ok(env)


def test_desk_writer_id_held_elsewhere_is_busy(env: SimpleNamespace) -> None:
    with env.log.open_writer("r1", WRITER_ID), pytest.raises(RecordBusy):
        env.desk.enqueue(request("d1", "flag_low"), env.policy)
    assert review_events(env.root) == []


# --- the lock ---------------------------------------------------------------------------


def test_lock_timeout_refuses_and_appends_nothing(env: SimpleNamespace) -> None:
    with desk_lock(env.root, "r1", timeout=1):
        started = time.monotonic()
        with pytest.raises(DeskLockTimeout) as info:
            Desk(env.root, lock_timeout=0.2).enqueue(request("d1", "flag_low"), env.policy)
        assert time.monotonic() - started >= 0.2
    assert (info.value.run_id, info.value.timeout, info.value.lock) == ("r1", 0.2, "desk")
    assert review_events(env.root) == []
    assert env.desk.enqueue(request("d1", "flag_low"), env.policy)  # released: proceeds


@pytest.mark.parametrize("bad", [-1, float("inf"), float("nan"), "1", True, None])
def test_bad_lock_timeouts_are_refused(env: SimpleNamespace, bad: object) -> None:
    with pytest.raises(ValueError), desk_lock(env.root, "r1", timeout=bad):  # type: ignore[arg-type]
        pass
    with pytest.raises(ValueError):
        Desk(env.root, lock_timeout=bad)  # type: ignore[arg-type]


def test_lock_is_released_after_every_outcome(env: SimpleNamespace) -> None:
    quick = Desk(env.root, clock=env.clock, lock_timeout=0)
    quick.enqueue(request("d1", "flag_low"), env.policy)
    with pytest.raises(AlreadyInReview):
        quick.enqueue(request("d1", "flag_low"), env.policy)
    engine_append(env.root, "r1", "d2", "review")
    with pytest.raises(AlreadyInReview):
        quick.enqueue(request("d2", "flag_low"), env.policy)
    assert quick.enqueue(request("d3", "flag_low"), env.policy)


def test_threads_of_one_process_are_serialised(env: SimpleNamespace) -> None:
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait()
        try:
            Desk(env.root, lock_timeout=30).enqueue(request("same", "flag_low"), env.policy)
            result = "enqueued"
        except AlreadyInReview:
            result = "already"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(outcomes) == ["already"] * 5 + ["enqueued"]
    assert len(review_events(env.root)) == 1
    verify_ok(env)


# --- index ------------------------------------------------------------------------------


def test_enqueue_applies_to_the_index(env: SimpleNamespace) -> None:
    index = Index(env.root / "index.sqlite")
    index.reindex(env.log)
    desk = Desk(env.root, index=index, clock=env.clock)
    done = desk.enqueue(request("d1", "flag_low"), env.policy)
    assert done.index_error is None
    assert [r.doc_id for r in index.review_queue("r1")] == ["d1"]
    assert index.drift(env.log) == []
    assert [c.doc_id for c in desk.queue()] == ["d1"]


def test_index_refusal_is_reported_not_raised(env: SimpleNamespace) -> None:
    index = Index(env.root / "index.sqlite")
    index.reindex(env.log)
    make_run(env.root, "r2")  # created after the rebuild and never add_run'd
    desk = Desk(env.root, index=index, clock=env.clock)
    done = desk.enqueue(request("d1", "flag_low", run_id="r2"), env.policy)
    assert done.index_error is not None and "unknown_run" in done.index_error
    assert doc_events(env.root, "r2", "d1")[-1] == done.event  # the event is durable anyway
    index.reindex(env.log)
    assert [r.doc_id for r in index.review_queue("r2")] == ["d1"]


def test_stale_index_changes_no_answer(env: SimpleNamespace) -> None:
    """An index behind the log (events it was never given) changes nothing the desk
    answers: the queue and the case come from the log; ``index_drift`` names the run."""
    make_run(env.root, "r2")
    index = Index(env.root / "index.sqlite")
    index.reindex(env.log)
    indexed = Desk(env.root, index=index, clock=env.clock)
    assert indexed.index_drift() == []
    env.desk.enqueue(request("d1", "flag_low"), env.policy)  # a desk without the index
    engine_append(env.root, "r1", "d2", "review", payload={"note": "parked elsewhere"})
    indexed.enqueue(request("d3", "flag_high", run_id="r2"), env.policy)  # applied: r2 in step
    assert index.review_queue("r1") == []  # the index really is behind
    cases = indexed.queue()
    assert [(c.run_id, c.doc_id) for c in cases] == [("r1", "d2"), ("r2", "d3"), ("r1", "d1")]
    assert [c.doc_id for c in indexed.queue("r1")] == ["d2", "d1"]
    assert indexed.queue() == Desk(env.root, clock=env.clock).queue()
    assert indexed.case("r1", "d1") == env.desk.case("r1", "d1")
    assert indexed.index_drift() == ["r1: 2 document head(s) differ from the log"]
    assert indexed.index_drift("r2") == []
    index.reindex(env.log)
    assert indexed.index_drift() == [] and [c.doc_id for c in indexed.queue("r1")] == ["d2", "d1"]
    assert env.desk.index_drift() == []  # no index, nothing to drift


def test_index_behind_on_a_known_document_is_drift(env: SimpleNamespace) -> None:
    """The index knows every document of the run but holds an older head for one: the
    membership agrees, the heads do not. That is drift too, and the queue still shows the
    case the index has not seen."""
    index = Index(env.root / "index.sqlite")
    index.reindex(env.log)
    index.apply(engine_append(env.root, "r1", "d1", "decision"))
    indexed = Desk(env.root, index=index, clock=env.clock)
    assert indexed.index_drift() == [] and [r.doc_id for r in index.documents("r1")] == ["d1"]
    env.desk.enqueue(request("d1", "flag_low"), env.policy)  # a desk without the index
    assert [r.doc_id for r in index.documents("r1")] == ["d1"] and index.review_queue("r1") == []
    assert indexed.index_drift() == ["r1: 1 document head(s) differ from the log"]
    assert [c.doc_id for c in indexed.queue("r1")] == ["d1"]


def _break_index(index_path: Path) -> None:
    """Replace the index file by a directory: sqlite then fails with a raw
    ``sqlite3.OperationalError`` that the record does not translate."""
    for f in index_path.parent.glob(index_path.name + "*"):
        f.unlink()
    index_path.mkdir()


def test_broken_index_changes_no_answer(env: SimpleNamespace) -> None:
    """A directory where the index file should be: reading never touches the index, so the
    queue and the case are the log's; enqueue still appends durably and reports why the
    index refused; ``index_drift`` reports the index instead of raising."""
    index = Index(env.root / "index.sqlite")
    index.reindex(env.log)
    desk = Desk(env.root, index=index, clock=env.clock)
    desk.enqueue(request("d1", "flag_low"), env.policy)
    _break_index(env.root / "index.sqlite")
    assert index.status() == "corrupt"
    assert [c.doc_id for c in desk.queue("r1")] == ["d1"]
    assert desk.case("r1", "d1") == env.desk.case("r1", "d1")
    done = desk.enqueue(request("d2", "flag_high"), env.policy)
    assert done.index_error == "OperationalError: unable to open database file"  # not an ApplyRefused
    assert doc_events(env.root, "r1", "d2") == [done.event]
    assert [c.doc_id for c in desk.queue()] == ["d2", "d1"] == [c.doc_id for c in env.desk.queue()]
    assert desk.index_drift() == ["r1: index unavailable (OperationalError: unable to open database file)"]
    verify_ok(env)


def test_locked_index_changes_no_answer(env: SimpleNamespace) -> None:
    """Another connection holds the index exclusively (no busy wait: ``busy_timeout=0``):
    the queue and the case still answer from the log, and the apply's refusal is reported."""
    import sqlite3

    index = Index(env.root / "index.sqlite", busy_timeout=0)
    index.reindex(env.log)
    desk = Desk(env.root, index=index, clock=env.clock)
    desk.enqueue(request("d1", "flag_low"), env.policy)
    other = sqlite3.connect(env.root / "index.sqlite", isolation_level=None)
    try:
        other.execute("BEGIN EXCLUSIVE")
        done = desk.enqueue(request("d2", "flag_high"), env.policy)
        # The record's index may raise sqlite's own error (older record) or translate it into
        # IndexUnavailable (current record); either way the desk reports it, never raises.
        assert done.index_error is not None
        assert any(word in done.index_error for word in ("locked", "busy"))
        assert [c.doc_id for c in desk.queue("r1")] == ["d2", "d1"]
        assert desk.case("r1", "d2") == done.case
    finally:
        other.close()
    assert desk.index_drift() == ["r1: 1 document head(s) differ from the log"]  # d2 was never applied


def test_index_drift_reports_and_never_raises(env: SimpleNamespace) -> None:
    """``index_drift`` is a diagnostic: an unknown or invalid run, an index never built and
    a damaged log are problem lines, never exceptions."""
    env.desk.enqueue(request("d1", "flag_low"), env.policy)
    missing = Desk(env.root, index=Index(env.root / "never-built.sqlite"), clock=env.clock)
    assert missing.index_drift() == [
        f"r1: index unavailable (IndexUnavailable: index {env.root / 'never-built.sqlite'} "
        "is missing; run reindex)"
    ]
    assert [c.doc_id for c in missing.queue("r1")] == ["d1"]  # reading never needed it
    index = Index(env.root / "index.sqlite")
    index.reindex(env.log)
    desk = Desk(env.root, index=index, clock=env.clock)
    assert desk.index_drift("r9") == ["r9: unknown run"]
    assert desk.index_drift("../r1") == ["'../r1': unknown run"]
    assert desk.index_drift(7) == ["7: unknown run"]
    assert desk.index_drift("x" * 500) == ["'" + "x" * 36 + "...: unknown run"]
    with pytest.raises(UnknownRun):
        desk.queue("r9")
    seg = next((env.root / "runs" / "r1" / "segments").glob("*.jsonl"))
    seg.write_bytes(seg.read_bytes() + b"not json\n")
    assert desk.index_drift() == ["r1: log unreadable (CorruptLine)"]
    (env.root / "runs" / "r1" / "run.json").write_text("{", encoding="utf-8")
    assert desk.index_drift("r1") == ["r1: run header unreadable (JSONDecodeError)"]


# --- reading --------------------------------------------------------------------------


class _ReadCounter:
    """Wraps a desk's ``EventLog`` reads: every ``scan`` (``iter_events`` goes through it)
    and every head read is recorded with its arguments."""

    def __init__(self, desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple] = []
        for name in ("scan", "iter_stored", "doc_head_seqs", "iter_located"):
            real = getattr(desk.log, name)
            monkeypatch.setattr(desk.log, name, self._wrap(name, real))

    def _wrap(self, name: str, real):
        def counted(*args, **kwargs):
            self.calls.append((name, *args, *sorted(kwargs.items())))
            return real(*args, **kwargs)

        return counted


class _ApplyOnlyIndex:
    """An index the desk may apply events to but must never read to answer."""

    def __init__(self, index: Index) -> None:
        self._index = index
        self.applied = 0

    def apply(self, event) -> None:
        self.applied += 1
        self._index.apply(event)

    def __getattr__(self, name: str):
        raise AssertionError(f"the desk read the index ({name}) to answer")


def test_reading_scans_each_run_once_and_never_reads_the_index(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The work of a read is bounded by the runs, never by the cases: ``queue()`` scans each
    run's log exactly once (however many documents are in review), ``case()`` scans once,
    and neither touches the index. A per-case re-read or an index lookup fails here."""
    make_run(env.root, "r2")
    real = Index(env.root / "index.sqlite")
    real.reindex(env.log)
    index = _ApplyOnlyIndex(real)
    desk = Desk(env.root, index=index, clock=env.clock)  # type: ignore[arg-type]
    for i in range(12):
        real.apply(engine_append(env.root, "r1", f"d{i:02d}", "decision"))
        if i % 3:
            desk.enqueue(request(f"d{i:02d}", "flag_low"), env.policy)
    for i in range(5):
        desk.enqueue(request(f"e{i}", "flag_high", run_id="r2"), env.policy)
    assert index.applied == 13 and real.drift(env.log) == []
    counter = _ReadCounter(desk, monkeypatch)
    cases = desk.queue()
    assert len(cases) == 13
    assert counter.calls == [("scan", "r1", None), ("scan", "r2", None)]
    counter.calls.clear()
    assert len(desk.queue("r1", statuses=["open"])) == 8
    assert counter.calls == [("scan", "r1", None)]
    counter.calls.clear()
    assert desk.case("r1", "d01").doc_id == "d01"
    assert counter.calls == [("scan", "r1", "d01")]


def test_case_of_a_document_never_in_review(env: SimpleNamespace) -> None:
    engine_append(env.root, "r1", "d1")
    with pytest.raises(NoCase):
        env.desk.case("r1", "d1")
    with pytest.raises(NoCase):
        env.desk.case("r1", "never-seen")


def test_request_validation() -> None:
    good = {"run_id": "r1", "doc_id": "d1", "causes": ["flag_low"], "requested_by": "node:x"}
    ReviewRequest(**good)
    for bad in (
        {"causes": ["flag_low", "flag_low"]},
        {"causes": ["Flag"]},
        {"causes": "flag_low"},
        {"run_id": "../x"},
        {"doc_id": ""},
        {"requested_by": ""},
        {"requested_by": " padded"},
        {"confidence_hint": 1.5},
        {"confidence_hint": "0.5"},
        {"class_hint": "line\nbreak"},
        {"shown": [{"sha256": "x", "role": "original", "size": 1}]},
        {"extra": 1},
    ):
        with pytest.raises(ValueError):
            ReviewRequest(**(good | bad))


# --- the edges of time and a full disk ------------------------------------------------

END = datetime(9999, 12, 31, 23, 0, tzinfo=UTC)


def test_enqueue_at_the_end_of_time_returns_its_event(env: SimpleNamespace) -> None:
    """A clock near datetime.max: the SLA due time saturates, and enqueue (which has
    appended durably by then) returns instead of raising."""
    env.clock.moment = END
    done = env.desk.enqueue(request("d1", "flag_low"), env.policy)  # 72 h past the end
    assert done.event.ts == "9999-12-31T23:00:00.000000Z"
    assert doc_events(env.root, "r1", "d1") == [done.event]
    assert done.case.sla_due == datetime.max.replace(tzinfo=UTC)
    assert any("saturated" in a for a in done.case.anomalies)
    assert not done.case.breached(END)
    verify_ok(env)


def test_one_far_future_event_does_not_take_down_the_queue(env: SimpleNamespace) -> None:
    env.desk.enqueue(request("d1", "flag_high"), env.policy)
    far = EnqueuePayload(
        action="enqueue",
        causes=("flag_low",),
        rule="default",
        priority="low",
        sla_hours=72,
        queue="main",
        sampled=False,
        policy_name="synthetic",
        policy_hash=env.policy.hash,
        requested_by="node:x",
    )
    with env.log.open_writer("r1", "other") as w:  # a record-valid event from another writer
        w.append(
            doc_id="d2", node=NODE, kind="review", payload=far.model_dump(mode="json"), ts=format_ts(END)
        )
    cases = env.desk.queue("r1")
    assert [c.doc_id for c in cases] == ["d1", "d2"]
    assert cases[1].sla_due == datetime.max.replace(tzinfo=UTC) and cases[1].anomalies


def test_enqueue_at_the_start_of_time_writes_a_valid_timestamp(env: SimpleNamespace) -> None:
    env.clock.moment = datetime(5, 1, 1, tzinfo=UTC)
    done = env.desk.enqueue(request("d1", "flag_low"), env.policy)
    assert done.event.ts == "0005-01-01T00:00:00.000000Z"
    verify_ok(env)


def test_close_failing_after_a_good_flush_does_not_raise(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the flush succeeded the event is durable, so an ``OSError`` from closing the
    writer (closing a descriptor can report a late error) must not make ``enqueue``
    raise: it returns the event, the lock is free and the record verifies."""
    import errno

    from mailroom_record.log import LogWriter

    real_close = LogWriter.close
    fired: list[bool] = []

    def failing_close(self: LogWriter) -> None:
        real_close(self)
        fired.append(True)
        raise OSError(errno.EIO, "injected EIO on close")

    monkeypatch.setattr(LogWriter, "close", failing_close)
    done = env.desk.enqueue(request("d1", "flag_low"), env.policy)
    assert fired and done is not None
    monkeypatch.undo()
    assert [e.hash for e in review_events(env.root)] == [done.event.hash]
    with desk_lock(env.root, "r1", timeout=0):
        pass
    assert env.desk.enqueue(request("d2", "flag_low"), env.policy)
    verify_ok(env)


def test_read_error_while_appending_is_damage_not_a_write_failure(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LogWriter.append`` only reads (the chain head) and buffers, so an ``OSError`` there
    is a record the desk cannot read: ``RecordDamaged``, nothing written, the lock free."""
    import errno

    from mailroom_record.log import LogWriter

    def failing_append(self: LogWriter, **kwargs) -> None:
        raise OSError(errno.EIO, "injected EIO reading the chain head")

    monkeypatch.setattr(LogWriter, "append", failing_append)
    with pytest.raises(RecordDamaged) as info:
        env.desk.enqueue(request("d1", "flag_low"), env.policy)
    assert isinstance(info.value.__cause__, OSError) and info.value.__cause__.errno == errno.EIO
    monkeypatch.undo()
    assert review_events(env.root) == []
    with desk_lock(env.root, "r1", timeout=0):
        pass
    assert env.desk.enqueue(request("d1", "flag_low"), env.policy).event.seq == 0
    verify_ok(env)


@LINUX_ONLY
def test_full_disk_on_flush_is_a_typed_error_and_leaves_the_record_usable(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import os

    real_write = os.write
    fired: list[int] = []

    def full_disk(fd: int, data):  # only the record's segment files fail
        if os.readlink(f"/proc/self/fd/{fd}").endswith(".jsonl"):
            fired.append(fd)
            raise OSError(errno.ENOSPC, "injected ENOSPC in os.write")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", full_disk)
    with pytest.raises(RecordWriteFailed) as info:
        env.desk.enqueue(request("d1", "flag_low"), env.policy)
    assert fired
    assert isinstance(info.value.__cause__, OSError) and info.value.__cause__.errno == errno.ENOSPC
    assert (info.value.run_id, info.value.doc_id) == ("r1", "d1")
    monkeypatch.setattr(os, "write", real_write)
    assert review_events(env.root) == []  # nothing reached the segment
    with desk_lock(env.root, "r1", timeout=0):  # the lock was released
        pass
    assert env.desk.enqueue(request("d1", "flag_low"), env.policy).event.seq == 0
    verify_ok(env)
