"""Streaming log reads: bounded memory, the ordering contract, and reads racing a writer.

Carried into Task 8: every reader streams segment by segment (``log.py``, "Ordering
contract"). These tests prove the memory bound on a run of many MB, that the two-pass
ordered read returns exactly what the old in-memory sort did, and that a reader never
returns or chokes on half a line a writer is still writing.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from record_testkit import SENTINEL, chain, make_event, peak_bytes, v1_event_dict, write_lines

from mailroom_record import log as log_module
from mailroom_record.blobs import BlobStore
from mailroom_record.log import CorruptLine, EventLog, ForkedChain, RawLine, TornTail

PAD = 512 * 1024  # bytes of synthetic payload per event in the large run


def _seg(store: SimpleNamespace, name: str = "w0.jsonl") -> Path:
    return store.root / "runs" / "r1" / "segments" / name


def _large_run(store: SimpleNamespace) -> int:
    """~24 MB over three writers and six documents, with handoffs. Returns the bytes stored."""
    pad = "f" * PAD  # hex-like filler, not text
    plan = [("w0", ["d0", "d1", "d2"], 6), ("w1", ["d0", "d3", "d4"], 5), ("w2", ["d1", "d5"], 9)]
    for writer, docs, n in plan:
        with store.log.open_writer("r1", writer, flush_every=2) as w:
            for i in range(n):
                for doc in docs:
                    w.append(doc_id=doc, node=f"n{i}", kind="handoff", payload={"pad": pad})
    return sum(p.stat().st_size for p in store.log.segment_paths("r1"))


def _old_order(log: EventLog, run_id: str, doc_id: str | None = None) -> list[tuple[str, int, str]]:
    """The pre-streaming contract: every stored line, stably sorted by ``(doc_id, seq)``."""
    rows = [s.raw for s in log.iter_stored(run_id) if doc_id is None or s.raw["doc_id"] == doc_id]
    rows.sort(key=lambda r: (r["doc_id"], r["seq"]))
    return [(r["doc_id"], r["seq"], r["hash"]) for r in rows]


# --- memory -----------------------------------------------------------------------


def test_every_reader_streams_a_large_run_in_bounded_memory(store: SimpleNamespace) -> None:
    total = _large_run(store)
    assert total > 20 * 1024 * 1024
    bound = 6 * 1024 * 1024  # a few 512 KB lines; the run is ~26 MB (the old readers peaked at 28-54 MB)

    def drain(it: Any) -> None:
        for _ in it:
            pass

    peaks = {
        "iter_events": peak_bytes(lambda: drain(store.log.iter_events("r1"))),
        "iter_events(doc)": peak_bytes(lambda: drain(store.log.iter_events("r1", "d1"))),
        "iter_stored": peak_bytes(lambda: drain(store.log.iter_stored("r1"))),
        "iter_raw": peak_bytes(lambda: drain(store.log.iter_raw("r1"))),
        "doc_heads": peak_bytes(lambda: store.log.doc_heads("r1")),
        "torn_tails": peak_bytes(lambda: store.log.torn_tails("r1")),
        "writer claim": peak_bytes(lambda: store.log.open_writer("r1", "w9").append(doc_id="d1", node="x", kind="handoff")),
    }
    assert {k: v for k, v in peaks.items() if v > bound} == {}, (total, peaks)
    # And the streamed reads are still right.
    assert [(e.doc_id, e.seq, e.hash) for e in store.log.iter_events("r1")] == _old_order(store.log, "r1")


def test_ordered_read_costs_words_per_event_not_payloads(tmp_path: Path) -> None:
    # 8,000 small events: pass one keeps four machine words per event.
    blobs = BlobStore(tmp_path)
    log = EventLog(tmp_path, blobs)
    lines = []
    for d in range(16):
        lines.extend(v1_event_dict(doc_id=f"doc{d:02d}", seq=i, inputs=[]) for i in range(500))
    write_lines(tmp_path, "w0.jsonl", lines[0::2])
    write_lines(tmp_path, "w1.jsonl", lines[1::2])
    scan_peak = peak_bytes(lambda: log.scan("r1"))
    assert scan_peak < 8_000 * 120, scan_peak  # the Event objects alone would be ~100x this
    n = [0]

    def drain() -> None:
        for _ in log.iter_events("r1"):
            n[0] += 1

    assert peak_bytes(drain) < 8_000 * 120 + 512 * 1024
    assert n[0] == 8_000


# --- ordering contract ------------------------------------------------------------


_layout = st.lists(
    st.tuples(st.integers(0, 3), st.integers(0, 2), st.integers(0, 4)),  # (doc, segment, seq)
    min_size=0,
    max_size=40,
)


@given(layout=_layout)
def test_ordered_read_equals_the_in_memory_sort(tmp_path_factory: pytest.TempPathFactory, layout: list[tuple[int, int, int]]) -> None:
    # Any placement of events over segments, forks (a repeated (doc, seq)) included.
    root = tmp_path_factory.mktemp("order")
    log = EventLog(root, BlobStore(root))
    for i, (doc, seg, seq) in enumerate(layout):
        e = make_event(doc_id=f"d{doc}", seq=seq, node=f"n{i}", inputs=[])
        write_lines(root, f"s{seg}.jsonl", [e])
    expected = _old_order(log, "r1")
    assert [(e.doc_id, e.seq, e.hash) for e in log.iter_events("r1")] == expected
    for doc in {f"d{d}" for d, _, _ in layout} | {"absent"}:
        assert [(e.doc_id, e.seq, e.hash) for e in log.iter_events("r1", doc)] == _old_order(log, "r1", doc)
    located = list(log.iter_located("r1"))
    for le in located:  # every location points at the line it came from
        data = (root / "runs/r1/segments" / le.segment).read_bytes()[le.byte_offset : le.byte_offset + le.length]
        assert json.loads(data)["hash"] == le.event.hash and data.endswith(b"\n")


def test_spans_read_only_the_named_ranges(store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    write_lines(store.root, "a.jsonl", chain("x", 3) + chain("y", 2))
    start = _seg(store, "a.jsonl").stat().st_size
    write_lines(store.root, "a.jsonl", chain("x", 2, start=3, prev=chain("x", 3)[-1].hash))
    write_lines(store.root, "b.jsonl", chain("z", 4))
    read: list[tuple[str, int, int | None]] = []
    original = log_module._stream_lines

    def counting(path: Path, start: int = 0, end: int | None = None):  # type: ignore[no-untyped-def]
        read.append((path.name, start, end))
        yield from original(path, start, end)

    monkeypatch.setattr(log_module, "_stream_lines", counting)
    got = [(le.event.doc_id, le.event.seq) for le in store.log.scan("r1", "x", spans={"a.jsonl": [(start, None)]}).events()]
    assert got == [("x", 3), ("x", 4)]
    assert read == [("a.jsonl", start, None)]  # b.jsonl and the head of a.jsonl were not read


# --- reads racing a writer --------------------------------------------------------


def test_a_reader_never_glues_the_rest_of_a_line_onto_a_torn_start(store: SimpleNamespace) -> None:
    # A writer is mid-line when the reader opens the segment, and finishes it (and writes
    # more) while the reader is still iterating. The reader must stop at the partial line,
    # never return its remainder as a line of its own.
    whole = json.dumps({"a": 1}).encode() + b"\n"
    seg = _seg(store)
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_bytes(whole + b'{"b": ')
    it = log_module.iter_raw_segment(seg)
    first = next(it)
    with seg.open("ab") as f:
        f.write(b'2}\n{"c": 3}\n')
    rest = list(it)
    assert first == RawLine("w0.jsonl", 0, whole[:-1])
    assert rest == [TornTail("w0.jsonl", len(whole))]


def test_readers_racing_a_flushing_writer_see_whole_prefixes(store: SimpleNamespace) -> None:
    stop = threading.Event()
    errors: list[BaseException] = []

    def write() -> None:
        try:
            with store.log.open_writer("r1", "w0", flush_every=1) as w:
                i = 0
                while not stop.is_set() and i < 800:
                    w.append(doc_id=f"d{i % 3}", node="n", kind="handoff", payload={"pad": "0" * (i % 700)})
                    i += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=write)
    t.start()
    reads = 0
    try:
        while t.is_alive() and reads < 60:
            by_doc: dict[str, list[int]] = {}
            for e in store.log.iter_events("r1"):
                by_doc.setdefault(e.doc_id, []).append(e.seq)
            assert all(seqs == list(range(len(seqs))) for seqs in by_doc.values()), by_doc
            store.log.doc_head_seqs("r1")
            reads += 1
    finally:
        stop.set()
        t.join()
    assert errors == [] and reads > 0


# --- corrupt and invalid lines -----------------------------------------------------


def test_strict_read_raises_on_a_corrupt_line_before_yielding_anything(store: SimpleNamespace) -> None:
    write_lines(store.root, "a.jsonl", chain("d1", 3))
    write_lines(store.root, "z.jsonl", chain("d2", 2), extra=b"garbage\n")  # in the last segment
    it = store.log.iter_events("r1")
    with pytest.raises(CorruptLine, match=r"z\.jsonl at byte"):
        next(it)


def test_tolerant_scan_records_bad_lines_without_their_content(store: SimpleNamespace) -> None:
    good = chain("d1", 2)
    bad_json = f'{{"doc_id": "{SENTINEL}", '.encode() + b"oops\n"
    invalid = v1_event_dict(doc_id="d2", inputs=[]) | {"cause": 7, "node": SENTINEL, "kind": SENTINEL}
    write_lines(store.root, "a.jsonl", good[:1], extra=bad_json)
    write_lines(store.root, "a.jsonl", [invalid] + good[1:], extra=b"[1, 2]\n" + b'{"torn": ')
    seg_bytes = _seg(store, "a.jsonl").read_bytes()
    assert SENTINEL.encode() in seg_bytes  # positive control: the content is on disk
    scan = store.log.scan("r1", strict=False)
    got = [(le.event.doc_id, le.event.seq) for le in scan.events()]
    assert got == [("d1", 0), ("d1", 1)]
    invalid_at = seg_bytes.rfind(b"\n", 0, seg_bytes.index(b'"kind":"' + SENTINEL.encode())) + 1
    assert [(b.kind, b.byte_offset) for b in scan.bad] == [
        ("corrupt_line", seg_bytes.index(bad_json)),
        ("corrupt_line", seg_bytes.index(b"[1, 2]")),
        ("invalid_event", invalid_at),
    ]
    assert scan.torn == [TornTail("a.jsonl", seg_bytes.index(b'{"torn": '))]
    assert all(SENTINEL not in b.detail for b in scan.bad), scan.bad
    assert "kind: literal_error" in scan.bad[2].detail  # names the field and the error type
    with pytest.raises(CorruptLine):
        list(store.log.iter_events("r1"))  # the strict reader still refuses


def test_a_writer_meeting_a_corrupt_line_keeps_refusing_and_never_sees_a_fork(store: SimpleNamespace) -> None:
    # Head refresh streams line by line; a corrupt line in the middle must not make the lines
    # before it count twice on the next refresh (which would look like a forked chain).
    write_lines(store.root, "a.jsonl", chain("d1", 2), extra=b"not json\n")
    write_lines(store.root, "a.jsonl", chain("d2", 1))
    w = store.log.open_writer("r1", "w0")
    for _ in range(3):
        with pytest.raises(CorruptLine):
            w.append(doc_id="d1", node="n", kind="decision")
    w.close()
    with pytest.raises(CorruptLine):
        store.log.doc_heads("r1")


def test_head_readers_still_detect_a_real_fork(store: SimpleNamespace) -> None:
    write_lines(store.root, "a.jsonl", chain("d1", 2))
    write_lines(store.root, "b.jsonl", chain("d1", 1, start=1, prev=chain("d1", 1)[0].hash, model="other"))
    with pytest.raises(ForkedChain):
        store.log.doc_heads("r1")
    assert [e.seq for e in store.log.iter_events("r1", "d1")] == [0, 1, 1]


# --- gaps on hostile seq numbers ---------------------------------------------------------


def test_scan_gaps_walk_stored_seqs_not_range_to_the_largest(store: SimpleNamespace) -> None:
    """One line with a seq near 10**12 must name the first gap quickly, not allocate up to it."""
    events = chain("d1", 3)
    huge = make_event(run_id="r1", doc_id="d1", seq=10**12, prev_hash=events[-1].hash, node="n")
    write_lines(store.root, "w0.jsonl", [*events, huge])
    scan = store.log.scan("r1", strict=False)
    assert scan.gaps() == {"d1": 3}


# --- fix pass 2: pass two's recheck, gaps over forks, and the one-document bound ---------


def test_a_line_swapped_between_the_two_passes_is_caught_not_yielded(store: SimpleNamespace) -> None:
    # Two lines of equal length. After pass one has located them, they swap places (a
    # rewrite in place, as a restore or a hostile edit could do). Pass two must notice that
    # the line at d1's offset is no longer d1's, instead of yielding d2's event as d1's.
    a, b = make_event(run_id="r1", doc_id="d1", seq=0, node="n"), make_event(run_id="r1", doc_id="d2", seq=0, node="n")
    seg = write_lines(store.root, "w0.jsonl", [a, b])
    la, lb = seg.read_bytes().splitlines(keepends=True)
    assert len(la) == len(lb)
    for strict in (True, False):
        seg.write_bytes(la + lb)
        scan = store.log.scan("r1", "d1", strict=strict)
        seg.write_bytes(lb + la)  # same size, same offsets, other owners
        if strict:
            with pytest.raises(CorruptLine, match="w0.jsonl at byte 0: line changed while reading"):
                list(scan.events())
        else:
            assert list(scan.events()) == []
            assert [(x.byte_offset, x.kind) for x in scan.bad] == [(0, "corrupt_line")]


def test_gaps_count_a_forked_seq_once(store: SimpleNamespace) -> None:
    c = chain("d1", 2)
    fork = make_event(run_id="r1", doc_id="d1", seq=0, prev_hash=c[0].prev_hash, node="other")
    late = make_event(run_id="r1", doc_id="d1", seq=3, prev_hash=c[1].hash, node="n")
    write_lines(store.root, "w0.jsonl", [c[0], fork, c[1], late])
    assert store.log.scan("r1", strict=False).gaps() == {"d1": 2}
    assert store.log.scan("r1", "d1", strict=False).seqs("d1") == [0, 0, 1, 3]


@pytest.mark.parametrize("handovers", [0, 3])
def test_one_large_document_costs_a_few_words_per_event(store: SimpleNamespace, handovers: int) -> None:
    # Task 8 review MINOR 5: pass two used to copy every row into a sorted list of tuples
    # (~200 bytes per event) and gaps() a list plus a set. One document of 60k events, in
    # one segment or handed between four, must cost the packed rows only (32 bytes each).
    from mailroom_record.hashing import canonical_json

    n = 60_000
    base = v1_event_dict(doc_id="d1", seq=0)
    per_segment = n // (handovers + 1)
    segs = [_seg(store, f"w{k}.jsonl") for k in range(handovers + 1)]
    segs[0].parent.mkdir(parents=True, exist_ok=True)
    files = [p.open("wb") for p in segs]
    try:
        for i in range(n):
            base["seq"] = i  # hashes not linked: only the reading cost matters here
            files[min(i // per_segment, handovers)].write(canonical_json(base) + b"\n")
    finally:
        for f in files:
            f.close()

    def first_event() -> None:
        it = store.log.iter_events("r1")
        next(it)  # pass one done, pass two started
        it.close()

    per_event = peak_bytes(first_event) / n
    assert per_event < 48, per_event
    scan = store.log.scan("r1")
    assert peak_bytes(scan.gaps) < 64 * 1024
    assert scan.gaps() == {}


# --- open handles and unreadable segments (PR #2 re-review, items 9 and 10) ----------

_LOW_FD_CHILD = """
import resource, sys
from pathlib import Path
from mailroom_record.blobs import BlobStore
from mailroom_record.log import EventLog

root = Path(sys.argv[1])
resource.setrlimit(resource.RLIMIT_NOFILE, (int(sys.argv[2]), resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
log = EventLog(root, BlobStore(root))
print(sum(1 for _ in log.iter_events("r1")))
"""


def test_pass_two_keeps_a_bounded_number_of_segments_open(store: SimpleNamespace) -> None:
    # 300 segments, one document each: pass two reads them in doc order. Holding every
    # segment open would need 300 descriptors; a child with a limit of 128 must still read
    # every event (EMFILE otherwise).
    import subprocess
    import sys

    n = 300
    for i in range(n):
        write_lines(store.root, f"w{i:03d}.jsonl", chain(f"d{i:03d}", 1))
    res = subprocess.run(
        [sys.executable, "-c", _LOW_FD_CHILD, str(store.root), "128"], capture_output=True, text=True, timeout=120
    )
    assert res.returncode == 0, res.stderr[-2000:]
    assert res.stdout.strip() == str(n)


def test_pass_two_reopens_an_evicted_segment(store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    # One document handed back and forth between more segments than the cache holds: each
    # read must land on the right segment after its handle was evicted and reopened.
    monkeypatch.setattr(log_module, "_MAX_OPEN_SEGMENTS", 2)
    events = chain("d1", 12)
    for i, e in enumerate(events):
        write_lines(store.root, f"w{i % 4}.jsonl", [e])
    got = [(x.segment, x.event.seq, x.event.hash) for x in store.log.scan("r1").events()]
    assert got == [(f"w{i % 4}.jsonl", i, e.hash) for i, e in enumerate(events)]


def test_tolerant_scan_keeps_other_segments_when_one_cannot_be_opened(
    store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_lines(store.root, "a.jsonl", chain("d1", 2))
    write_lines(store.root, "b.jsonl", chain("d2", 3))
    write_lines(store.root, "c.jsonl", chain("d3", 1))
    real_open = Path.open

    def failing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == "b.jsonl":
            raise PermissionError(13, "injected")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(PermissionError):  # strict readers still fail loudly
        store.log.scan("r1")
    scan = store.log.scan("r1", strict=False)
    assert scan.unreadable == [("b.jsonl", "PermissionError")]
    assert scan.doc_ids() == ["d1", "d3"]
    got: list[tuple[str, int]] = []
    with pytest.raises(PermissionError):  # after every readable event: the read was partial
        for located in scan.events():
            got.append((located.event.doc_id, located.event.seq))
    assert got == [("d1", 0), ("d1", 1), ("d3", 0)]
