"""Real OS processes: racing enqueues, a desk process killed while it holds the lock.

Children are spawned (a fresh interpreter, no inherited state) and run targets from
``desk_testkit``. The racing children read the desk clock under the lock between the
in-review check and the append, and that clock sleeps, so the window a missing lock would
expose is wide on every run, not only on a lucky schedule.
"""

from __future__ import annotations

import contextlib
import multiprocessing as mp
import os
import signal
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest

from desk_testkit import (
    engine_writes_across_a_handoff,
    enqueue_in_child,
    enqueue_then_hang,
    hold_desk_lock,
    log_of,
    make_run,
    policy,
    request,
    review_events,
)
from mailroom_desk import AlreadyInReview, Desk, DeskLockTimeout, RecordDamaged, desk_lock
from mailroom_record.verify import verify_run

CTX = mp.get_context("spawn")


def _run_children(root: Path, plans: list[list[str]], pause: float) -> list[tuple[str, str]]:
    barrier = CTX.Barrier(len(plans))
    results = CTX.Queue()
    procs = [
        CTX.Process(target=enqueue_in_child, args=(str(root), "r1", docs, barrier, results, pause))
        for docs in plans
    ]
    for p in procs:
        p.start()
    out = [results.get(timeout=120) for _ in range(sum(len(d) for d in plans))]
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    return out


def _verify_clean(root: Path) -> None:
    log = log_of(root)
    report = verify_run(log, log.blobs, "r1")
    assert report.ok and [p for p in report.problems if p.fails] == []


def test_two_processes_enqueueing_one_document_enqueue_it_once(tmp_path: Path) -> None:
    make_run(tmp_path)
    docs = ["same-1", "same-2", "same-3"]
    out = _run_children(tmp_path, [docs, docs], pause=0.15)
    by_doc: dict[str, Counter] = {d: Counter() for d in docs}
    for doc, outcome in out:
        by_doc[doc][outcome] += 1
    for doc in docs:
        assert by_doc[doc] == Counter({"enqueued": 1, "already": 1}), (doc, by_doc[doc])
    events = review_events(tmp_path)
    assert sorted(e.doc_id for e in events) == docs  # exactly one review event each
    assert all(e.seq == 0 for e in events)
    _verify_clean(tmp_path)


def test_processes_enqueueing_different_documents_all_succeed(tmp_path: Path) -> None:
    make_run(tmp_path)
    plans = [[f"p{i}-d{j}" for j in range(5)] for i in range(4)]
    out = _run_children(tmp_path, plans, pause=0.01)
    assert sorted(out) == sorted((d, "enqueued") for plan in plans for d in plan)
    queued = Desk(tmp_path).queue("r1")
    assert sorted(c.doc_id for c in queued) == sorted(d for plan in plans for d in plan)
    _verify_clean(tmp_path)


@contextlib.contextmanager
def _reaped(proc: mp.process.BaseProcess) -> Iterator[None]:
    """Kill and join ``proc`` on the way out, whatever happened inside. The hanging children
    sleep for 600 s; a non-daemon child left alive by a failed assertion would hold the
    suite (multiprocessing joins it at exit) until it woke."""
    try:
        yield
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(30)


def _wait_for(path: Path, proc: mp.Process, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert proc.is_alive(), f"child exited early with {proc.exitcode}"
        assert time.monotonic() < deadline, "child never reached the marker"
        time.sleep(0.01)


def test_desk_killed_between_lock_and_append_leaves_a_clean_record_and_no_lock(tmp_path: Path) -> None:
    log = make_run(tmp_path)
    marker = tmp_path / "holding"
    child = CTX.Process(target=enqueue_then_hang, args=(str(tmp_path), "r1", "d1", str(marker)))
    child.start()
    with _reaped(child):
        _wait_for(marker, child)
        # The child is inside enqueue, holding the desk lock (and the seal lock), checked and
        # about to append. Positive control: the lock really is held now.
        with pytest.raises(DeskLockTimeout), desk_lock(tmp_path, "r1", timeout=0.1):
            pass
        os.kill(child.pid, signal.SIGKILL)
        child.join(30)
        assert child.exitcode == -signal.SIGKILL
    # The kernel released both locks; nothing was appended; the next process proceeds.
    with desk_lock(tmp_path, "r1", timeout=2):
        pass
    assert review_events(tmp_path) == []
    _verify_clean(tmp_path)
    done = Desk(tmp_path, lock_timeout=2).enqueue(
        request("d1", "flag_low"), policy(sample={"rate": 0.0, "seed": ""})
    )
    assert done.event.seq == 0
    with pytest.raises(AlreadyInReview):
        Desk(tmp_path, lock_timeout=2).enqueue(
            request("d1", "flag_low"), policy(sample={"rate": 0.0, "seed": ""})
        )
    _verify_clean(tmp_path)
    assert verify_run(log, log.blobs, "r1").ok


def test_lock_held_by_another_process_times_out_then_frees_on_its_death(tmp_path: Path) -> None:
    make_run(tmp_path)
    marker = tmp_path / "held"
    child = CTX.Process(target=hold_desk_lock, args=(str(tmp_path), "r1", str(marker)))
    child.start()
    pol = policy(sample={"rate": 0.0, "seed": ""})
    with _reaped(child):
        _wait_for(marker, child)
        started = time.monotonic()
        with pytest.raises(DeskLockTimeout):
            Desk(tmp_path, lock_timeout=0.3).enqueue(request("d1", "flag_low"), pol)
        assert time.monotonic() - started >= 0.3
        assert review_events(tmp_path) == []
        os.kill(child.pid, signal.SIGKILL)
        child.join(30)
        assert child.exitcode == -signal.SIGKILL
    assert Desk(tmp_path, lock_timeout=5).enqueue(request("d1", "flag_low"), pol)
    _verify_clean(tmp_path)


def test_a_child_forked_without_exec_keeps_the_lock_after_the_parent_lets_go(tmp_path: Path) -> None:
    """``flock`` belongs to the open file description, which ``fork`` shares: a child forked
    while the desk lock is held holds it too, until it exits, even after the parent has
    released it. Hence the rule (lock.py, README): never fork while holding a desk lock;
    use spawn or exec. The child only blocks on a pipe and calls ``os._exit``."""
    import warnings

    make_run(tmp_path)
    r, w = os.pipe()
    with desk_lock(tmp_path, "r1", timeout=1):
        with warnings.catch_warnings():  # Python warns about fork in a threaded process
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:  # the child: wait until the parent closes the pipe, then exit
            try:
                os.close(w)
                os.read(r, 1)
            finally:
                os._exit(0)
        os.close(r)
    try:
        # The parent closed its descriptor; the child's holds the lock.
        with pytest.raises(DeskLockTimeout), desk_lock(tmp_path, "r1", timeout=0):
            pass
    finally:
        os.close(w)
        _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    with desk_lock(tmp_path, "r1", timeout=0):  # the child exited: free
        pass


def test_a_spawned_child_does_not_inherit_the_lock(tmp_path: Path) -> None:
    """Positive control for the rule above: a child started with ``spawn`` (fork and exec)
    while the lock is held gets none of its descriptors, so releasing frees it at once."""
    make_run(tmp_path)
    with desk_lock(tmp_path, "r1", timeout=1):
        child = CTX.Process(target=time.sleep, args=(600,))
        child.start()
    try:
        assert child.is_alive()
        with desk_lock(tmp_path, "r1", timeout=0):
            pass
    finally:
        child.kill()
        child.join(30)


def test_a_writer_in_another_process_breaking_the_one_owner_rule_is_caught_as_a_fork(
    tmp_path: Path,
) -> None:
    """The desk lock serializes desk actions only. Across processes the record leaves
    document ownership to its callers (``mailroom_record.log``, "One owner per document"):
    the engine closes its writer for a document before handing it to the desk and never
    writes it while it is in review. This test breaks that rule on purpose and pins what
    follows: the chain forks, and every desk read and action refuses it as damage."""
    make_run(tmp_path)
    marker, go = tmp_path / "buffered", tmp_path / "go"
    child = CTX.Process(
        target=engine_writes_across_a_handoff, args=(str(tmp_path), "r1", "d1", str(marker), str(go))
    )
    child.start()
    with _reaped(child):
        _wait_for(marker, child)
        # The engine holds an unflushed seq 0 of d1; the desk sees no events and appends its own.
        pol = policy(sample={"rate": 0.0, "seed": ""})
        desk = Desk(tmp_path, lock_timeout=5)
        assert desk.enqueue(request("d1", "flag_low"), pol).event.seq == 0
        go.write_text("go", encoding="utf-8")
        child.join(60)
        assert child.exitcode == 0
    log = log_of(tmp_path)
    assert sorted(e.seq for e in log.iter_events("r1", "d1")) == [0, 0]  # forked
    assert "forked_chain" in {p.kind for p in verify_run(log, log.blobs, "r1").problems}
    for act in (
        lambda: desk.case("r1", "d1"),
        lambda: desk.queue("r1"),
        lambda: desk.queue(),
        lambda: desk.enqueue(request("d1", "flag_high"), pol),
    ):
        with pytest.raises(RecordDamaged) as info:
            act()
        assert type(info.value.cause).__name__ == "ForkedChain"
