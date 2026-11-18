"""Stateful property test of the blob store, event log and seals together (hypothesis).

A model tracks what must be on disk; random sequences of operations run against a real
store, and the record's invariants are checked after every step:

- every whole line hashes to itself (``event_hash`` of the stored dict);
- per document, ``seq`` is unique and gap-free on disk and every ``prev_hash`` link holds;
- the events on disk are exactly the ones the model says were made durable (flushed,
  closed, or written whole before a simulated crash), and ``doc_heads`` is each
  document's last one;
- torn tails are reported exactly where the simulated crashes left them;
- across all seals, every on-disk event at or below the latest seal's heads is exported to
  exactly one Parquet file, with its stored hash; at each seal, ``event_count`` equals the
  sum of ``head seq + 1``; seal links and hashes hold on the stored seal files;
- every blob an event names is present and decodes to its hash.

Operations: open a writer, append (with or without a blob, or with a tombstoned blob),
flush, close, abandon a writer (it dies with its buffer unwritten), crash a writer part
way through a write (a prefix of its buffer reaches the disk, which can leave a torn
tail), put and tombstone a blob, seal.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from hypothesis import event, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
from record_testkit import run_fields, segment_lines

from mailroom_record.blobs import BlobStore, BlobTombstoned
from mailroom_record.log import BlobNotStored, DocumentInUse, EventLog, LogWriter, TornTail, WriterInUse
from mailroom_record.models import GENESIS, BlobRef, Run, event_hash, seal_hash
from mailroom_record.seal import seal_run

WRITERS = st.sampled_from(["w0", "w1", "w2"])
DOCS = st.sampled_from(["d0", "d1", "d2", "d3"])


class RecordMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.root = Path(tempfile.mkdtemp(prefix="record-machine-"))
        self.blobs = BlobStore(self.root)
        self.log = EventLog(self.root, self.blobs)
        self.log.create_run(Run(**run_fields()))
        self.writers: dict[str, LogWriter] = {}
        self.flush_every: dict[str, int] = {}
        self.pending: dict[str, list[tuple[tuple[str, int], str]]] = {}  # buffered (doc, seq), hash
        self.durable: dict[tuple[str, int], str] = {}  # (doc, seq) -> hash on disk
        self.owner: dict[str, str] = {}  # doc -> open writer holding it
        self.torn: list[TornTail] = []
        self.refs: list[BlobRef] = []
        self.tombstoned: set[str] = set()
        self.seal_heads: list[dict[str, int]] = []  # per seal: doc -> head seq

    def teardown(self) -> None:
        for w in self.writers.values():
            w._buffer.clear()
            w.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # --- model helpers --------------------------------------------------------------

    def _next(self, wid: str, doc: str) -> tuple[int, str]:
        """The seq and prev_hash the next event of ``doc`` must get from writer ``wid``."""
        best = (-1, GENESIS)
        for (d, seq), h in self.durable.items():
            if d == doc and seq > best[0]:
                best = (seq, h)
        for (d, seq), h in self.pending.get(wid, []):
            if d == doc and seq > best[0]:
                best = (seq, h)
        return best[0] + 1, best[1]

    def _make_durable(self, wid: str, count: int | None = None) -> None:
        items = self.pending[wid] if count is None else self.pending[wid][:count]
        for key, h in items:
            assert key not in self.durable, f"model: {key} made durable twice"
            self.durable[key] = h
        self.pending[wid] = self.pending[wid][len(items):]

    def _release(self, wid: str) -> None:
        self.owner = {d: w for d, w in self.owner.items() if w != wid}
        del self.writers[wid], self.pending[wid]

    # --- rules ----------------------------------------------------------------------

    @initialize(flush_every=st.sampled_from([2, 3, 50]))
    def first_writer(self, flush_every: int) -> None:
        self.open_writer("w0", flush_every)

    @rule(wid=WRITERS, flush_every=st.sampled_from([1, 2, 3, 50]))
    def open_writer(self, wid: str, flush_every: int) -> None:
        if wid in self.writers:
            with pytest.raises(WriterInUse):
                self.log.open_writer("r1", wid)
            return
        w = self.log.open_writer("r1", wid, flush_every=flush_every)
        self.writers[wid], self.flush_every[wid], self.pending[wid] = w, flush_every, []
        if any(t.segment.startswith(f"{wid}.") for t in self.torn):
            assert w.segment.name not in {t.segment for t in self.torn}, "a writer appended after torn bytes"

    @rule(content=st.binary(max_size=200))
    def put_blob(self, content: bytes) -> None:
        sha = hashlib.sha256(content).hexdigest()
        if sha in self.tombstoned:
            with pytest.raises(BlobTombstoned):
                self.blobs.put(content, "transcript")
            return
        self.refs.append(self.blobs.put(content, "transcript"))

    @precondition(lambda self: self.refs)
    @rule(data=st.data())
    def tombstone_blob(self, data: st.DataObject) -> None:
        ref = data.draw(st.sampled_from(self.refs))
        self.blobs.tombstone(ref.sha256, "synthetic erasure")
        self.tombstoned.add(ref.sha256)

    @precondition(lambda self: self.writers)
    @rule(data=st.data(), doc=DOCS, with_blob=st.booleans())
    def append(self, data: st.DataObject, doc: str, with_blob: bool) -> None:
        wid = data.draw(st.sampled_from(sorted(self.writers)))
        w = self.writers[wid]
        refs = [data.draw(st.sampled_from(self.refs))] if with_blob and self.refs else []
        holder = self.owner.get(doc)
        if holder is not None and holder != wid:
            with pytest.raises(DocumentInUse):
                w.append(doc_id=doc, node="n", kind="output", inputs=refs)
            return
        if any(r.sha256 in self.tombstoned for r in refs):
            with pytest.raises(BlobNotStored):
                w.append(doc_id=doc, node="n", kind="output", inputs=refs)
            self.owner[doc] = wid  # the writer took the document before the blob check
            return
        seq, prev = self._next(wid, doc)
        ev = w.append(doc_id=doc, node="n", kind="output", inputs=refs, payload={"seq": seq})
        assert (ev.seq, ev.prev_hash) == (seq, prev), "writer and model disagree on the chain"
        self.owner[doc] = wid
        self.pending[wid].append(((doc, seq), ev.hash))
        if len(self.pending[wid]) >= self.flush_every[wid]:
            self._make_durable(wid)

    @precondition(lambda self: self.writers)
    @rule(data=st.data())
    def abandon(self, data: st.DataObject) -> None:
        """A writer dies with nothing written: whatever it buffered is lost."""
        wid = data.draw(st.sampled_from(sorted(self.writers)))
        w = self.writers[wid]
        w._buffer.clear()
        w.close()
        self._release(wid)

    @precondition(lambda self: self.writers)
    @rule(data=st.data())
    def flush(self, data: st.DataObject) -> None:
        wid = data.draw(st.sampled_from(sorted(self.writers)))
        self.writers[wid].flush()
        self._make_durable(wid)

    @precondition(lambda self: self.writers)
    @rule(data=st.data())
    def close(self, data: st.DataObject) -> None:
        wid = data.draw(st.sampled_from(sorted(self.writers)))
        self.writers[wid].close()
        self._make_durable(wid)
        self._release(wid)

    @precondition(lambda self: any(self.pending.values()))
    @rule(data=st.data())
    def crash(self, data: st.DataObject) -> None:
        """Kill a writer: its buffer is lost, except a prefix of it that reached the disk."""
        wid = data.draw(st.sampled_from(sorted(w for w, p in self.pending.items() if p)))
        w = self.writers[wid]
        buffered = b"".join(w._buffer)
        # Cut after some whole lines, then optionally part way into the next one.
        whole = data.draw(st.integers(0, len(w._buffer)), label="whole lines written")
        cut = sum(len(line) for line in w._buffer[:whole])
        if whole < len(w._buffer):
            cut += data.draw(st.integers(0, len(w._buffer[whole]) - 1), label="bytes of the next line")
        w._buffer.clear()
        w.close()  # flushes nothing: the buffer is gone
        if cut:
            size = w.segment.stat().st_size
            with w.segment.open("ab") as f:
                f.write(buffered[:cut])
            prefix = buffered[:cut]
            self._make_durable(wid, prefix.count(b"\n"))
            event(f"crash wrote {prefix.count(b'\n')} whole lines")
            if not prefix.endswith(b"\n"):
                event("crash left a torn tail")
                self.torn.append(TornTail(w.segment.name, size + prefix.rfind(b"\n") + 1))
        self.pending[wid] = []
        self._release(wid)

    @rule()
    def seal(self) -> None:
        before = len(self.seal_heads)
        s = seal_run(self.log, "r1")
        heads: dict[str, int] = {}
        for (doc, seq) in self.durable:
            heads[doc] = max(seq, heads.get(doc, -1))
        if before and heads == self.seal_heads[-1]:
            assert s.n == before - 1, "a seal without new events must be the previous one"
            return
        assert s.n == before
        event(f"seal {s.n} written")
        assert s.doc_heads == {d: self.durable[(d, q)] for d, q in heads.items()}
        assert s.event_count == len(self.durable) == sum(q + 1 for q in heads.values())
        self.seal_heads.append(heads)

    # --- invariants -----------------------------------------------------------------

    @invariant()
    def log_matches_the_model(self) -> None:
        by_doc: dict[str, list[dict[str, Any]]] = {}
        for line in segment_lines(self.root):
            raw = json.loads(line)
            assert event_hash(raw) == raw["hash"], "a whole line does not hash to itself"
            by_doc.setdefault(raw["doc_id"], []).append(raw)
            for ref in raw["inputs"]:
                if ref["sha256"] not in self.tombstoned:
                    assert self.blobs.exists(ref["sha256"]) == "present"
        on_disk: dict[tuple[str, int], str] = {}
        for doc, events in by_doc.items():
            events.sort(key=lambda r: r["seq"])
            assert [r["seq"] for r in events] == list(range(len(events))), f"seq gap or repeat in {doc}"
            prev = GENESIS
            for r in events:
                assert r["prev_hash"] == prev, f"broken link in {doc} at {r['seq']}"
                prev = r["hash"]
                on_disk[(doc, r["seq"])] = r["hash"]
        assert on_disk == self.durable
        assert self.log.doc_heads("r1") == {d: e[-1]["hash"] for d, e in sorted(by_doc.items())}
        assert self.log.torn_tails("r1") == sorted(self.torn, key=lambda t: t.segment)

    @invariant()
    def seals_cover_every_event_once(self) -> None:
        seal_dir = self.root / "runs/r1/seals"
        n_seals = len(self.seal_heads)
        if not n_seals:
            assert not seal_dir.exists() or not list(seal_dir.glob("*.json"))
            return
        prev = GENESIS
        exported: list[tuple[str, int, str]] = []
        for n in range(n_seals):
            raw = json.loads((seal_dir / f"{n}.json").read_bytes())
            assert raw["n"] == n and raw["prev_seal_hash"] == prev and seal_hash(raw) == raw["hash"]
            prev = raw["hash"]
            heads = self.seal_heads[n]
            assert raw["event_count"] == sum(q + 1 for q in heads.values())
            assert raw["doc_heads"] == {d: self.durable[(d, q)] for d, q in heads.items()}
            table = pq.read_table(self.root / f"runs/r1/columns/events-{n}.parquet", columns=["doc_id", "seq", "hash"])
            exported += [(r["doc_id"], r["seq"], r["hash"]) for r in table.to_pylist()]
        assert not (seal_dir / f"{n_seals}.json").exists()
        assert len(exported) == len({(d, q) for d, q, _ in exported}), "an event exported twice"
        latest = self.seal_heads[-1]
        want = sorted((d, q, h) for (d, q), h in self.durable.items() if q <= latest.get(d, -1))
        assert sorted(exported) == want


RecordMachine.TestCase.settings = settings(
    settings.default, stateful_step_count=40, max_examples=settings.default.max_examples * 5 // 2
)
test_record_machine = RecordMachine.TestCase
