"""The append-only event log: per-document chains, torn tails, concurrency, crash safety."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from record_testkit import (
    append_in_child,
    append_until_killed,
    check_chains,
    run_fields,
    segment_lines,
    stored_v0_event_dict,
)

from mailroom_record.hashing import canonical_json
from mailroom_record.log import (
    BlobNotStored,
    DocumentInUse,
    EventLog,
    ForkedChain,
    RunExists,
    TornTail,
    WriterInUse,
)
from mailroom_record import log as log_module
from mailroom_record.models import GENESIS, BlobRef, Event, Run, event_hash

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def seg_dir(store: SimpleNamespace, run_id: str = "r1") -> Path:
    return store.root / "runs" / run_id / "segments"


# --- the plan's tests -------------------------------------------------------------


def test_chain_links_per_document(store: SimpleNamespace) -> None:
    w = store.log.open_writer("r1", "w0")
    a = w.append(doc_id="d1", node="classify", kind="decision")
    b = w.append(doc_id="d1", node="extract", kind="output")
    c = w.append(doc_id="d2", node="classify", kind="decision")
    assert (a.seq, b.seq, c.seq) == (0, 1, 0)
    assert a.prev_hash == GENESIS and b.prev_hash == a.hash and c.prev_hash == GENESIS


def test_event_before_blob_is_refused(store: SimpleNamespace) -> None:
    ghost = BlobRef(sha256="1" * 64, role="extraction", size=3)
    with pytest.raises(BlobNotStored):
        store.log.open_writer("r1", "w0").append(doc_id="d1", node="extract", kind="output", outputs=[ghost])


def test_torn_tail_is_reported_and_chain_continues(store: SimpleNamespace) -> None:  # Review Focus 1
    w = store.log.open_writer("r1", "w0")
    a = w.append(doc_id="d1", node="n", kind="decision")
    w.close()
    seg = next(seg_dir(store).iterdir())
    size = seg.stat().st_size
    with seg.open("ab") as f:
        f.write(b'{"run_id": "r1", "doc_')  # crash mid-line
    assert store.log.torn_tails("r1") == [TornTail(segment=seg.name, byte_offset=size)]
    assert [e.hash for e in store.log.iter_events("r1")] == [a.hash]
    w2 = store.log.open_writer("r1", "w1")
    assert w2.append(doc_id="d1", node="n2", kind="decision").prev_hash == a.hash


def test_parallel_documents_keep_their_chains(store: SimpleNamespace) -> None:  # Review Focus 4
    writers, docs_per_writer, events_per_doc = 8, 4, 50
    errors: list[BaseException] = []
    barrier = threading.Barrier(writers)

    def go(k: int) -> None:
        try:
            docs = [f"t{k}-d{j}" for j in range(docs_per_writer)]
            barrier.wait()
            with store.log.open_writer("r1", f"w{k}") as w:
                for i in range(events_per_doc):
                    for doc in docs:
                        w.append(doc_id=doc, node=f"n{i}", kind="decision")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(k,)) for k in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    all_doc_ids = [f"t{k}-d{j}" for k in range(writers) for j in range(docs_per_writer)]
    for doc_id in all_doc_ids:
        events = list(store.log.iter_events("r1", doc_id))
        assert [e.seq for e in events] == list(range(events_per_doc))
        assert all(e.prev_hash == p.hash for p, e in zip(events, events[1:]))
    assert check_chains(store.root) == {d: events_per_doc for d in all_doc_ids}


def test_run_header_is_write_once(store: SimpleNamespace) -> None:
    with pytest.raises(RunExists):
        store.log.create_run(store.run)


# --- concurrency beyond the sketch --------------------------------------------------


def test_parallel_processes_keep_their_chains(store: SimpleNamespace) -> None:  # Review Focus 4
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(
            target=append_in_child,
            args=(str(store.root), "r1", f"p{k}", [f"p{k}-d{j}" for j in range(4)], 50),
        )
        for k in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0
    counts = check_chains(store.root)
    assert counts == {f"p{k}-d{j}": 50 for k in range(4) for j in range(4)}
    assert store.log.torn_tails("r1") == []
    assert len(list(seg_dir(store).glob("*.jsonl"))) == 4  # one file per writer process


def test_threads_sharing_one_writer_serialize(store: SimpleNamespace) -> None:
    w = store.log.open_writer("r1", "w0", flush_every=3)
    errors: list[BaseException] = []

    def go(k: int) -> None:
        try:
            for i in range(40):
                w.append(doc_id=f"s{k}", node=f"n{i}", kind="handoff")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    w.close()
    assert errors == []
    assert check_chains(store.root) == {f"s{k}": 40 for k in range(6)}


def test_killed_writer_process_leaves_a_verifiable_log(store: SimpleNamespace) -> None:
    docs = [f"k-d{j}" for j in range(3)]
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=append_until_killed, args=(str(store.root), "r1", "w0", docs, 32))
    proc.start()
    seg = seg_dir(store) / "w0.jsonl"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and (not seg.exists() or seg.stat().st_size < 32 * 1024 * 1024):
        time.sleep(0.01)
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(timeout=30)
    assert proc.exitcode == -signal.SIGKILL

    # Every whole line verifies (hash and link); a torn tail, if the kill landed mid-write
    # (most runs: each flush is one ~32 MB write), is reported at the end of the segment.
    # Buffered events that never reached disk are simply absent.
    before = check_chains(store.root)
    assert sum(before.values()) >= 3
    data = seg.read_bytes()
    torn = store.log.torn_tails("r1")
    if data.endswith(b"\n"):
        assert torn == []
    else:
        assert torn == [TornTail(segment="w0.jsonl", byte_offset=data.rfind(b"\n") + 1)]
    heads = store.log.doc_heads("r1")

    # The same writer id reopens (the dead process released its lock) and every chain
    # continues from the last whole event on disk: no seq is reused.
    with store.log.open_writer("r1", "w0") as w:
        nxt = {d: w.append(doc_id=d, node="after_crash", kind="handoff") for d in docs}
    for d, ev in nxt.items():
        assert (ev.seq, ev.prev_hash) == (before.get(d, 0), heads.get(d, GENESIS))
    assert check_chains(store.root) == {d: before.get(d, 0) + 1 for d in docs}
    assert store.log.torn_tails("r1") == torn  # the old tail stays reported, never glued to


# --- writer ownership ---------------------------------------------------------------


def test_same_writer_id_cannot_be_open_twice(store: SimpleNamespace) -> None:
    w = store.log.open_writer("r1", "w0")
    with pytest.raises(WriterInUse):
        store.log.open_writer("r1", "w0")
    w.close()
    store.log.open_writer("r1", "w0").close()  # free again after close


def test_document_held_by_a_live_writer_is_refused(store: SimpleNamespace) -> None:
    a = store.log.open_writer("r1", "wa")
    b = store.log.open_writer("r1", "wb")  # opened before wa writes anything
    e0 = a.append(doc_id="d1", node="n", kind="decision")
    with pytest.raises(DocumentInUse):
        b.append(doc_id="d1", node="n", kind="decision")
    b.append(doc_id="d2", node="n", kind="decision")  # other documents are fine
    e1 = a.append(doc_id="d1", node="n", kind="decision", ts=None)
    a.close()
    # wb was opened earlier, but picks the head up from disk when it first takes d1.
    e2 = b.append(doc_id="d1", node="n", kind="decision")
    b.close()
    assert (e2.seq, e2.prev_hash) == (2, e1.hash) and e1.prev_hash == e0.hash
    check_chains(store.root)


def test_writer_reopened_with_the_same_id_appends_to_its_segment(store: SimpleNamespace) -> None:
    with store.log.open_writer("r1", "w0") as w:
        a = w.append(doc_id="d1", node="n", kind="decision")
    with store.log.open_writer("r1", "w0") as w:
        b = w.append(doc_id="d1", node="n", kind="decision")
    assert b.prev_hash == a.hash and b.seq == 1
    assert [p.name for p in seg_dir(store).iterdir()] == ["w0.jsonl"]


def test_own_torn_segment_rolls_to_a_new_segment(store: SimpleNamespace) -> None:
    with store.log.open_writer("r1", "w0") as w:
        a = w.append(doc_id="d1", node="n", kind="decision")
    seg = seg_dir(store) / "w0.jsonl"
    with seg.open("ab") as f:
        f.write(b'{"half')
    with store.log.open_writer("r1", "w0") as w:
        b = w.append(doc_id="d1", node="n", kind="decision")
    assert b.prev_hash == a.hash
    assert sorted(p.name for p in seg_dir(store).iterdir()) == ["w0.1.jsonl", "w0.jsonl"]
    assert seg.read_bytes().endswith(b'{"half')  # the torn bytes are never appended to
    assert store.log.torn_tails("r1") == [TornTail(segment="w0.jsonl", byte_offset=seg.stat().st_size - 6)]
    check_chains(store.root)


@pytest.mark.parametrize("bad", ["", ".", "..", "../x", "a/b", "w.1", "-w", "w 0", "w\n", "x" * 101, 3])
def test_writer_id_must_be_path_safe(store: SimpleNamespace, bad: Any) -> None:
    with pytest.raises(ValueError):
        store.log.open_writer("r1", bad)
    assert not seg_dir(store).exists() or list(seg_dir(store).iterdir()) == []


def test_open_writer_on_unknown_run_fails(store: SimpleNamespace) -> None:
    with pytest.raises(FileNotFoundError):
        store.log.open_writer("nope", "w0")


# --- flush semantics ----------------------------------------------------------------


def test_unflushed_events_are_invisible_to_readers(store: SimpleNamespace) -> None:
    w = store.log.open_writer("r1", "w0", flush_every=4)
    evs = [w.append(doc_id="d1", node=f"n{i}", kind="decision") for i in range(3)]
    assert list(store.log.iter_events("r1")) == []  # readers see disk only
    assert store.log.doc_heads("r1") == {}
    evs.append(w.append(doc_id="d1", node="n3", kind="decision"))  # 4th append flushes
    assert [e.hash for e in store.log.iter_events("r1")] == [e.hash for e in evs]
    evs.append(w.append(doc_id="d1", node="n4", kind="decision"))
    assert store.log.doc_heads("r1") == {"d1": evs[3].hash}
    w.flush()
    assert store.log.doc_heads("r1") == {"d1": evs[4].hash}
    w.append(doc_id="d1", node="n5", kind="decision")
    w.close()
    assert len(list(store.log.iter_events("r1"))) == 6


def test_flush_every_must_be_positive(store: SimpleNamespace) -> None:
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            store.log.open_writer("r1", "w0", flush_every=bad)


def test_lost_unflushed_events_never_cause_seq_reuse(store: SimpleNamespace) -> None:
    w = store.log.open_writer("r1", "w0", flush_every=100)
    w.append(doc_id="d1", node="n", kind="decision")
    w.flush()
    w.append(doc_id="d1", node="lost", kind="decision")  # never flushed
    w._buffer.clear()  # simulate a crash: the buffered event never reaches disk
    w.close()
    with store.log.open_writer("r1", "w1") as w2:
        nxt = w2.append(doc_id="d1", node="n", kind="decision")
    assert nxt.seq == 1
    check_chains(store.root)


def test_closed_writer_refuses_appends(store: SimpleNamespace) -> None:
    w = store.log.open_writer("r1", "w0")
    w.close()
    w.close()  # idempotent
    with pytest.raises(RuntimeError):
        w.append(doc_id="d1", node="n", kind="decision")


# --- what is written ----------------------------------------------------------------


def test_every_line_is_canonical_and_rehashes_to_its_hash(store: SimpleNamespace) -> None:
    ref = store.blobs.put(b"synthetic transcript", "transcript")
    with store.log.open_writer("r1", "w0") as w:
        w.append(
            doc_id="d1",
            node="classify",
            kind="decision",
            agent="sorter",
            attempt=1,
            model="m",
            prompt_id="p",
            tokens_in=1,
            tokens_out=2,
            tokens_cached=0,
            price_basis="list",
            latency_ms=12.25,
            decision=[{"question": "q", "answer": "a", "p": 0.1}],
            cause="c",
            inputs=[ref],
            outputs=[ref.model_dump()],
            payload={"route": ("a", "b"), "x": 1.0, "é": None},
        )
        w.append(doc_id="_run", node="start", kind="handoff")
    lines = segment_lines(store.root)
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert event_hash(obj) == obj["hash"]
        assert line == canonical_json(obj) + b"\n"
        assert line.count(b"\n") == 1


def test_mutating_inputs_or_results_after_append_does_not_change_the_record(store: SimpleNamespace) -> None:
    payload: dict[str, Any] = {"route": ["a"], "nested": {"k": 1}}
    decision: list[Any] = [{"question": "q", "answer": "a"}]
    w = store.log.open_writer("r1", "w0", flush_every=10)
    ev = w.append(doc_id="d1", node="n", kind="decision", payload=payload, decision=decision)
    payload["route"].append("tampered")
    payload["nested"]["k"] = 2
    decision[0]["answer"] = "tampered"
    ev.payload["nested"]["k"] = 3  # the returned model holds plain dicts too
    ev.decision[0]["answer"] = "also tampered"
    w.close()
    (stored,) = list(store.log.iter_events("r1"))
    assert stored.payload == {"route": ["a"], "nested": {"k": 1}}
    assert stored.decision == [{"question": "q", "answer": "a"}]
    check_chains(store.root)


def test_ts_defaults_to_now_and_accepts_an_explicit_value(store: SimpleNamespace) -> None:
    from datetime import datetime

    with store.log.open_writer("r1", "w0") as w:
        auto = w.append(doc_id="d1", node="n", kind="decision")
        given = w.append(doc_id="d1", node="n", kind="decision", ts="2025-01-02T03:04:05Z")
    datetime.strptime(auto.ts, TS_FORMAT)
    assert auto.ts.endswith("Z") and given.ts == "2025-01-02T03:04:05Z"


def test_invalid_append_changes_nothing(store: SimpleNamespace) -> None:
    w = store.log.open_writer("r1", "w0")
    a = w.append(doc_id="d1", node="n", kind="decision")
    with pytest.raises(ValidationError):
        w.append(doc_id="d1", node="n", kind="decision", ts="2025-01-02 03:04:05")
    with pytest.raises(ValidationError):
        w.append(doc_id="d1", node="n", kind="not-a-kind")
    b = w.append(doc_id="d1", node="n", kind="decision")
    w.close()
    assert (b.seq, b.prev_hash) == (1, a.hash)
    assert len(segment_lines(store.root)) == 2


def test_blob_refs_must_be_present_not_tombstoned(store: SimpleNamespace) -> None:
    keep = store.blobs.put(b"keep", "original")
    gone = store.blobs.put(b"gone", "original")
    store.blobs.tombstone(gone.sha256, "deletion request")
    w = store.log.open_writer("r1", "w0")
    with pytest.raises(BlobNotStored):
        w.append(doc_id="d1", node="n", kind="output", inputs=[keep], outputs=[gone])
    ev = w.append(doc_id="d1", node="n", kind="output", inputs=[keep])
    w.close()
    assert ev.seq == 0 and ev.inputs == [keep]


# --- reading ------------------------------------------------------------------------


def test_iter_events_orders_by_doc_and_seq_across_segments(store: SimpleNamespace) -> None:
    with store.log.open_writer("r1", "wb") as w:
        w.append(doc_id="d2", node="n", kind="decision")
        w.append(doc_id="_run", node="start", kind="handoff")
    with store.log.open_writer("r1", "wa") as w:
        w.append(doc_id="d2", node="n", kind="decision")
        w.append(doc_id="Z", node="n", kind="decision")
        w.append(doc_id="d1", node="n", kind="decision")
    keys = [(e.doc_id, e.seq) for e in store.log.iter_events("r1")]
    assert keys == sorted(keys) == [("Z", 0), ("_run", 0), ("d1", 0), ("d2", 0), ("d2", 1)]
    assert [(e.doc_id, e.seq) for e in store.log.iter_events("r1", "d2")] == [("d2", 0), ("d2", 1)]
    assert list(store.log.iter_events("r1", "absent")) == []


def test_stored_v0_line_is_read_through_the_upgrade_path(store: SimpleNamespace) -> None:
    raw = stored_v0_event_dict(doc_id="d1", inputs=[])
    seg_dir(store).mkdir(parents=True, exist_ok=True)
    (seg_dir(store) / "old.jsonl").write_bytes(canonical_json(raw) + b"\n")
    (ev,) = list(store.log.iter_events("r1"))
    assert ev.tokens_cached is None and ev.hash == raw["hash"]
    assert store.log.doc_heads("r1") == {"d1": raw["hash"]}
    with store.log.open_writer("r1", "w0") as w:
        nxt = w.append(doc_id="d1", node="n", kind="decision")
    assert (nxt.seq, nxt.prev_hash) == (1, raw["hash"])


def test_corrupt_whole_line_is_an_error_not_skipped(store: SimpleNamespace) -> None:
    with store.log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="n", kind="decision")
    seg = seg_dir(store) / "w0.jsonl"
    seg.write_bytes(b"not json\n" + seg.read_bytes())
    with pytest.raises(ValueError, match="w0.jsonl"):
        list(store.log.iter_events("r1"))
    with pytest.raises(ValueError):
        store.log.open_writer("r1", "w1").append(doc_id="d1", node="n", kind="decision")


def test_read_run_round_trips(store: SimpleNamespace) -> None:
    assert store.log.read_run("r1") == store.run
    raw = json.loads((store.root / "runs/r1/run.json").read_bytes())
    assert raw == store.run.model_dump(mode="json")


def test_concurrent_create_run_has_one_winner(tmp_path: Path) -> None:
    from mailroom_record.blobs import BlobStore

    log = EventLog(tmp_path, BlobStore(tmp_path))
    run = Run(**run_fields(run_id="race"))
    results: list[str] = []
    barrier = threading.Barrier(8)

    def go() -> None:
        barrier.wait()
        try:
            log.create_run(run)
            results.append("created")
        except RunExists:
            results.append("exists")

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["created"] + ["exists"] * 7
    assert log.read_run("race") == run
    assert not list(tmp_path.rglob("*.tmp"))



# --- review fixes (forked chains, durability) ---------------------------------------


def write_fork(store: SimpleNamespace, doc_id: str = "d1") -> tuple[str, str]:
    """Make two segments that both hold seq 1 of ``doc_id``: a chain forked by two owners.

    Returns the hashes of the two seq-1 events. The second segment is written by hand,
    as a second process owning the same document would have written it.
    """
    with store.log.open_writer("r1", "w0") as w:
        a = w.append(doc_id=doc_id, node="n", kind="decision")
        b = w.append(doc_id=doc_id, node="left", kind="decision")
    forked = Event.build(
        run_id="r1", doc_id=doc_id, seq=1, ts="2026-11-18T12:00:00Z", node="right", agent=None,
        attempt=0, kind="decision", model=None, prompt_id=None, tokens_in=None, tokens_out=None,
        tokens_cached=None, price_basis=None, latency_ms=None, decision=None, cause=None,
        inputs=[], outputs=[], payload={}, prev_hash=a.hash,
    )
    (seg_dir(store) / "w9.jsonl").write_bytes(canonical_json(forked.model_dump(mode="json")) + b"\n")
    return b.hash, forked.hash


def test_writer_refuses_to_extend_a_forked_chain(store: SimpleNamespace) -> None:
    write_fork(store)
    with store.log.open_writer("r1", "w1") as w:
        with pytest.raises(ForkedChain, match=r"'d1'.*seq 1"):
            w.append(doc_id="d1", node="n2", kind="decision")
        # Other documents are unaffected, and the refused document is not held.
        assert w.append(doc_id="d2", node="n", kind="decision").seq == 0
    assert not any(json.loads(line)["node"] == "n2" for line in segment_lines(store.root))


def test_doc_heads_refuses_a_forked_chain_but_readers_return_every_line(store: SimpleNamespace) -> None:
    left, right = write_fork(store)
    with pytest.raises(ForkedChain, match="d1"):
        store.log.doc_heads("r1")
    events = list(store.log.iter_events("r1", "d1"))
    assert [e.seq for e in events] == [0, 1, 1]
    assert {e.hash for e in events if e.seq == 1} == {left, right}
    assert len(list(store.log.iter_stored("r1"))) == 3


def test_open_writer_fsyncs_the_segment_dir_even_when_the_segment_exists(
    store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A crash before the creating process fsynced the directory would leave the segment's
    # entry undurable; every open must fsync it again.
    store.log.open_writer("r1", "w0").close()
    calls: list[Path] = []
    real = log_module._fsync_dir
    monkeypatch.setattr(log_module, "_fsync_dir", lambda p: (calls.append(Path(p)), real(p))[1])
    store.log.open_writer("r1", "w0").close()
    assert seg_dir(store) in calls
