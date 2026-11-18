"""The derived SQLite index (Task 8): equivalence with the log, and every way it can fail.

The index is disposable and the log is the truth (ADR-0001), so these tests check that it
always equals a rebuild from the log (by example, as a property over random multi-writer
histories, and under threads), that a damaged, old or half-written index is never
trusted, that no crash or reader can see a half-built one, that a damaged log does not
stop a rebuild, and that no table holds document text.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from record_testkit import (
    SENTINEL,
    build_two_run_store,
    chain,
    dump_digest,
    join_all,
    make_event,
    peak_bytes,
    read_index_until,
    reindex_in_child,
    run_fields,
    seal_fields,
    slow_log,
    v1_event_dict,
    write_lines,
)

from mailroom_record import log as log_module
from mailroom_record.blobs import BlobStore
from mailroom_record.index import SCHEMA_VERSION, ApplyRefused, Index, IndexUnavailable
from mailroom_record.log import EventLog
from mailroom_record.models import Run, Seal
from mailroom_record.seal import list_seals, seal_run


def append(store: SimpleNamespace, writer: str = "w-x", run_id: str = "r1", **fields: Any) -> Any:
    with store.log.open_writer(run_id, writer) as w:
        return w.append(**fields)


def built(store: SimpleNamespace, path: Path) -> Index:
    idx = Index(path)
    idx.reindex(store.log)
    return idx


# --- the plan's sketches ----------------------------------------------------------


def test_reindex_equals_incremental(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    store = store_with_events
    inc = Index(tmp_path / "a.sqlite")
    inc.reindex(EventLog(tmp_path / "empty", store.blobs))  # a ready, empty index
    inc.add_run(store.log.read_run("r1"))  # the engine registers a run after create_run
    for e in store.log.iter_events("r1"):
        inc.apply(e)
    full = Index(tmp_path / "b.sqlite")
    full.reindex(store.log)
    assert inc.dump() == full.dump()
    assert [(d.doc_id, d.events) for d in full.documents("r1")] == [("d1", 2), ("d2", 1)]


def test_index_is_disposable(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    store = store_with_events
    p = tmp_path / "i.sqlite"
    Index(p).reindex(store.log)
    before = Index(p).dump()
    assert before["chain_heads"]  # positive control: there is something to lose
    p.unlink()
    assert Index(p).status() == "missing"
    Index(p).reindex(store.log)
    assert Index(p).dump() == before


def test_an_index_path_with_uri_characters_works(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    p = tmp_path / "odd dir?#%20" / "i x?.sqlite"
    idx = built(store_with_events, p)
    assert idx.status() == "ready" and len(idx.documents("r1")) == 2
    assert sorted(f.name for f in p.parent.iterdir()) == ["i x?.sqlite", "i x?.sqlite.lock"]


def test_document_seen_in_two_runs(tmp_path: Path) -> None:
    two = build_two_run_store(tmp_path / "store")
    idx = Index(tmp_path / "i.sqlite")
    idx.reindex(two.log)
    assert [r.run_id for r in idx.runs_for_document("d1")] == ["r1", "r2"]
    assert [r.run_id for r in idx.runs_for_document("d2")] == ["r1"]
    assert idx.runs_for_document("absent") == []
    assert [(d.doc_id, d.events) for d in idx.documents("r2")] == [("d1", 2), ("d3", 2)]
    # The chains are per run: one document's heads differ between the runs.
    heads = {r: {d.doc_id: d.head_hash for d in idx.documents(r)} for r in ("r1", "r2")}
    assert heads["r1"]["d1"] != heads["r2"]["d1"]
    # An event in one run does not touch the other run's rows.
    e = append(two, run_id="r2", doc_id="d1", node="late", kind="review")
    idx.apply(e)
    assert [d.in_review for d in idx.documents("r1")] == [False, False]
    assert [(d.doc_id, d.events, d.in_review) for d in idx.documents("r2")] == [("d1", 3, True), ("d3", 2, False)]
    assert [(r.run_id, r.documents, r.events, r.in_review) for r in idx.runs()] == [("r1", 2, 4, 0), ("r2", 2, 5, 1)]


def test_review_queue_tracks_last_event_kind(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    store = store_with_events
    append(store, doc_id="d1", node="human_review", kind="review", decision={"state": "paused"})
    idx = Index(tmp_path / "i.sqlite")
    idx.reindex(store.log)
    assert [d.doc_id for d in idx.documents("r1") if d.in_review] == ["d1"]
    assert [(d.doc_id, d.last_node, d.last_kind) for d in idx.review_queue("r1")] == [("d1", "human_review", "review")]


def test_review_rule_reads_kind_only_and_a_later_event_closes_it(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    store = store_with_events
    idx = built(store, tmp_path / "i.sqlite")
    steps = [
        ("d2", "anything", "review", {"d1": False, "d2": True}),  # node name does not matter
        ("d1", "human_review", "decision", {"d1": False, "d2": True}),  # nor does it make a review
        ("d2", "second_look", "review", {"d1": False, "d2": True}),  # a second review keeps it in
        ("d2", "resolve", "output", {"d1": False, "d2": False}),  # the outcome takes it out
    ]
    for doc, node, kind, want in steps:
        idx.apply(append(store, doc_id=doc, node=node, kind=kind))
        assert {d.doc_id: d.in_review for d in idx.documents("r1")} == want
    assert idx.drift(store.log) == []


# --- reindex == incremental, as a property -----------------------------------------

_OPS = st.lists(
    st.tuples(
        st.sampled_from(["append", "append", "append", "review", "flush", "close", "seal", "reapply"]),
        st.sampled_from(["r1", "r2"]),
        st.integers(0, 2),  # writer
        st.integers(0, 3),  # document
    ),
    max_size=30,
)


@given(ops=_OPS)
def test_incremental_equals_reindex_over_random_histories(ops: list[tuple[str, str, int, int]]) -> None:
    # Two runs sharing document ids, three writers per run with handoffs (a document moves
    # to another writer, which needs its holder closed), explicit flushes, seals and repeated
    # applies. Events are applied only once durable (after the flush or close that wrote
    # them), as the engine must. After every step the index equals a rebuild of the log.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log = EventLog(root, BlobStore(root))
        for i, run_id in enumerate(("r1", "r2")):
            log.create_run(Run(**run_fields(run_id=run_id, created_at=f"2026-11-18T1{i}:00:00Z")))
        inc = Index(root / "inc.sqlite")
        inc.reindex(log)
        writers: dict[tuple[str, int], Any] = {}
        pending: dict[tuple[str, int], list[Any]] = {}
        holder: dict[tuple[str, str], tuple[str, int]] = {}
        applied: list[Any] = []

        def durable(key: tuple[str, int]) -> None:
            for e in pending.pop(key, []):
                inc.apply(e)
                applied.append(e)

        def close(key: tuple[str, int]) -> None:
            w = writers.pop(key, None)
            if w is not None:
                w.close()
                durable(key)
                for k in [k for k, v in holder.items() if v == key]:
                    del holder[k]

        for op, run_id, wi, di in ops:
            key, doc = (run_id, wi), f"d{di}"
            if op in ("append", "review"):
                other = holder.get((run_id, doc))
                if other is not None and other != key:
                    close(other)  # hand the document over
                if key not in writers:
                    writers[key] = log.open_writer(run_id, f"w{wi}", flush_every=10**6)
                kind = "review" if op == "review" else "decision"
                e = writers[key].append(doc_id=doc, node=f"n{len(applied)}", kind=kind)
                pending.setdefault(key, []).append(e)
                holder[(run_id, doc)] = key
            elif op == "flush" and key in writers:
                writers[key].flush()
                durable(key)
            elif op == "close":
                close(key)
            elif op == "seal":
                inc.apply_seal(seal_run(log, run_id))
            elif op == "reapply" and applied:
                before = inc.dump()
                inc.apply(applied[di % len(applied)])
                assert inc.dump() == before
            assert inc.drift(log) == [], (op, run_id, wi, di)
        for key in list(writers):
            close(key)
        full = Index(root / "full.sqlite")
        full.reindex(log)
        assert inc.dump() == full.dump()
        inc.reindex(log)  # a rebuild over the incremental index changes nothing
        assert inc.dump() == full.dump()
        assert sum(r.events for r in inc.runs()) == len(applied)


# --- apply: idempotent, ordered, never silently wrong -------------------------------


def test_apply_is_idempotent_and_refuses_out_of_order_events(store: SimpleNamespace, tmp_path: Path) -> None:
    with store.log.open_writer("r1", "w0") as w:
        evs = [w.append(doc_id="d1", node=f"n{i}", kind="decision") for i in range(4)]
    idx = Index(tmp_path / "i.sqlite")
    idx.reindex(EventLog(tmp_path / "empty", store.blobs))
    with pytest.raises(ApplyRefused) as exc:
        idx.apply(evs[0])
    assert exc.value.kind == "unknown_run"
    idx.add_run(store.run)
    idx.add_run(store.run)  # idempotent
    with pytest.raises(ApplyRefused, match="run_mismatch"):
        idx.add_run(Run(**run_fields(kind="service_period")))
    idx.apply(evs[0])
    idx.apply(evs[1])
    snapshot = idx.dump()
    forked = make_event(doc_id="d1", seq=1, prev_hash=evs[0].hash, node="other", inputs=[])
    refusals = {
        "seq_gap": evs[3],  # seq 3 after head 1
        "forked_chain": forked,  # another event at the head's seq
        "broken_link": make_event(doc_id="d1", seq=2, prev_hash=evs[0].hash, inputs=[]),
    }
    for kind, e in refusals.items():
        with pytest.raises(ApplyRefused) as exc:
            idx.apply(e)
        assert exc.value.kind == kind
        assert idx.dump() == snapshot  # a refused apply changes nothing
    for e in (evs[0], evs[1], evs[0]):  # already covered: no change
        idx.apply(e)
        assert idx.dump() == snapshot
    idx.apply(evs[2])
    idx.apply(evs[3])
    assert idx.dump() == built(store, tmp_path / "full.sqlite").dump()


def test_events_covered_by_a_rebuild_apply_as_no_ops(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    store = store_with_events
    idx = built(store, tmp_path / "i.sqlite")
    before = idx.dump()
    for e in store.log.iter_events("r1"):
        idx.apply(e)
    assert idx.dump() == before


def test_an_index_that_lost_its_tail_refuses_and_a_rebuild_repairs_it(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    # synchronous=NORMAL lets a power cut drop the index's last commits; the next apply
    # must notice the gap rather than build on a stale head.
    store = store_with_events
    idx = built(store, tmp_path / "i.sqlite")
    with store.log.open_writer("r1", "w1") as w:
        lost = [w.append(doc_id="d1", node=f"x{i}", kind="decision") for i in range(3)]
    with pytest.raises(ApplyRefused, match="seq_gap"):
        idx.apply(lost[2])
    assert idx.drift(store.log) == ["documents", "chain_heads"]
    idx.reindex(store.log)
    assert idx.drift(store.log) == []
    idx.apply(lost[2])  # now covered


def test_seal_apply_follows_the_seal_chain(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    store = store_with_events
    idx = built(store, tmp_path / "i.sqlite")
    s0 = seal_run(store.log, "r1")
    append(store, doc_id="d3", node="n", kind="decision")
    s1 = seal_run(store.log, "r1")
    with pytest.raises(ApplyRefused, match="seal_chain"):
        idx.apply_seal(s1)  # seal 1 before seal 0
    idx.apply_seal(s0)
    idx.apply_seal(s0)  # already taken
    other = s0.model_copy(update={"hash": "e" * 64})
    with pytest.raises(ApplyRefused, match="seal_chain"):
        idx.apply_seal(other)
    idx.apply_seal(s1)
    assert idx.runs()[0].seals == 2
    idx.apply(store.log.iter_events("r1", "d3").__next__())
    assert idx.dump() == built(store, tmp_path / "full.sqlite").dump()


# --- the index file is never trusted when damaged -----------------------------------


def _flip(path: Path, offset: int, value: int) -> None:
    with path.open("r+b") as f:
        f.seek(offset)
        f.write(bytes([value]))


def _sql(path: Path, *statements: str) -> None:
    con = sqlite3.connect(path)
    for s in statements:
        con.execute(s)
    con.commit()
    con.close()


DAMAGE = {
    "garbage": ("corrupt", lambda p: p.write_bytes(os.urandom(8192))),
    "empty_file": ("missing", lambda p: p.write_bytes(b"")),
    "truncated_half": ("corrupt", lambda p: os.truncate(p, p.stat().st_size // 2)),
    "truncated_header": ("corrupt", lambda p: os.truncate(p, 50)),
    "header_magic": ("corrupt", lambda p: _flip(p, 0, ord("X"))),
    "btree_page_type": ("corrupt", lambda p: _flip(p, 4096 * 2, 0xFF)),
    "old_version": ("old_version", lambda p: _sql(p, f"UPDATE meta SET value = '{SCHEMA_VERSION - 1}' WHERE key = 'schema_version'")),
    "older_layout": ("old_version", lambda p: _sql(p, "DROP TABLE meta")),
    "unfinished": ("incomplete", lambda p: _sql(p, "UPDATE meta SET value = 'building' WHERE key = 'state'")),
}


@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_a_damaged_or_old_index_is_refused_then_rebuilt(store_with_events: SimpleNamespace, tmp_path: Path, damage: str) -> None:
    store = store_with_events
    append(store, doc_id="d2", node="hr", kind="review")
    p = tmp_path / "i.sqlite"
    idx = built(store, p)
    before = idx.dump()
    assert not Path(str(p) + "-wal").exists()  # every write closed its connection: all in the main file
    status, hurt = DAMAGE[damage]
    hurt(p)
    assert idx.status() == status
    for query in (idx.runs, lambda: idx.documents("r1"), lambda: idx.runs_for_document("d1"), idx.dump, idx.review_queue):
        with pytest.raises(IndexUnavailable) as exc:
            query()
        assert exc.value.status == status
    with pytest.raises(IndexUnavailable):
        idx.apply(next(store.log.iter_events("r1")))
    # A per-document read never depends on the damaged index.
    assert list(idx.iter_events(store.log, "r1", "d2")) == list(store.log.iter_events("r1", "d2"))
    stats = idx.ensure(store.log)
    assert stats is not None and stats.events == 4
    assert idx.status() == "ready" and idx.dump() == before
    assert idx.ensure(store.log) is None  # ready: nothing to do


# --- a crash or a reader never sees a half-built index -----------------------------


def test_a_failure_mid_rebuild_keeps_the_old_index(store_with_events: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_with_events
    p = tmp_path / "i.sqlite"
    idx = built(store, p)
    before = idx.dump()
    append(store, doc_id="d3", node="n", kind="decision")
    original = log_module.RunScan.events

    def failing(self: Any) -> Any:  # the disk fails after the first event of the rebuild
        it = original(self)
        yield next(it)
        raise OSError(28, "injected ENOSPC while reading the log")

    monkeypatch.setattr(log_module.RunScan, "events", failing)
    with pytest.raises(OSError, match="ENOSPC"):
        idx.reindex(store.log)
    monkeypatch.setattr(log_module.RunScan, "events", original)
    assert idx.status() == "ready" and idx.dump() == before
    idx.reindex(store.log)
    assert [d.doc_id for d in idx.documents("r1")] == ["d1", "d2", "d3"]


def test_a_full_disk_inside_sqlite_keeps_the_old_index(store_with_events: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A real SQLITE_FULL from the rebuild transaction: the file may not grow past its size.
    store = store_with_events
    p = tmp_path / "i.sqlite"
    idx = built(store, p)
    before = idx.dump()
    with store.log.open_writer("r1", "w-many") as w:
        for i in range(400):
            w.append(doc_id=f"many-{i:04d}-" + "x" * 150, node="n", kind="decision")
    pages = p.stat().st_size // 4096
    real_connect = Index._connect

    def capped(self: Index, create: bool) -> sqlite3.Connection:
        con = real_connect(self, create)
        if create:
            con.execute(f"PRAGMA max_page_count = {pages + 2}")
        return con

    monkeypatch.setattr(Index, "_connect", capped)
    with pytest.raises(sqlite3.OperationalError) as exc:
        idx.reindex(store.log)
    assert exc.value.sqlite_errorname == "SQLITE_FULL"
    monkeypatch.setattr(Index, "_connect", real_connect)
    assert p.exists() and idx.status() == "ready" and idx.dump() == before  # not deleted as "corrupt"
    assert idx.reindex(store.log).documents == 402


@pytest.mark.parametrize("had_index", [True, False])
def test_a_rebuild_killed_mid_transaction_leaves_the_old_index_or_none(store_with_events: SimpleNamespace, tmp_path: Path, had_index: bool) -> None:
    store = store_with_events
    p = tmp_path / "i.sqlite"
    before = built(store, p).dump() if had_index else None
    for i in range(3):
        append(store, writer=f"w{i + 1}", doc_id=f"late{i}", node="n", kind="decision")
    marker = tmp_path / "mid-build"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=reindex_in_child, args=(str(store.root), str(p), str(marker), 4))
    proc.start()
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert proc.is_alive() and time.monotonic() < deadline
            time.sleep(0.02)
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        (code,) = join_all([proc], timeout=30)  # reaped, and killed if the wait failed
    assert code == -signal.SIGKILL
    idx = Index(p)
    if had_index:
        assert idx.status() == "ready" and idx.dump() == before
    else:
        assert idx.status() == "missing"
        with pytest.raises(IndexUnavailable, match="missing"):
            idx.documents("r1")
    assert idx.ensure(store.log) is None if had_index else idx.ensure(store.log) is not None
    idx.reindex(store.log)
    assert idx.drift(store.log) == [] and len(idx.documents("r1")) == 5


def test_a_reader_process_sees_the_old_index_or_the_new_one_never_a_mix(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    store = store_with_events
    p = tmp_path / "i.sqlite"
    old = dump_digest(built(store, p))
    for i in range(4):
        append(store, writer=f"w{i + 1}", doc_id=f"late{i}", node="n", kind="review" if i % 2 else "decision")
    new = dump_digest(built(store, tmp_path / "expected.sqlite"))
    assert old != new
    stop = str(tmp_path / "stop")
    out = tmp_path / "seen.json"
    mid = str(tmp_path / "mid")  # written by the rebuild inside its transaction
    ctx = multiprocessing.get_context("spawn")
    reader = ctx.Process(target=read_index_until, args=(str(p), stop, str(out), mid))
    reader.start()
    try:
        Path(stop + ".building").touch()
        slow = slow_log(store.root, store.blobs, marker=mid, after=3, pause=1.5)
        time.sleep(0.5)  # let the reader start reading the old index
        Index(p).reindex(slow)
        os.unlink(stop + ".building")
        time.sleep(0.2)
    finally:
        Path(stop).touch()
        (code,) = join_all([reader], timeout=60)  # killed and reaped if it overruns
    assert code == 0
    seen = json.loads(out.read_text())
    assert {d for _, d in seen} <= {old, new}, {d for _, d in seen} - {old, new}
    assert any(b and d == old for b, d in seen)  # positive control: it read inside the open transaction
    assert seen[-1][1] == new


# --- a damaged log does not stop a rebuild ----------------------------------------


def test_rebuild_indexes_what_is_valid_and_records_each_problem(tmp_path: Path) -> None:
    root = tmp_path
    blobs = BlobStore(root)
    log = EventLog(root, blobs)
    log.create_run(Run(**run_fields()))
    good = chain("good", 3)
    gap = chain("gap", 5)
    fork = chain("fork", 3)  # seq 2 links to the first seq 1: it must not be indexed past the fork
    fork_b = make_event(doc_id="fork", seq=1, prev_hash=fork[0].hash, node="b", inputs=[])
    link = chain("link", 1) + [make_event(doc_id="link", seq=1, prev_hash="c" * 64, inputs=[])]
    invalid = v1_event_dict(doc_id="inv", seq=1, inputs=[]) | {"kind": "bogus"}
    write_lines(root, "a.jsonl", good[:2] + gap[:2], extra=b"\x00not json\n")
    write_lines(root, "a.jsonl", [good[2], gap[3], gap[4]] + fork + link + chain("inv", 1) + [invalid] + chain("inv", 1, start=2, prev=chain("inv", 2)[1].hash))
    write_lines(root, "b.jsonl", [fork_b], extra=b'{"torn')
    log2 = Run(**run_fields(run_id="r2"))
    log.create_run(log2)
    (root / "runs/r2/seals").mkdir()
    (root / "runs/r2/seals/0.json").write_text("{broken")
    write_lines(root, "w.jsonl", chain("other", 2, run_id="r2"), run_id="r2")
    (root / "runs/r3/segments").mkdir(parents=True)
    (root / "runs/r3/run.json").write_text('{"schema_version": 1}')
    (root / "runs/r4/segments").mkdir(parents=True)  # being created: no header, no segment yet
    write_lines(root, "w.jsonl", chain("lost", 1, run_id="r5"), run_id="r5")  # segments, header gone

    idx = Index(root / "i.sqlite")
    stats = idx.reindex(log)
    got = sorted((p.run_id, p.doc_id or "", p.kind) for p in stats.problems)
    assert got == [  # one problem per damage: a document stops at its first refused event
        ("r1", "", "corrupt_line"),
        ("r1", "", "invalid_event"),
        ("r1", "fork", "forked_chain"),
        ("r1", "gap", "seq_gap"),
        ("r1", "inv", "seq_gap"),
        ("r1", "link", "broken_link"),
        ("r2", "", "seal_corrupt"),
        ("r3", "", "corrupt_run_header"),
        ("r5", "", "corrupt_run_header"),
    ]
    assert stats.torn_tails == 1
    heads = {d.doc_id: d.events for d in idx.documents("r1")}
    assert heads == {"good": 3, "gap": 2, "fork": 2, "link": 1, "inv": 1}
    assert [(d.doc_id, d.events) for d in idx.documents("r2")] == [("other", 2)]
    assert [(r.run_id, r.seals) for r in idx.runs()] == [("r1", 0), ("r2", None)]
    assert {(p.kind, p.segment) for p in idx.problems() if p.kind in ("corrupt_line", "invalid_event")} == {
        ("corrupt_line", "a.jsonl"),
        ("invalid_event", "a.jsonl"),
    }
    with pytest.raises(ApplyRefused, match="seals could not be read"):  # r2's seal state is unknown
        idx.apply_seal(Seal.build(**seal_fields(run_id="r2", n=0)))


def test_a_handoff_during_the_scan_is_not_recorded_as_a_gap(store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Writer "a" hands d1 to writer "b" after the scan read a.jsonl but before b.jsonl;
    # the first read sees seq 0,1,2,4. The rebuild must re-read, not record a gap.
    from record_testkit import append_more

    append_more(store, doc_id="d2", n=1, writer_id="b")
    append_more(store, doc_id="d1", n=3, writer_id="a")
    wa = store.log.open_writer("r1", "a")
    original = log_module._stream_lines
    fired: list[bool] = []

    def racing(path: Path, start: int = 0, end: int | None = None) -> Any:
        yield from original(path, start, end)
        if path.name == "a.jsonl" and not fired:
            fired.append(True)
            wa.append(doc_id="d1", node="n", kind="output")
            wa.close()
            append_more(store, doc_id="d1", n=1, writer_id="b")

    monkeypatch.setattr(log_module, "_stream_lines", racing)
    stats = Index(tmp_path / "i.sqlite").reindex(store.log)
    assert fired and stats.problems == () and stats.events == 6


# --- per-document reads through the index -----------------------------------------


def test_per_document_reads_use_the_index_and_stay_exact(store: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in range(4):
        with store.log.open_writer("r1", f"w{k}", flush_every=8) as w:
            for i in range(30):
                for j in range(5):
                    w.append(doc_id=f"w{k}-d{j}", node=f"n{i}", kind="decision")
    idx = built(store, tmp_path / "i.sqlite")
    lines: list[str] = []
    original = log_module._stream_lines

    def counting(path: Path, start: int = 0, end: int | None = None) -> Any:
        for item in original(path, start, end):
            lines.append(path.name)
            yield item

    monkeypatch.setattr(log_module, "_stream_lines", counting)

    seen: list[str] = []

    def read(doc: str) -> list[tuple[int, str]]:
        lines.clear()
        out = [(e.seq, e.hash) for e in idx.iter_events(store.log, "r1", doc)]
        seen[:] = lines  # the lines this read touched (truth() below reads too)
        return out

    def truth(doc: str) -> list[tuple[int, str]]:
        return [(e.seq, e.hash) for e in store.log.iter_events("r1", doc)]

    assert read("w2-d3") == truth("w2-d3") and len(truth("w2-d3")) == 30
    assert set(seen) == {"w2.jsonl"} and len(seen) <= 150  # one writer's lines, not 600
    # Events after the rebuild: an indexed segment's tail and a new segment are read too.
    with store.log.open_writer("r1", "w2") as w:
        w.append(doc_id="w2-d3", node="tail", kind="review")
    with store.log.open_writer("r1", "w9") as w:
        w.append(doc_id="w2-d3", node="new", kind="output")
    assert read("w2-d3") == truth("w2-d3") and len(truth("w2-d3")) == 32
    assert set(seen) == {"w2.jsonl", "w9.jsonl"} and len(seen) <= 152
    # A segment shorter than the index remembers: fall back to the whole run.
    seg = store.root / "runs/r1/segments/w1.jsonl"
    os.truncate(seg, seg.stat().st_size - 10)
    assert read("w1-d0") == truth("w1-d0")
    assert len(seen) > 600
    # An index that does not know the run, or is gone, falls back as well.
    assert read("absent") == [] == truth("absent")
    (tmp_path / "i.sqlite").unlink()
    assert read("w0-d0") == truth("w0-d0")


_LAYOUT = st.lists(st.tuples(st.integers(0, 3), st.integers(0, 2), st.integers(0, 4)), max_size=30)


@given(layout=_LAYOUT, cut=st.integers(0, 30))
def test_per_document_reads_equal_the_log_for_any_layout(layout: list[tuple[int, int, int]], cut: int) -> None:
    # Any placement of (doc, segment, seq) lines, forks, gaps and out-of-order lines
    # included; part of the history is written after the rebuild (unindexed tails).
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log = EventLog(root, BlobStore(root))
        log.create_run(Run(**run_fields()))
        idx = Index(root / "i.sqlite")

        def write(items: list[tuple[int, int, int]]) -> None:
            for i, (doc, seg, seq) in enumerate(items):
                write_lines(root, f"s{seg}.jsonl", [make_event(doc_id=f"d{doc}", seq=seq, node=f"n{i}", inputs=[])])

        write(layout[:cut])
        idx.reindex(log)
        write(layout[cut:])
        for doc in [f"d{d}" for d in range(4)]:
            want = [(e.seq, e.hash) for e in log.iter_events("r1", doc)]
            assert [(e.seq, e.hash) for e in idx.iter_events(log, "r1", doc)] == want, doc


# --- concurrency -------------------------------------------------------------------


def test_threads_applying_while_another_rebuilds_end_equal_to_the_log(store: SimpleNamespace, tmp_path: Path) -> None:
    idx = built(store, tmp_path / "i.sqlite")
    errors: list[BaseException] = []
    done = threading.Event()
    rebuilds = [0]

    def work(k: int) -> None:
        try:
            with store.log.open_writer("r1", f"t{k}", flush_every=1000) as w:
                for i in range(25):
                    evs = [w.append(doc_id=f"t{k}-d{j}", node=f"n{i}", kind="review" if i % 7 == 0 else "decision") for j in range(3)]
                    w.flush()
                    for e in evs:
                        idx.apply(e)  # only after the flush that made it durable
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def rebuild() -> None:
        try:
            while not done.is_set() and rebuilds[0] < 15:
                Index(tmp_path / "i.sqlite").reindex(store.log)
                rebuilds[0] += 1
                time.sleep(0.01)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    rebuilder = threading.Thread(target=rebuild)
    rebuilder.start()
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    done.set()
    rebuilder.join()
    assert errors == [] and rebuilds[0] > 1  # rebuilds really ran while threads applied
    assert idx.drift(store.log) == []
    assert sum(r.events for r in idx.runs()) == 4 * 25 * 3


# --- memory and leaks --------------------------------------------------------------


def test_rebuild_and_per_document_reads_stream_a_large_run(store: SimpleNamespace, tmp_path: Path) -> None:
    pad = "f" * (512 * 1024)
    for k, docs in enumerate((["a", "b"], ["b", "c"], ["d"])):
        with store.log.open_writer("r1", f"w{k}", flush_every=2) as w:
            for i in range(8):
                for doc in docs:
                    w.append(doc_id=doc, node=f"n{i}", kind="handoff", payload={"pad": pad})
    total = sum(p.stat().st_size for p in store.log.segment_paths("r1"))
    assert total > 20 * 1024 * 1024
    idx = Index(tmp_path / "i.sqlite")
    bound = 6 * 1024 * 1024
    assert peak_bytes(lambda: idx.reindex(store.log)) < bound
    assert peak_bytes(lambda: list(e.seq for e in idx.iter_events(store.log, "r1", "b"))) < bound
    assert peak_bytes(lambda: idx.drift(store.log)) < bound
    assert [(d.doc_id, d.events) for d in idx.documents("r1")] == [("a", 8), ("b", 16), ("c", 8), ("d", 8)]


def test_no_table_holds_document_text_payloads_or_causes(store: SimpleNamespace, tmp_path: Path) -> None:
    sha = store.blobs.put(f"{SENTINEL} original text".encode(), "original")
    with store.log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="n", kind="review", inputs=[sha], cause=SENTINEL,
                 decision={"note": SENTINEL}, payload={"why": SENTINEL})
    write_lines(store.root, "bad.jsonl", [v1_event_dict(doc_id="d2", inputs=[]) | {"kind": SENTINEL}])
    p = tmp_path / "i.sqlite"
    stats = Index(p).reindex(store.log)
    assert [pr.kind for pr in stats.problems] == ["invalid_event"]
    on_disk = b"".join(f.read_bytes() for f in tmp_path.glob("i.sqlite*") if f.is_file())
    segments = b"".join(s.read_bytes() for s in store.log.segment_paths("r1"))
    assert SENTINEL.encode() in segments  # positive control: the log does carry it
    assert SENTINEL.encode() not in on_disk
    con = sqlite3.connect(p)
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        for row in con.execute(f"SELECT * FROM {table}"):
            assert SENTINEL not in json.dumps(row), table
    con.close()


# --- fix pass 2: per-document reads are never silent (Task 8 review MAJOR 2, 3) ---------


def _both(store: SimpleNamespace, idx: Index, doc: str) -> tuple[Any, Any]:
    """What the log and the index give for ``doc``: the (seq, hash) list, or the error type."""
    out = []
    for read in (lambda: store.log.iter_events("r1", doc), lambda: idx.iter_events(store.log, "r1", doc)):
        try:
            out.append([(e.seq, e.hash) for e in read()])
        except Exception as exc:  # noqa: BLE001 - the error type is the observable
            out.append(type(exc).__name__)
    return out[0], out[1]


@pytest.mark.parametrize("where", ["first", "last", "middle"])
def test_an_invalid_line_of_the_document_is_never_dropped_by_the_index(store: SimpleNamespace, tmp_path: Path, where: str) -> None:
    # d1's line at one end (or in the middle) of its run of lines is valid JSON with doc_id
    # and seq but an invalid field. The rebuild cannot say whose it is, so it is in no
    # span; the index read must not quietly return d1 without it.
    c = chain("d1", 3)
    bad = c[{"first": 0, "last": 2, "middle": 1}[where]].model_dump(mode="json")
    bad["kind"] = 12345
    lines: list[Any] = [*c]
    lines[{"first": 0, "last": 2, "middle": 1}[where]] = bad
    write_lines(store.root, "w0.jsonl", chain("d0", 2) + lines + chain("d2", 2))
    idx = built(store, tmp_path / "i.sqlite")
    assert "invalid_event" in [p.kind for p in idx.problems()]  # plus a seq_gap when it is not the last
    log_says, index_says = _both(store, idx, "d1")
    assert log_says == index_says == "InvalidEvent"
    # Documents without the bad line still read, and equal the log, through the index.
    for doc in ("d0", "d2"):
        assert _both(store, idx, doc)[1] == [(e.seq, e.hash) for e in store.log.iter_events("r1", doc)]


@pytest.mark.parametrize(
    "rewrite",
    [
        "reordered_same_size",
        "moved_earlier_and_grown",
        "middle_swapped_edges_kept",
        "replaced_by_other_run_copy",
        "new_document_inside_indexed_bytes",
    ],
)
def test_a_segment_rewritten_under_the_index_is_read_from_the_log(store: SimpleNamespace, tmp_path: Path, rewrite: str) -> None:
    pad = {"pad": "p" * 600}  # lines of a few hundred bytes, so 4 KiB edges hold several
    a, b = chain("d1", 3, payload=pad), chain("d2", 3, payload=pad)
    filler = chain("f", 12, payload=pad)
    seg = store.root / "runs/r1/segments/w0.jsonl"
    if rewrite == "middle_swapped_edges_kept":
        first = filler[:6]
        last = filler[6:]
        write_lines(store.root, "w0.jsonl", first + a + b + last)
    elif rewrite == "moved_earlier_and_grown":
        write_lines(store.root, "w0.jsonl", b + a)
    else:
        write_lines(store.root, "w0.jsonl", a + b)
    idx = built(store, tmp_path / "i.sqlite")
    assert _both(store, idx, "d1")[1] == [(e.seq, e.hash) for e in a]
    seg.unlink()
    if rewrite == "reordered_same_size":
        write_lines(store.root, "w0.jsonl", b + a)  # same bytes, d1 now after d2
    elif rewrite == "moved_earlier_and_grown":
        write_lines(store.root, "w0.jsonl", a + b + chain("d3", 1))
    elif rewrite == "middle_swapped_edges_kept":
        write_lines(store.root, "w0.jsonl", first + b + a + last)  # first and last 4 KiB unchanged
    elif rewrite == "replaced_by_other_run_copy":  # same layout, other hashes (another history restored)
        write_lines(store.root, "w0.jsonl", chain("d1", 3, payload=pad, agent="other") + b)
    else:  # d5, unknown to the index (no head to check), now has lines inside the indexed bytes
        write_lines(store.root, "w0.jsonl", chain("d5", 3, payload=pad) + a + b)
    doc = "d5" if rewrite == "new_document_inside_indexed_bytes" else "d1"
    log_says, index_says = _both(store, idx, doc)
    assert index_says == log_says and isinstance(log_says, list) and len(log_says) == 3


def test_the_head_check_alone_catches_moved_lines(store: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # With the fingerprint check disabled, a rewrite that keeps the fingerprint is still
    # caught because the recorded ranges no longer hold d1's indexed chain.
    from mailroom_record import index as index_module

    a, b = chain("d1", 3), chain("d2", 3)
    seg = write_lines(store.root, "w0.jsonl", a + b)
    idx = built(store, tmp_path / "i.sqlite")
    seg.unlink()
    write_lines(store.root, "w0.jsonl", b + a)
    monkeypatch.setattr(index_module, "_fingerprint", lambda path, upto: "same")
    with sqlite3.connect(tmp_path / "i.sqlite") as con:
        con.execute("UPDATE segments SET fingerprint = 'same'")
    log_says, index_says = _both(store, idx, "d1")
    assert index_says == log_says == [(e.seq, e.hash) for e in a]


def test_the_head_check_compares_the_head_hash_not_only_its_seq(store: SimpleNamespace, tmp_path: Path) -> None:
    # Same-length rewrites in the middle of the segment, edges and size kept: d1's head
    # line A moves out of the recorded ranges (into another document's line), and a forged
    # seq-1 line B of the same length takes its place. The ranges still hold seqs 0 and 1,
    # so only the head hash shows they no longer hold the indexed chain.
    from mailroom_record.hashing import canonical_json

    def line(e: Any) -> bytes:
        return canonical_json(e.model_dump(mode="json")) + b"\n"

    def same_length_as(target: bytes, build: Any) -> Any:
        return next(e for k in range(300) if len(line(e := build("x" * k))) == len(target))

    pad = {"pad": "p" * 1000}
    d0, a = chain("d1", 2)
    b = same_length_as(line(a), lambda agent: chain("d1", 1, start=1, prev=d0.hash, agent=agent)[0])
    y = same_length_as(line(a), lambda agent: chain("y", 1, agent=agent)[0])
    assert b.hash != a.hash
    before = [*chain("p", 6, payload=pad), d0, a, *chain("m", 2, payload=pad), y, *chain("q", 6, payload=pad)]
    seg = write_lines(store.root, "w0.jsonl", before)
    idx = built(store, tmp_path / "i.sqlite")
    data = seg.read_bytes()
    a_at, y_at = data.index(line(a)), data.index(line(y))
    assert 4096 < a_at and y_at + len(line(y)) < len(data) - 4096  # both away from the fingerprinted edges
    data = data[:a_at] + line(b) + data[a_at + len(line(a)) : y_at] + line(a) + data[y_at + len(line(y)) :]
    seg.write_bytes(data)  # same size, same first and last 4096 bytes
    log_says, index_says = _both(store, idx, "d1")
    assert log_says == [(0, d0.hash), (1, b.hash), (1, a.hash)]  # a fork: both seq-1 lines
    assert index_says == log_says


# --- fix pass 2: a busy index is a typed, retryable error (Task 8 review MINOR 4) --------


def test_apply_locked_out_by_a_long_rebuild_is_index_unavailable_busy(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    store = store_with_events
    path = tmp_path / "i.sqlite"
    built(store, path)
    idx = Index(path, busy_timeout=0.2)
    e = append(store, doc_id="d3", node="n", kind="decision")
    hold = sqlite3.connect(path, isolation_level=None)
    hold.execute("BEGIN IMMEDIATE")  # stands in for a rebuild's write transaction
    try:
        with pytest.raises(IndexUnavailable) as caught:
            idx.apply(e)
        assert caught.value.status == "busy" and "retry" in str(caught.value)
        assert [d.doc_id for d in idx.documents("r1")] == ["d1", "d2"]  # readers do not wait
    finally:
        hold.execute("ROLLBACK")
        hold.close()
    idx.apply(e)  # the retry succeeds and nothing was half-applied
    assert idx.drift(store.log) == []


def test_rebuild_locked_out_by_another_writer_is_index_unavailable_busy(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    # reindex and ensure take the write lock too: past busy_timeout they raise the same
    # typed, retryable error as apply, never sqlite3.OperationalError ("database is locked").
    store = store_with_events
    path = tmp_path / "i.sqlite"
    built(store, path)
    idx = Index(path, busy_timeout=0.2)
    hold = sqlite3.connect(path, isolation_level=None)
    hold.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")  # ensure must rebuild
    hold.execute("BEGIN IMMEDIATE")  # another rebuild, or an apply, holding the write lock
    try:
        assert idx.status() == "old_version"
        for rebuild in (idx.reindex, idx.ensure):
            with pytest.raises(IndexUnavailable) as caught:
                rebuild(store.log)
            assert caught.value.status == "busy" and "retry" in str(caught.value), rebuild.__name__
        assert idx.status() == "old_version"  # nothing half-built
    finally:
        hold.execute("ROLLBACK")
        hold.close()
    assert idx.ensure(store.log) is not None and idx.status() == "ready"  # the retry succeeds
    assert idx.drift(store.log) == []


# --- fix pass 2: the review rule for a document that opens with a review (MINOR 8) -------


def test_a_document_whose_first_event_is_a_review_is_in_the_queue(store: SimpleNamespace, tmp_path: Path) -> None:
    idx = built(store, tmp_path / "i.sqlite")
    idx.add_run(store.run)
    first = append(store, doc_id="d9", node="triage", kind="review")
    assert first.seq == 0
    idx.apply(first)
    assert [(d.doc_id, d.events, d.in_review) for d in idx.review_queue("r1")] == [("d9", 1, True)]
    rebuilt = built(store, tmp_path / "j.sqlite")
    assert [d.doc_id for d in rebuilt.review_queue("r1")] == ["d9"]
    idx.apply(append(store, doc_id="d9", node="triage", kind="decision"))
    assert idx.review_queue("r1") == [] and idx.drift(store.log) == []


# --- fix pass 3: re-review findings (PR #2) -------------------------------------------


def test_a_directory_that_is_not_a_run_is_skipped(store_with_events: SimpleNamespace, tmp_path: Path) -> None:
    # runs/.staging (an import in progress) is no run id; it used to abort the rebuild with
    # ValueError: bad run id '.staging'.
    store = store_with_events
    (store.root / "runs" / ".staging" / "segments").mkdir(parents=True)
    (store.root / "runs" / "-dash").mkdir()
    idx = Index(tmp_path / "i.sqlite")
    stats = idx.reindex(store.log)
    assert stats.problems == () and [r.run_id for r in idx.runs()] == ["r1"]
    assert idx.drift(store.log) == []


def test_a_line_stored_twice_in_two_segments_is_a_fork(tmp_path: Path) -> None:
    # The same event (same seq, same hash) in two segments is a forked chain, as verify_run
    # reports it; it is not an apply replay.
    from mailroom_record.verify import verify_run

    blobs = BlobStore(tmp_path)
    log = EventLog(tmp_path, blobs)
    log.create_run(Run(**run_fields()))
    d = chain("d", 3, inputs=[])
    write_lines(tmp_path, "a.jsonl", d)
    write_lines(tmp_path, "b.jsonl", [d[1]])  # seq 1 again, byte for byte
    stats = Index(tmp_path / "i.sqlite").reindex(log)
    assert [(p.doc_id, p.kind, p.segment) for p in stats.problems] == [("d", "forked_chain", "b.jsonl")]
    assert "forked_chain" in {p.kind for p in verify_run(log, blobs, "r1").problems}  # the same verdict


def _racing_handoffs(store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, times: int) -> list[bool]:
    """After each whole read of a.jsonl, ``a`` writes d1's next seq there and ``b`` the one after.

    Every scan then sees a gap (the event ``a`` wrote after the scan passed a.jsonl), and
    every re-read sees a new one: repeated hand-offs between segments.
    """
    from record_testkit import append_more

    append_more(store, doc_id="d2", n=1, writer_id="b")  # b.jsonl exists before any scan lists it
    append_more(store, doc_id="d1", n=3, writer_id="a")
    original = log_module._stream_lines
    fired: list[bool] = []
    busy: list[bool] = []  # a writer's own recovery read streams a.jsonl too: no re-entry

    def racing(path: Path, start: int = 0, end: int | None = None) -> Any:
        yield from original(path, start, end)
        if path.name == "a.jsonl" and start == 0 and end is None and len(fired) < times and not busy:
            fired.append(True)
            busy.append(True)
            try:
                append_more(store, doc_id="d1", n=1, writer_id="a")
                append_more(store, doc_id="d1", n=1, writer_id="b")
            finally:
                busy.clear()

    monkeypatch.setattr(log_module, "_stream_lines", racing)
    return fired


def test_repeated_handoffs_during_every_reread_are_not_recorded_as_a_gap(
    store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fired = _racing_handoffs(store, monkeypatch, times=10)
    idx = Index(tmp_path / "i.sqlite")
    stats = idx.reindex(store.log)
    assert len(fired) >= 3  # every read raced, past the old retry limit
    assert stats.problems == ()  # no false seq_gap
    monkeypatch.undo()
    (d1,) = [d for d in idx.documents("r1") if d.doc_id == "d1"]
    stats = idx.reindex(store.log)  # quiet now: the whole chain, still no problem
    assert stats.problems == () and d1.events <= [d for d in idx.documents("r1") if d.doc_id == "d1"][0].events


def test_a_real_gap_is_still_recorded_while_writers_hand_off(
    store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Positive control: a gap below what an earlier read already saw is real damage.
    g = chain("g", 4, inputs=[])
    write_lines(store.root, "c.jsonl", [g[0], g[1], g[3]])
    _racing_handoffs(store, monkeypatch, times=10)
    stats = Index(tmp_path / "i.sqlite").reindex(store.log)
    assert [(p.doc_id, p.kind) for p in stats.problems] == [("g", "seq_gap")]


def test_a_per_document_read_does_not_report_handoffs_as_a_gap(
    store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailroom_record._reading import read_document

    fired = _racing_handoffs(store, monkeypatch, times=10)
    got = read_document(store.log, None, "r1", "d1")
    assert len(fired) >= 3
    assert [d.kind for d in got.damage] == []
    assert [e.seq for e in got.events] == list(range(len(got.events)))  # contiguous, from 0


def test_a_runs_listing_error_does_not_make_trace_report_runs_deleted(
    store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailroom_record._reading import list_runs

    assert list_runs(store.log) == (["r1"], None)
    runs = store.root / "runs"
    real_iterdir = Path.iterdir

    def iterdir(self: Path) -> Any:
        if self == runs:
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    ids, damage = list_runs(store.log)
    assert ids == [] and damage is not None and damage.kind == "unreadable"
    assert "PermissionError" in damage.detail and str(store.root) not in damage.detail  # no local path


def test_a_store_with_no_runs_folder_lists_no_runs_and_no_damage(tmp_path: Path) -> None:
    from mailroom_record._reading import list_runs

    assert list_runs(EventLog(tmp_path, BlobStore(tmp_path))) == ([], None)


def test_a_failed_worker_start_leaves_no_worker_running(store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    # The second worker fails to start: the first, already running, must be stopped and reaped.
    from multiprocessing.context import SpawnProcess

    from record_testkit import start_handoff_workers

    real_start = SpawnProcess.start
    seen: list[Any] = []

    def start(self: Any) -> None:
        seen.append(self)
        if len(seen) == 2:
            raise RuntimeError("injected: the second worker cannot start")
        real_start(self)

    monkeypatch.setattr(SpawnProcess, "start", start)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            start_handoff_workers(store.root, 3)
        assert len(seen) == 2 and seen[0].pid is not None
        assert not seen[0].is_alive() and seen[0].exitcode is not None  # stopped and reaped
    finally:
        join_all(seen, timeout=30)  # never leave a child behind, even when the assert fails
