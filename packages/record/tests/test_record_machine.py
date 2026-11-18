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
tail), put and tombstone a blob, seal, crash then seal at once, seal while a document is
handed between two writers part way through the sealer's read, lose (delete or truncate)
the latest or an older seal's Parquet file (the next seal rebuilds it), and damage the
store in a way sealing must refuse (a deleted seal file, a line cut out of a chain, an
unsealed line edited with its old hash), check the refusal wrote nothing, and repair it.

Every rule is always enabled (no preconditions) and does nothing when it has nothing to
act on, so hypothesis never aborts an example for want of an enabled rule.
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
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule
from record_testkit import run_fields, segment_lines

from mailroom_record.blobs import BlobStore, BlobTombstoned
from mailroom_record.log import BlobNotStored, DocumentInUse, EventLog, LogWriter, TornTail, WriterInUse
from mailroom_record.models import GENESIS, BlobRef, Run, event_hash, seal_hash
from mailroom_record.seal import SealRefused, seal_run

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
        self.lost_columns: set[int] = set()  # seals whose Parquet file was deleted or cut

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

    @rule(content=st.binary(max_size=64))
    def put_blob(self, content: bytes) -> None:
        sha = hashlib.sha256(content).hexdigest()
        if sha in self.tombstoned:
            with pytest.raises(BlobTombstoned):
                self.blobs.put(content, "transcript")
            return
        self.refs.append(self.blobs.put(content, "transcript"))

    @rule(data=st.data())
    def tombstone_blob(self, data: st.DataObject) -> None:
        if not self.refs:
            return
        ref = data.draw(st.sampled_from(self.refs))
        self.blobs.tombstone(ref.sha256, "synthetic erasure")
        self.tombstoned.add(ref.sha256)

    @rule(data=st.data(), doc=DOCS, with_blob=st.booleans())
    def append(self, data: st.DataObject, doc: str, with_blob: bool) -> None:
        wid = data.draw(WRITERS)
        if wid not in self.writers:
            self.open_writer(wid, data.draw(st.sampled_from([1, 2, 3, 50])))
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

    @rule(data=st.data())
    def abandon(self, data: st.DataObject) -> None:
        """A writer dies with nothing written: whatever it buffered is lost."""
        if not self.writers:
            return
        wid = data.draw(st.sampled_from(sorted(self.writers)))
        w = self.writers[wid]
        w._buffer.clear()
        w.close()
        self._release(wid)

    @rule(data=st.data())
    def flush(self, data: st.DataObject) -> None:
        if not self.writers:
            return
        wid = data.draw(st.sampled_from(sorted(self.writers)))
        self.writers[wid].flush()
        self._make_durable(wid)

    @rule(data=st.data())
    def close(self, data: st.DataObject) -> None:
        if not self.writers:
            return
        wid = data.draw(st.sampled_from(sorted(self.writers)))
        self.writers[wid].close()
        self._make_durable(wid)
        self._release(wid)

    @rule(data=st.data())
    def crash(self, data: st.DataObject) -> None:
        """Kill a writer: its buffer is lost, except a prefix of it that reached the disk."""
        if not any(self.pending.values()):
            return
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
            lines_written = prefix.count(b"\n")
            self._make_durable(wid, lines_written)
            event(f"crash wrote {lines_written} whole lines")
            if not prefix.endswith(b"\n"):
                event("crash left a torn tail")
                self.torn.append(TornTail(w.segment.name, size + prefix.rfind(b"\n") + 1))
        self.pending[wid] = []
        self._release(wid)

    def _durable_heads(self) -> dict[str, int]:
        heads: dict[str, int] = {}
        for (doc, seq) in self.durable:
            heads[doc] = max(seq, heads.get(doc, -1))
        return heads

    def _record_seal(self, s: Any, must_cover: dict[str, int], exact: bool) -> None:
        """Check a seal ``seal_run`` returned and add it to the model.

        Every sealed head is a durable event and the seal covers at least ``must_cover``
        (everything durable before the call); with ``exact`` it covers exactly the durable
        heads now. A seal with nothing new returns the previous seal.
        """
        before = len(self.seal_heads)
        by_hash = {(d, h): q for (d, q), h in self.durable.items()}
        heads = {d: by_hash[(d, h)] for d, h in s.doc_heads.items()}  # KeyError: not durable
        assert all(heads.get(d, -1) >= q for d, q in must_cover.items()), "a durable event was not sealed"
        if exact:
            assert heads == self._durable_heads()
        self.lost_columns.clear()  # any call that returns rebuilt every lost column file
        if before and heads == self.seal_heads[-1]:
            assert s.n == before - 1, "a seal without new events must be the previous one"
            return
        assert s.n == before
        event(f"seal {min(s.n, 3)}{'+' if s.n >= 3 else ''} written")
        assert s.event_count == sum(q + 1 for q in heads.values())
        self.seal_heads.append(heads)

    @rule()
    def seal(self) -> None:
        if self.lost_columns:
            event("seal rebuilt a lost Parquet file")
        if self.torn:
            event("seal with a torn tail on disk")
        self._record_seal(seal_run(self.log, "r1"), self._durable_heads(), exact=True)

    @rule(data=st.data())
    def crash_then_seal(self, data: st.DataObject) -> None:
        """Seal straight after a crash, so recovery and sealing meet often."""
        if not any(self.pending.values()):
            self._buffer_some(data)
        if any(self.pending.values()):
            self.crash(data)
            event("seal right after a crash")
        self.seal()

    def _buffer_some(self, data: st.DataObject) -> None:
        """Give a writer one to three buffered events, so a crash has something to cut."""
        closed = [w for w in ("w0", "w1", "w2") if w not in self.writers]
        if closed:
            wid = data.draw(st.sampled_from(closed), label="writer to crash")
            self.open_writer(wid, 50)
        else:
            wid = max(self.writers, key=lambda w: self.flush_every[w])
            if self.flush_every[wid] < 4:
                return
        free = [d for d in ("d0", "d1", "d2", "d3") if self.owner.get(d, wid) == wid]
        if not free:
            return
        doc = data.draw(st.sampled_from(free), label="document to crash")
        w = self.writers[wid]
        for _ in range(data.draw(st.integers(1, 3), label="events to crash")):
            if len(self.pending[wid]) + 1 >= self.flush_every[wid]:
                break
            seq, prev = self._next(wid, doc)
            ev = w.append(doc_id=doc, node="n", kind="output", payload={"seq": seq})
            assert (ev.seq, ev.prev_hash) == (seq, prev)
            self.owner[doc] = wid
            self.pending[wid].append(((doc, seq), ev.hash))

    @rule(data=st.data(), doc=DOCS)
    def seal_during_handoff(self, data: st.DataObject, doc: str) -> None:
        """Hand ``doc`` from writer ``ha`` to writer ``hz`` while the sealer reads the log.

        The hand-off runs once the sealer has its first line (``ha.jsonl`` sorts first, so
        that is usually ``ha``'s segment), so the event written to ``ha`` can sit in a
        segment already read and the next one, in ``hz``, in a segment not read yet. The seal must
        cover everything durable before the call and may cover the hand-off or not.
        """
        if doc in self.owner:  # held by an open writer: no hand-off possible
            self.seal()
            return
        must = self._durable_heads()
        original = self.log.iter_stored
        done: list[bool] = []

        reads: list[int] = []

        def racing(run_id: str) -> Any:
            reads.append(1)
            for line in original(run_id):
                if not done:  # the first segment has been read (or is being streamed)
                    done.append(True)
                    self._handoff(doc)
                yield line
            if not done:
                done.append(True)
                self._handoff(doc)

        self.log.iter_stored = racing  # type: ignore[method-assign]
        try:
            s = seal_run(self.log, "r1")
        finally:
            del self.log.iter_stored
        event("seal with a hand-off mid-read")
        if len(reads) > 1:
            event("seal re-read the log after a hand-off showed a gap")
        self._record_seal(s, must, exact=False)

    def _handoff(self, doc: str) -> None:
        for wid in ("ha", "hz"):
            seq, prev = self._next(wid, doc)
            with self.log.open_writer("r1", wid, flush_every=1) as w:
                ev = w.append(doc_id=doc, node="handoff", kind="handoff")
            assert (ev.seq, ev.prev_hash) == (seq, prev)
            self.durable[(doc, seq)] = ev.hash

    @rule(data=st.data(), latest=st.booleans(), how=st.sampled_from(["delete", "truncate"]))
    def lose_columns(self, data: st.DataObject, latest: bool, how: str) -> None:
        """A seal's Parquet file is deleted or cut short; the next seal must rebuild it."""
        if not self.seal_heads:
            return
        last = len(self.seal_heads) - 1
        n = last if latest or last == 0 else data.draw(st.integers(0, last - 1), label="older seal")
        path = self.root / f"runs/r1/columns/events-{n}.parquet"
        if not path.exists():
            return
        if how == "delete":
            path.unlink()
        else:
            path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])
        self.lost_columns.add(n)
        event(f"lost Parquet of {'the latest' if n == last else 'an older'} seal")

    @rule(data=st.data(), what=st.sampled_from(["seal_file", "gap", "stale_hash"]))
    def damage_is_refused(self, data: st.DataObject, what: str) -> None:
        """Damage that sealing must refuse: check it refuses, wrote nothing, then repair."""
        seals_dir = self.root / "runs/r1/seals"
        if what == "seal_file":
            last = len(self.seal_heads) - 1
            if last < 0 or last in self.lost_columns:
                return  # with its Parquet file gone too, nothing shows the seal existed
            target = seals_dir / f"{last}.json"
            match = "a seal file was deleted"
            original = target.read_bytes()
            target.unlink()
        else:
            if what == "gap":
                keys = [(d, q) for (d, q) in self.durable if (d, q + 1) in self.durable]
                match = "no event with seq|no longer holds"
            else:
                sealed = self.seal_heads[-1] if self.seal_heads else {}
                keys = [(d, q) for (d, q) in self.durable if q > sealed.get(d, -1)]
                match = "does not hash"
            if not keys:
                return
            doc, seq = data.draw(st.sampled_from(sorted(keys)), label="line")
            target, original, line = self._find(doc, seq)
            if what == "gap":
                damaged = original.replace(line, b"", 1)
            else:
                raw = json.loads(line)
                raw["node"] = "EDITED"
                damaged = original.replace(line, json.dumps(raw, sort_keys=True, separators=(",", ":")).encode() + b"\n", 1)
            target.write_bytes(damaged)
        files_before = sorted(p.name for p in seals_dir.glob("*.json")) if seals_dir.exists() else []
        with pytest.raises(SealRefused, match=match):
            seal_run(self.log, "r1")
        files_after = sorted(p.name for p in seals_dir.glob("*.json")) if seals_dir.exists() else []
        assert files_after == files_before, "a refused seal wrote a seal file"
        target.write_bytes(original)
        event(f"refused: {what}")

    def _find(self, doc: str, seq: int) -> tuple[Path, bytes, bytes]:
        for seg in sorted((self.root / "runs/r1/segments").glob("*.jsonl")):
            data = seg.read_bytes()
            for line in data[: data.rfind(b"\n") + 1].splitlines(keepends=True):
                raw = json.loads(line)
                if (raw["doc_id"], raw["seq"]) == (doc, seq):
                    return seg, data, line
        raise AssertionError(f"model: {doc}/{seq} is not on disk")

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
        below: dict[str, int] = {}
        for n in range(n_seals):
            raw = json.loads((seal_dir / f"{n}.json").read_bytes())
            assert raw["n"] == n and raw["prev_seal_hash"] == prev and seal_hash(raw) == raw["hash"]
            prev = raw["hash"]
            heads = self.seal_heads[n]
            assert raw["event_count"] == sum(q + 1 for q in heads.values())
            assert raw["doc_heads"] == {d: self.durable[(d, q)] for d, q in heads.items()}
            # Seal n's Parquet file holds exactly the durable events it added, once each.
            want = sorted(
                (d, q, h) for (d, q), h in self.durable.items() if below.get(d, -1) < q <= heads.get(d, -1)
            )
            below = heads
            if n in self.lost_columns:
                continue  # deleted or cut; the next seal rebuilds it
            table = pq.read_table(self.root / f"runs/r1/columns/events-{n}.parquet", columns=["doc_id", "seq", "hash"])
            got = sorted((r["doc_id"], r["seq"], r["hash"]) for r in table.to_pylist())
            assert got == want, f"Parquet of seal {n} disagrees with the model"
        assert not (seal_dir / f"{n_seals}.json").exists()


RecordMachine.TestCase.settings = settings(
    settings.default, stateful_step_count=40, max_examples=settings.default.max_examples * 5 // 2
)
test_record_machine = RecordMachine.TestCase
