"""Task 9: the version chain and trace, on real on-disk stores, clean and damaged."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from record_testkit import (
    SENTINEL,
    append_torn_tail,
    blob_bytes,
    build_review_store,
    build_two_run_store,
    built_index,
    find_line,
    forge_event,
    garble_line,
    insert_line_after,
    remove_blob_file,
    rewrite_line,
    run_cli,
    run_fields,
)

from mailroom_record._reading import read_document
from mailroom_record.hashing import hash_obj
from mailroom_record.index import Index
from mailroom_record.log import CorruptLine, EventLog, RunScan
from mailroom_record.models import Run
from mailroom_record.trace import BadRunId, UnknownRun, digest, to_json, trace_doc, trace_run
from mailroom_record.versions import read_versions, version_chain


@pytest.fixture
def review_store(tmp_path: Path) -> SimpleNamespace:
    return build_review_store(tmp_path / "store")


@pytest.fixture
def two_run_store(tmp_path: Path) -> SimpleNamespace:
    return build_two_run_store(tmp_path / "store")


def _kinds(damage) -> set[str]:
    return {d.kind for d in damage}


# --- the plan's sketches ---------------------------------------------------------------


def test_version_chain_orders_original_to_correction(review_store: SimpleNamespace) -> None:
    roles = [v.role for v in version_chain(review_store.log, "r1", "d1")]
    assert roles == ["original", "transcript", "extraction", "extraction", "archived_record", "human_correction"]


def test_trace_doc_lists_every_run(two_run_store: SimpleNamespace, tmp_path: Path) -> None:
    t = trace_doc(two_run_store.log, built_index(two_run_store, tmp_path), "d1")
    assert [r.run_id for r in t.runs] == ["r1", "r2"]


# --- version chain ---------------------------------------------------------------------


def test_version_chain_names_each_blob_once_at_its_first_use(review_store: SimpleNamespace) -> None:
    chain = version_chain(review_store.log, "r1", "d1")
    shas = review_store.shas
    assert [v.sha256 for v in chain] == [
        shas["original"], shas["transcript"], *shas["extraction"], shas["archived_record"], shas["human_correction"]
    ]
    assert [v.order for v in chain] == list(range(6))
    # first use: the archived record is named by seq 4 (output), 5 and 6 (inputs): listed once, at 4
    assert [(v.node, v.attempt, v.seq, v.used_as) for v in chain] == [
        ("intake", 0, 0, "output"), ("transcribe", 0, 1, "output"), ("extract", 0, 2, "output"),
        ("extract", 1, 3, "output"), ("archive", 0, 4, "output"), ("human_review", 0, 6, "output"),
    ]
    assert all(v.state == "present" for v in chain)
    assert chain[0].size == len(blob_bytes("original d1"))
    # timestamps are the events' own
    events = list(review_store.log.iter_events("r1", "d1"))
    assert [v.ts for v in chain] == [events[v.seq].ts for v in chain]


def test_inputs_come_before_outputs_within_one_event(tmp_path: Path) -> None:
    from mailroom_record.blobs import BlobStore

    blobs = BlobStore(tmp_path)
    log = EventLog(tmp_path, blobs)
    log.create_run(Run(**run_fields()))
    a, b = blobs.put(b"in", "transcript"), blobs.put(b"out", "extraction")
    with log.open_writer("r1", "w0") as w:
        w.append(doc_id="d", node="extract", kind="output", inputs=[a], outputs=[b])
    assert [(v.role, v.used_as) for v in version_chain(log, "r1", "d")] == [("transcript", "input"), ("extraction", "output")]


def test_tombstoned_and_missing_blobs_keep_their_place(review_store: SimpleNamespace) -> None:
    shas = review_store.shas
    review_store.blobs.tombstone(shas["extraction"][0], "deletion request")
    remove_blob_file(review_store, shas["transcript"])
    states = {v.sha256: v.state for v in version_chain(review_store.log, "r1", "d1")}
    assert states[shas["extraction"][0]] == "tombstoned"
    assert states[shas["transcript"]] == "missing"
    assert list(states.values()).count("present") == 4


def test_a_corrupt_blob_shows_as_present(review_store: SimpleNamespace) -> None:
    """Documented: the chain checks existence only; verify_run reports corrupt_blob."""
    from record_testkit import flip_byte, verify

    sha = review_store.shas["original"]
    flip_byte(review_store.blobs.content_path(sha), 10)
    assert [v.state for v in version_chain(review_store.log, "r1", "d1")][0] == "present"
    assert "corrupt_blob" in {p.kind for p in verify(review_store).problems}


def test_version_chain_of_a_document_not_in_the_run_is_empty(review_store: SimpleNamespace) -> None:
    assert version_chain(review_store.log, "r1", "nope") == []
    assert version_chain(review_store.log, "r1", "d3") == []  # events, but no blobs


@pytest.mark.parametrize("bad", ["../r1", "a/b", "", ".", "..", "-x", "r 1", "x" * 201])
def test_malformed_run_ids_raise_bad_run_id(review_store: SimpleNamespace, bad: str) -> None:
    for call in (
        lambda: version_chain(review_store.log, bad, "d1"),
        lambda: trace_run(review_store.log, None, bad),
    ):
        with pytest.raises(BadRunId):
            call()


def test_unknown_run_raises_unknown_run(review_store: SimpleNamespace) -> None:
    with pytest.raises(UnknownRun):
        version_chain(review_store.log, "r9", "d1")
    with pytest.raises(UnknownRun):
        trace_run(review_store.log, None, "r9")


# --- trace run -------------------------------------------------------------------------


def test_trace_run_follows_each_document(review_store: SimpleNamespace, tmp_path: Path) -> None:
    t = trace_run(review_store.log, Index(tmp_path / "i.sqlite"), "r1")
    assert t.header is not None and (t.header.kind, t.header.declared_documents) == ("full_pipeline", 3)
    assert [d.doc_id for d in t.documents] == ["d1", "d2", "d3"]
    assert (t.events, t.in_review, t.seal_files, t.damage, t.damaged) == (11, 1, 0, (), False)
    assert t.index == "rebuilt from the log"  # ensure built the missing index
    d1, d2, d3 = t.documents
    assert d1.nodes == ("intake", "transcribe", "extract", "archive", "human_review")
    assert d1.attempts == {"intake": 1, "transcribe": 1, "extract": 2, "archive": 1, "human_review": 1}
    assert (d1.events, d1.head_seq, d1.last_kind, d1.in_review) == (7, 6, "output", False)
    assert (d1.last_decision.seq, d1.last_decision.node) == (5, "human_review")
    assert d1.last_decision.digest == digest({"state": "paused"})
    assert d1.last_cause.seq == 2
    cause = f"{SENTINEL} quoted in a cause"
    assert d1.last_cause.digest.sha256 == hashlib.sha256(cause.encode()).hexdigest()
    assert (d2.in_review, d2.last_node, d2.last_decision.digest.keys) == (True, "human_review", ("answer", "question"))
    assert (d3.events, d3.last_decision, d3.last_cause) == (1, None, None)
    assert SENTINEL not in to_json(t)


def test_digest_shows_shape_keys_and_hash_never_values() -> None:
    d = digest({"verdict": SENTINEL, "p": 0.5})
    assert (d.shape, d.keys, d.size, d.sha256) == ("object", ("p", "verdict"), 2, hash_obj({"verdict": SENTINEL, "p": 0.5}))
    assert digest([{"q": 1}, {"a": SENTINEL}, SENTINEL]).keys == ("a", "q")
    assert digest(SENTINEL) == digest(SENTINEL) and digest(SENTINEL) != digest(SENTINEL + "x")
    assert digest("\ud800").shape == "string"  # a lone surrogate (valid JSON) does not crash
    assert digest(None) is None and digest(True).shape == "boolean" and digest(3).shape == "number"
    assert SENTINEL not in json.dumps([str(digest(v)) for v in ({"x": SENTINEL}, [SENTINEL], SENTINEL)])


def test_index_note_says_when_the_index_is_behind_the_log(review_store: SimpleNamespace, tmp_path: Path) -> None:
    idx = built_index(review_store, tmp_path)
    assert trace_run(review_store.log, idx, "r1").index == "ready"
    with review_store.log.open_writer("r1", "w1") as w:
        w.append(doc_id="d3", node="classify", kind="decision")
    t = trace_run(review_store.log, idx, "r1")
    assert "behind the log: 1 document(s) differ" in t.index
    assert t.documents[2].events == 2  # the log is the truth, not the index


def test_a_segment_that_fails_mid_read_keeps_what_was_read(review_store: SimpleNamespace, monkeypatch) -> None:
    real = RunScan.events

    def failing(self):
        it = real(self)
        yield next(it)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(RunScan, "events", failing)
    t = trace_run(review_store.log, None, "r1")
    assert [d.events for d in t.documents] == [1]
    assert _kinds(t.damage) == {"unreadable"} and t.damaged


# --- trace doc and the index ----------------------------------------------------------


def test_trace_doc_gives_each_run_its_steps_and_versions(review_store: SimpleNamespace, tmp_path: Path) -> None:
    # a second run holding d1 again: same original blob, one step
    review_store.log.create_run(Run(**run_fields(run_id="r2", created_at="2026-11-19T00:00:00Z")))
    original = review_store.blobs.put(blob_bytes("original d1"), "original")
    with review_store.log.open_writer("r2", "w0") as w:
        w.append(doc_id="d1", node="intake", kind="handoff", outputs=[original])
    t = trace_doc(review_store.log, built_index(review_store, tmp_path), "d1")
    r1, r2 = t.runs
    assert (r1.run_id, r2.run_id) == ("r1", "r2")
    assert [s.node for s in r1.steps] == ["intake", "transcribe", "extract", "extract", "archive", "human_review", "human_review"]
    assert len(r1.versions) == 6 and [v.role for v in r2.versions] == ["original"]
    assert r1.versions[0].sha256 == r2.versions[0].sha256  # one blob, two runs
    assert (r1.source, r2.source, t.index) == ("index", "index", "ready")
    assert not t.damaged and SENTINEL not in to_json(t)


def test_trace_doc_reads_through_the_index(two_run_store: SimpleNamespace, tmp_path: Path, monkeypatch) -> None:
    idx = built_index(two_run_store, tmp_path)
    calls: list[tuple[str, object]] = []
    real = EventLog.scan

    def spy(self, run_id, doc_id=None, *, spans=None, strict=True):
        calls.append((run_id, spans))
        return real(self, run_id, doc_id, spans=spans, strict=strict)

    monkeypatch.setattr(EventLog, "scan", spy)
    t = trace_doc(two_run_store.log, idx, "d2")
    assert [r.run_id for r in t.runs] == ["r1"]
    assert calls and all(spans is not None for _, spans in calls)  # never a whole-run scan
    assert [spans for rid, spans in calls if rid == "r2"] == [{}]  # nothing of r2 is read


def test_trace_doc_finds_runs_and_events_the_index_has_not_seen(two_run_store: SimpleNamespace, tmp_path: Path) -> None:
    idx = built_index(two_run_store, tmp_path)
    log = two_run_store.log
    log.create_run(Run(**run_fields(run_id="r3", created_at="2026-11-18T14:00:00Z")))
    with log.open_writer("r3", "w0") as w:
        w.append(doc_id="d1", node="classify", kind="decision")
    with log.open_writer("r1", "w1") as w:
        w.append(doc_id="d1", node="review", kind="review")
    t = trace_doc(log, idx, "d1")
    assert [r.run_id for r in t.runs] == ["r1", "r2", "r3"]
    assert t.runs[0].summary.events == 3 and t.runs[0].summary.in_review
    assert t.index == "ready"


@pytest.mark.parametrize("how", ["none", "directory"])
def test_trace_doc_without_a_usable_index_reads_the_log(two_run_store: SimpleNamespace, tmp_path: Path, how: str) -> None:
    if how == "none":
        idx = None
    else:
        (tmp_path / "idx").mkdir()
        idx = Index(tmp_path / "idx")  # a directory: cannot be opened or rebuilt
    t = trace_doc(two_run_store.log, idx, "d1")
    assert [r.run_id for r in t.runs] == ["r1", "r2"]
    assert {r.source for r in t.runs} == {"log"}
    assert t.index.startswith("not used" if how == "none" else "unavailable")
    assert trace_run(two_run_store.log, idx, "r1").index.startswith("not used" if how == "none" else "unavailable")


def test_a_run_the_index_lists_but_the_log_lost_is_reported(two_run_store: SimpleNamespace, tmp_path: Path) -> None:
    idx = built_index(two_run_store, tmp_path)
    for seg in two_run_store.log.segment_paths("r2"):
        seg.write_bytes(b"")
    t = trace_doc(two_run_store.log, idx, "d1")
    assert [r.run_id for r in t.runs] == ["r1", "r2"]
    assert _kinds(t.runs[1].summary.damage) == {"missing_from_log"} and t.damaged


def test_a_read_that_raises_after_yielding_is_read_again_from_the_log(review_store: SimpleNamespace, tmp_path: Path, monkeypatch) -> None:
    """Index.iter_events can yield events, then raise on a later bad line: never half a document."""
    idx = built_index(review_store, tmp_path)
    real = Index.iter_events

    def half(self, log, run_id, doc_id):
        it = real(self, log, run_id, doc_id)
        yield next(it)
        yield next(it)
        raise CorruptLine("w0.jsonl at byte 1: not JSON")

    monkeypatch.setattr(Index, "iter_events", half)
    read = read_document(review_store.log, idx, "r1", "d1")
    assert (read.source, len(read.events), read.damage) == ("log", 7, ())


# --- damaged runs: every kind shown, nothing raised ---------------------------------------


def _damage(store: SimpleNamespace, kind: str) -> dict:
    """Apply one kind of damage; return what the trace must show."""
    log, shas = store.log, store.shas
    if kind == "tombstoned_blob":
        store.blobs.tombstone(shas["extraction"][0], "deletion request")
        return {"run": set(), "doc": set(), "state": ("tombstoned", 2), "verify_ok": True, "events": 7}
    if kind == "missing_blob":
        remove_blob_file(store, shas["transcript"])
        return {"run": set(), "doc": set(), "state": ("missing", 1), "verify_ok": False, "events": 7}
    if kind == "forked_chain":
        prev = next(e for e in log.iter_events("r1", "d1") if e.seq == 2).hash
        forge_event(store, "d1", 3, prev, cause=SENTINEL, decision={"x": SENTINEL})
        return {"run": set(), "doc": {"forked_chain"}, "verify_ok": False, "events": 8}
    if kind == "corrupt_line":
        insert_line_after(store, "d2", 1, f'{{"not json {SENTINEL}'.encode())
        return {"run": {"corrupt_line"}, "doc": set(), "verify_ok": False, "events": 7}
    if kind == "corrupt_event_line":
        garble_line(store, "d1", 3, f"{SENTINEL} garbled".encode())
        # extraction B is still in the chain: seq 4 names it as an input
        return {"run": {"corrupt_line"}, "doc": {"seq_gap"}, "verify_ok": False, "events": 6, "first_b": (4, "input")}
    if kind == "invalid_event":
        bad = {"doc_id": "d1", "seq": 7, "kind": "bogus", "cause": SENTINEL, "payload": {"t": SENTINEL}}
        insert_line_after(store, "d1", 6, json.dumps(bad).encode())
        return {"run": {"invalid_event"}, "doc": set(), "verify_ok": False, "events": 7}
    if kind == "torn_tail":
        append_torn_tail(store, f'{{"doc_id": "d1", "cause": "{SENTINEL}'.encode())
        return {"run": {"torn_tail"}, "doc": set(), "verify_ok": True, "events": 7}
    if kind == "seq_gap":
        seg, off, line = find_line(store, "d1", 2)
        seg.write_bytes(seg.read_bytes().replace(line + b"\n", b""))
        return {"run": set(), "doc": {"seq_gap"}, "verify_ok": False, "events": 6, "versions": 5}  # extraction A lost
    if kind == "broken_link":
        rewrite_line(store, "d1", 4, recompute_hash=True, prev_hash="f" * 64)
        return {"run": set(), "doc": {"broken_link"}, "verify_ok": False, "events": 7}
    if kind == "corrupt_run_header":
        (log.run_dir("r1") / "run.json").write_bytes(f'{{"run_id": "{SENTINEL}"'.encode())
        return {"run": {"corrupt_run_header"}, "doc": set(), "verify_ok": False, "events": 7}
    raise AssertionError(kind)


DAMAGE_KINDS = [
    "tombstoned_blob", "missing_blob", "forked_chain", "corrupt_line", "corrupt_event_line",
    "invalid_event", "torn_tail", "seq_gap", "broken_link", "corrupt_run_header",
]


@pytest.mark.parametrize("kind", DAMAGE_KINDS)
def test_damage_is_shown_never_raised(review_store: SimpleNamespace, tmp_path: Path, kind: str) -> None:
    idx = built_index(review_store, tmp_path)  # built on the clean log: the damage comes after
    want = _damage(review_store, kind)
    log = review_store.log

    t = trace_run(log, idx, "r1")
    d1 = next(d for d in t.documents if d.doc_id == "d1")
    assert _kinds(t.damage) == want["run"]
    assert _kinds(d1.damage) == want["doc"]
    assert d1.events == want["events"]
    assert t.damaged == bool((want["run"] - {"torn_tail"}) or want["doc"])
    others = [d for d in t.documents if d.doc_id != "d1"]
    assert [(d.doc_id, d.events, d.damage) for d in others] == [("d2", 3, ()), ("d3", 1, ())]
    if kind == "corrupt_run_header":
        assert t.header is None

    dt = trace_doc(log, idx, "d1")
    assert [r.run_id for r in dt.runs] == ["r1"]
    assert _kinds(dt.runs[0].summary.damage) == want["doc"]
    assert dt.runs[0].summary.events == want["events"]

    vr = read_versions(log, "r1", "d1", index=idx)
    assert _kinds(d for d in vr.damage if d.doc_id == "d1") == want["doc"]
    assert len(vr.versions) == want.get("versions", 6)
    b = next(v for v in vr.versions if v.sha256 == review_store.shas["extraction"][1])
    assert (b.seq, b.used_as) == want.get("first_b", (3, "output"))
    if "state" in want:
        state, order = want["state"]
        assert vr.versions[order].state == state

    for text in (to_json(t), to_json(dt), json.dumps([str(v) for v in vr.versions] + [str(d) for d in vr.damage])):
        assert SENTINEL not in text

    # the CLI shows the same, exits 0 for traces, and verify fails exactly when it should
    for args in (("trace", "run", "r1"), ("trace", "doc", "d1"), ("versions", "r1", "d1")):
        res = run_cli(*args, root=review_store.root)
        assert res.exit_code == 0, (args, res.stdout, res.stderr)
        assert SENTINEL not in res.stdout + res.stderr
        for k in want["run"] | want["doc"]:
            if args[0] == "versions" and k in want["run"] and k not in ("corrupt_line", "invalid_event", "torn_tail"):
                continue
            assert k in res.stdout, (args, k)
    res = run_cli("verify", "r1", root=review_store.root)
    assert res.exit_code == (0 if want["verify_ok"] else 1), res.stdout
    assert SENTINEL not in res.stdout + res.stderr


def test_a_line_nested_too_deep_stops_the_read_without_raising(review_store: SimpleNamespace, tmp_path: Path) -> None:
    """A valid event whose decision nests 600 deep: the reader cannot follow it (RecursionError
    in the log's upgrade path today). Trace keeps what it read, says where it stopped, never raises."""
    import sys

    from mailroom_record.hashing import canonical_json
    from mailroom_record.models import event_hash

    head = list(review_store.log.iter_events("r1", "d3"))[-1]
    deep: dict = {}
    cur = deep
    for _ in range(600):
        cur["a"] = {}
        cur = cur["a"]
    raw = head.model_dump(mode="json")
    raw.pop("hash")
    raw.update(seq=head.seq + 1, prev_hash=head.hash, decision=deep, cause=SENTINEL)
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 20_000))
    try:
        raw["hash"] = event_hash(raw)
        line = canonical_json(raw)
    finally:
        sys.setrecursionlimit(limit)
    with review_store.log.segment_paths("r1")[-1].open("ab") as f:
        f.write(line + b"\n")

    t = trace_run(review_store.log, Index(tmp_path / "i.sqlite"), "r1")
    assert [(d.doc_id, d.events) for d in t.documents] == [("d1", 7), ("d2", 3), ("d3", 1)]
    lost = [d for d in t.damage if d.kind in ("unreadable", "invalid_event", "corrupt_line")]
    assert lost and t.damaged
    dt = trace_doc(review_store.log, Index(tmp_path / "j.sqlite"), "d3")
    assert dt.runs[0].summary.events == 1 and dt.damaged
    for args in (("trace", "run", "r1"), ("trace", "doc", "d3"), ("versions", "r1", "d3")):
        res = run_cli(*args, root=review_store.root)
        assert res.exit_code == 0 and SENTINEL not in res.stdout + res.stderr, (args, res.stderr)
    assert run_cli("verify", "r1", root=review_store.root).exit_code == 1


def test_skipped_line_damage_holds_nothing_stored(review_store: SimpleNamespace, tmp_path: Path) -> None:
    """A valid event plus a stored key: the validator names the key. Trace and versions must
    report the skipped line by kind, segment and offset only (whatever log.py's detail says)."""
    from mailroom_record.models import GENESIS
    from record_testkit import hand_line

    bad = hand_line("r1", "d2", 0, GENESIS) | {f"{SENTINEL}-key": 1}
    _, offset = insert_line_after(review_store, "d1", 6, json.dumps(bad).encode())
    reads = [
        trace_run(review_store.log, None, "r1"),
        trace_run(review_store.log, Index(tmp_path / "a.sqlite"), "r1"),
        trace_doc(review_store.log, None, "d2"),
        trace_doc(review_store.log, Index(tmp_path / "b.sqlite"), "d2"),  # skipped lines from the index
    ]
    for t in reads:
        assert SENTINEL not in to_json(t)
    assert [(d.kind, d.byte_offset) for d in reads[0].damage] == [("invalid_event", offset)]
    assert [(d.kind, d.byte_offset) for d in reads[3].runs[0].damage] == [("invalid_event", offset)]
    for args in (("trace", "run", "r1"), ("trace", "doc", "d2"), ("versions", "r1", "d2")):
        for extra in ((), ("--json",)):
            res = run_cli(*args, *extra, root=review_store.root)
            assert res.exit_code == 0 and SENTINEL not in res.stdout + res.stderr, (args, extra)
            assert "invalid_event" in res.stdout


def test_error_details_never_carry_stored_key_names(review_store: SimpleNamespace) -> None:
    """A run header with a stored extra key, and seal_run refusing a line with one."""
    from mailroom_record.models import GENESIS
    from record_testkit import hand_line

    header = review_store.log.run_dir("r1") / "run.json"
    good = header.read_bytes()
    header.write_bytes(json.dumps(json.loads(good) | {f"{SENTINEL}-key": 1}).encode())
    t = trace_run(review_store.log, None, "r1")
    assert _kinds(t.damage) == {"corrupt_run_header"} and SENTINEL not in to_json(t)
    for args in (("trace", "run", "r1"), ("trace", "doc", "d1")):
        res = run_cli(*args, root=review_store.root)
        assert res.exit_code == 0 and SENTINEL not in res.stdout + res.stderr
    header.write_bytes(good)

    from mailroom_record.models import event_hash

    bad = hand_line("r1", "d9", 0, GENESIS) | {f"{SENTINEL}-key": 1}
    bad["hash"] = event_hash(bad)  # hashes to itself, so seal_run reaches validation (CorruptLine)
    insert_line_after(review_store, "d1", 6, json.dumps(bad).encode())
    res = run_cli("seal", "r1", root=review_store.root)
    assert res.stderr.startswith("error: CorruptLine: w0.jsonl at byte "), res.stderr
    assert res.exit_code == 1 and res.stderr.startswith("error: ") and res.stderr.count("\n") == 1
    assert SENTINEL not in res.stdout + res.stderr, res.stderr
