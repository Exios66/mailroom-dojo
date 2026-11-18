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
    deadline = time.monotonic() + 60
    while not marker.exists():
        assert proc.is_alive() and time.monotonic() < deadline
        time.sleep(0.02)
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(timeout=30)
    assert proc.exitcode == -signal.SIGKILL
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
    ctx = multiprocessing.get_context("spawn")
    reader = ctx.Process(target=read_index_until, args=(str(p), stop, str(out)))
    reader.start()
    try:
        Path(stop + ".building").touch()
        slow = slow_log(store.root, store.blobs, marker=str(tmp_path / "mid"), after=3, pause=1.5)
        time.sleep(0.5)  # let the reader start reading the old index
        Index(p).reindex(slow)
        os.unlink(stop + ".building")
        time.sleep(0.2)
    finally:
        Path(stop).touch()
        reader.join(timeout=60)
    assert reader.exitcode == 0
    seen = json.loads(out.read_text())
    assert {d for _, d in seen} <= {old, new}, {d for _, d in seen} - {old, new}
    assert any(b and d == old for b, d in seen)  # positive control: it read mid-build
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
