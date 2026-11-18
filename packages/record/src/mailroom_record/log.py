"""The append-only event log: one hash chain per document, one segment file per writer.

Layout under the store root (ADR-0004)::

    runs/<run_id>/run.json                     the Run header, written once
    runs/<run_id>/segments/<writer_id>.jsonl   one canonical JSON event per line
    runs/<run_id>/segments/<writer_id>.<k>.jsonl  a later segment of the same writer

Writing
-------
- ``create_run`` writes ``run.json`` exclusively (fsynced temp file hard-linked into place,
  directories fsynced). A second call raises ``RunExists``; concurrent calls have one winner.
- ``open_writer`` takes an exclusive ``flock`` on the writer's segment, so one writer id is
  live in one place at a time (another open raises ``WriterInUse``, in this process or
  another). Segments are opened in append mode and never rewritten. If the writer's latest
  segment ends in a torn line (a crash mid-write), the writer starts a new segment
  ``<writer_id>.<k+1>.jsonl`` instead of appending after the torn bytes, which stay where
  they are and stay reported by ``torn_tails``. Every open fsyncs the segment directory
  (not only on create), so a segment whose creator crashed before its directory fsync
  becomes durable on the next open.
- ``LogWriter.append`` validates the event, sets ``seq``, ``prev_hash`` and ``hash`` itself,
  and serializes the line at once: canonical JSON of the validated model's JSON-mode dump,
  plus ``\\n``. So every stored line satisfies ``event_hash(json.loads(line)) == hash``.
  Inputs are deep-copied before validation and the returned Event is parsed from the line,
  so nothing a caller holds (before or after the call) can change what is stored.
- Blob before event: every ref in ``inputs`` and ``outputs`` must be ``present`` in the
  blob store, else ``BlobNotStored`` (a tombstoned blob is not present).
- Lines are buffered and written with fsync every ``flush_every`` appends, on ``flush()``
  and on ``close()``. Readers (``iter_events``, ``doc_heads``, ``torn_tails``) read disk
  only, so buffered events are invisible to them until flushed. A crash loses at most the
  buffered events, plus possibly one torn line. The Event that ``append`` returns is not
  durable until the next flush (``flush()``, ``close()`` or an automatic one); anything
  derived from events, such as the index (Task 8), must be written only after the flush
  that made them durable (write order: blob, event, index).
- If a write or fsync fails the writer is marked failed and refuses further use: its
  in-memory heads may be ahead of disk, so a new writer must take over. A failed fsync
  does not mean the bytes are absent: they may sit in the page cache, visible to readers.
  A later writer may then chain onto those events, and if power is lost before the kernel
  writes them back, the later events survive while the earlier ones do not: a ``seq``
  gap that verification (Task 7) reports.

One owner per document
----------------------
A document's chain has one writer at a time. A writer takes a document on its first
append to it and holds it until ``close()``. At that moment it reads the document's head
from disk (all segments, incrementally), so a writer opened earlier still continues from
events another writer flushed and closed meanwhile, and a writer opened after a crash
never reuses a ``seq`` that is on disk. Inside one process the rule is enforced: appending
to a document held by another live writer raises ``DocumentInUse``. Across processes it is
not enforced (no per-document lock files); callers partition documents between processes.
If two processes do write one document, its chain forks (two stored events with one
``seq``). Head computation detects that: ``doc_heads`` and a writer taking the document
raise ``ForkedChain`` naming the document and ``seq``, so nothing extends or seals a forked
chain. The line readers still return every line, and verification (Task 7) reports it.

Reading
-------
A line is whole when it ends with ``\\n``. Bytes after the last newline of a segment are a
torn tail: skipped and reported. A whole line that is not a JSON object is corruption and
raises ``CorruptLine`` naming the segment and byte offset; it is never skipped. Events are
upgraded on read (``Event.from_stored``); ``iter_stored`` yields the raw stored dicts so a
verifier can hash the stored form (ADR-0005). ``iter_events`` orders by ``(doc_id, seq)``
across segments; ``_run`` sorts like any other doc id.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import threading
import weakref
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mailroom_record.blobs import BlobStore, _fsync_dir, _makedirs_durable, write_exclusive
from mailroom_record.hashing import canonical_json
from mailroom_record.models import GENESIS, BlobRef, Event, Run, utc_now

__all__ = [
    "BlobNotStored",
    "CorruptLine",
    "DocumentInUse",
    "EventLog",
    "ForkedChain",
    "LogWriter",
    "RunExists",
    "StoredLine",
    "TornTail",
    "WriterInUse",
]

# A writer id names a file: letters, digits, "_" and "-" only (no dots: "<id>.<k>.jsonl"
# names a writer's later segments).
_WRITER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
_SEGMENT_RE = re.compile(r"(?P<writer>[A-Za-z0-9][A-Za-z0-9_-]{0,99})(?:\.(?P<k>[1-9][0-9]*))?\.jsonl")


class RunExists(FileExistsError):
    """``run.json`` already exists for this run id."""


class BlobNotStored(LookupError):
    """An event names a blob that is not present in the blob store (blob before event)."""


class WriterInUse(RuntimeError):
    """Another live writer (in this process or another) holds this writer id's segment."""


class DocumentInUse(RuntimeError):
    """Another live writer in this process holds this document's chain."""


class CorruptLine(ValueError):
    """A whole segment line is not a JSON object."""


class ForkedChain(RuntimeError):
    """Two stored events share one ``(doc_id, seq)``: two owners wrote the same document.

    Raised by head computation (``doc_heads``, a writer taking the document), never by the
    line readers: ``iter_stored`` and ``iter_events`` still return every line, so
    verification (Task 7) can report the fork.
    """

    def __init__(self, run_id: str, doc_id: str, seq: int) -> None:
        super().__init__(f"document {doc_id!r} of run {run_id!r} is forked: seq {seq} is stored twice")
        self.run_id = run_id
        self.doc_id = doc_id
        self.seq = seq


@dataclass(frozen=True)
class TornTail:
    """Bytes after the last newline of a segment: a write cut short by a crash."""

    segment: str
    byte_offset: int


@dataclass(frozen=True)
class StoredLine:
    """One whole stored line, parsed but not upgraded or validated."""

    segment: str
    byte_offset: int
    raw: dict[str, Any]


def _check_writer_id(writer_id: Any) -> str:
    if not isinstance(writer_id, str) or not _WRITER_ID_RE.fullmatch(writer_id):
        raise ValueError(f"writer_id must match {_WRITER_ID_RE.pattern}, got {writer_id!r}")
    return writer_id


def _parse_lines(segment: str, data: bytes, base: int) -> tuple[list[StoredLine], int]:
    """Parse the whole lines of ``data`` (which starts at file offset ``base``).

    Returns the lines and the number of bytes consumed (up to and including the last
    newline). Raises ``CorruptLine`` for a whole line that is not a JSON object.
    """
    end = data.rfind(b"\n") + 1
    out: list[StoredLine] = []
    pos = 0
    while pos < end:
        nl = data.index(b"\n", pos)
        line = data[pos:nl]
        try:
            raw = json.loads(line)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CorruptLine(f"{segment} at byte {base + pos}: not JSON ({exc})") from None
        if not isinstance(raw, dict):
            raise CorruptLine(f"{segment} at byte {base + pos}: not a JSON object")
        out.append(StoredLine(segment, base + pos, raw))
        pos = nl + 1
    return out, end


# In-process document ownership: (store root, run_id, doc_id) -> the live writer.
_CLAIMS: dict[tuple[str, str, str], weakref.ref[LogWriter]] = {}
_CLAIMS_LOCK = threading.Lock()


class EventLog:
    """The event log of every run under ``<root>/runs``."""

    def __init__(self, root: Path, blobs: BlobStore) -> None:
        self.root = Path(root)
        self.blobs = blobs

    # --- paths ----------------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        # Run ids are validated by the Run model on create; reject separators on read too.
        if not isinstance(run_id, str) or not run_id or "/" in run_id or run_id in {".", ".."}:
            raise ValueError(f"bad run id {run_id!r}")
        return self.root / "runs" / run_id

    def segment_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "segments"

    def _segments(self, run_id: str) -> list[Path]:
        d = self.segment_dir(run_id)
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir() if p.suffix == ".jsonl" and p.is_file())

    # --- run header -----------------------------------------------------------------

    def create_run(self, run: Run) -> None:
        """Write ``run.json`` once. Raises ``RunExists`` if it is already there."""
        run_dir = self.run_dir(run.run_id)
        _makedirs_durable(run_dir / "segments")
        body = canonical_json(run.model_dump(mode="json"))
        if not write_exclusive(run_dir / "run.json", body):
            raise RunExists(run.run_id)

    def read_run(self, run_id: str) -> Run:
        try:
            raw = json.loads((self.run_dir(run_id) / "run.json").read_bytes())
        except FileNotFoundError:
            raise FileNotFoundError(f"no run {run_id!r} under {self.root}") from None
        return Run.from_stored(raw)

    # --- writers --------------------------------------------------------------------

    def open_writer(self, run_id: str, writer_id: str, flush_every: int = 16) -> LogWriter:
        """Open the single live writer for ``writer_id`` in ``run_id`` (see module doc)."""
        _check_writer_id(writer_id)
        if not isinstance(flush_every, int) or isinstance(flush_every, bool) or flush_every < 1:
            raise ValueError(f"flush_every must be an int >= 1, got {flush_every!r}")
        if not (self.run_dir(run_id) / "run.json").is_file():
            raise FileNotFoundError(f"no run {run_id!r} under {self.root}")
        return LogWriter(self, run_id, writer_id, flush_every)

    # --- reading --------------------------------------------------------------------

    def iter_stored(self, run_id: str) -> Iterator[StoredLine]:
        """Every whole stored line of a run, in segment-name then file order, not upgraded."""
        for seg in self._segments(run_id):
            lines, _ = _parse_lines(seg.name, seg.read_bytes(), 0)
            yield from lines

    def torn_tails(self, run_id: str) -> list[TornTail]:
        """Segments whose last bytes do not end in a newline, with where the torn part starts."""
        out: list[TornTail] = []
        for seg in self._segments(run_id):
            data = seg.read_bytes()
            if data and not data.endswith(b"\n"):
                out.append(TornTail(seg.name, data.rfind(b"\n") + 1))
        return out

    def iter_events(self, run_id: str, doc_id: str | None = None) -> Iterator[Event]:
        """Flushed events, upgraded and validated, ordered by ``(doc_id, seq)``."""
        rows = [s for s in self.iter_stored(run_id) if doc_id is None or s.raw.get("doc_id") == doc_id]
        events = [Event.from_stored(s.raw) for s in rows]
        events.sort(key=lambda e: (e.doc_id, e.seq))
        yield from events

    def doc_heads(self, run_id: str) -> dict[str, str]:
        """``doc_id -> hash`` of the highest-``seq`` flushed event of each document.

        Raises ``ForkedChain`` if any document has two stored events with one ``seq``.
        """
        return {d: h for d, (_, h) in self.doc_head_seqs(run_id).items()}

    def doc_head_seqs(self, run_id: str) -> dict[str, tuple[int, str]]:
        """``doc_id -> (seq, hash)`` of each document's head, sorted by doc id.

        Raises ``ForkedChain`` like ``doc_heads``.
        """
        heads = _Heads()
        for s in self.iter_stored(run_id):
            heads.add(s.raw, s)
        heads.check_forks(run_id)
        return dict(sorted(heads.heads.items()))


class _Heads:
    """Each document's highest ``(seq, hash)``, plus any ``(doc_id, seq)`` stored twice."""

    def __init__(self) -> None:
        self.heads: dict[str, tuple[int, str]] = {}
        self._seqs: dict[str, set[int]] = {}
        self.forks: dict[str, int] = {}  # doc_id -> the lowest seq seen twice

    def add(self, raw: Mapping[str, Any], where: StoredLine) -> None:
        doc, seq, h = raw.get("doc_id"), raw.get("seq"), raw.get("hash")
        if not isinstance(doc, str) or not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(h, str):
            raise CorruptLine(f"{where.segment} at byte {where.byte_offset}: missing doc_id, seq or hash")
        seqs = self._seqs.setdefault(doc, set())
        if seq in seqs:
            self.forks[doc] = min(seq, self.forks.get(doc, seq))
        seqs.add(seq)
        cur = self.heads.get(doc)
        if cur is None or seq > cur[0]:
            self.heads[doc] = (seq, h)

    def check_forks(self, run_id: str, doc_id: str | None = None) -> None:
        docs = sorted(self.forks) if doc_id is None else [doc_id] if doc_id in self.forks else []
        if docs:
            raise ForkedChain(run_id, docs[0], self.forks[docs[0]])


class _DiskHeads:
    """Incremental reader of every segment's whole lines, tracking each document's head."""

    def __init__(self, log: EventLog, run_id: str) -> None:
        self._log = log
        self._run_id = run_id
        self._offsets: dict[str, int] = {}
        self.heads = _Heads()

    def refresh(self) -> None:
        for seg in self._log._segments(self._run_id):
            start = self._offsets.get(seg.name, 0)
            with seg.open("rb") as f:
                f.seek(start)
                data = f.read()
            lines, used = _parse_lines(seg.name, data, start)
            for s in lines:
                self.heads.add(s.raw, s)
            self._offsets[seg.name] = start + used


class LogWriter:
    """Appends events for one run to one segment. Thread-safe; one live instance per id."""

    def __init__(self, log: EventLog, run_id: str, writer_id: str, flush_every: int) -> None:
        self.log = log
        self.run_id = run_id
        self.writer_id = writer_id
        self.flush_every = flush_every
        self._lock = threading.RLock()
        self._buffer: list[bytes] = []
        self._heads: dict[str, tuple[int, str]] = {}  # documents this writer holds
        self._disk = _DiskHeads(log, run_id)
        self._closed = False
        self._failed: BaseException | None = None
        self._root_key = os.path.realpath(log.root)
        self._file, self.segment = self._open_segment()

    # --- segment ownership ----------------------------------------------------------

    def _open_segment(self) -> tuple[Any, Path]:
        seg_dir = self.log.segment_dir(self.run_id)
        _makedirs_durable(seg_dir)
        ks = [0]
        for p in seg_dir.iterdir():
            m = _SEGMENT_RE.fullmatch(p.name)
            if m and m["writer"] == self.writer_id:
                ks.append(int(m["k"] or 0))
        k = max(ks)
        while True:
            path = seg_dir / (f"{self.writer_id}.jsonl" if k == 0 else f"{self.writer_id}.{k}.jsonl")
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            f = os.fdopen(fd, "ab", buffering=0)  # closing (or GC) releases the flock
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.close()
                raise WriterInUse(f"writer {self.writer_id!r} of run {self.run_id!r} is live") from None
            # Every open, not only on create: the process that created the segment may
            # have crashed before its directory fsync, leaving the entry undurable.
            _fsync_dir(seg_dir)
            size = os.fstat(fd).st_size
            if size == 0 or _last_byte(path) == b"\n":
                return f, path
            f.close()  # torn: leave it as it is, move to the next segment
            k += 1

    def _claim(self, doc_id: str) -> None:
        key = (self._root_key, self.run_id, doc_id)
        with _CLAIMS_LOCK:
            ref = _CLAIMS.get(key)
            other = ref() if ref is not None else None
            if other is not None and other is not self and not other._closed:
                raise DocumentInUse(f"document {doc_id!r} of run {self.run_id!r} is held by writer {other.writer_id!r}")
            _CLAIMS[key] = weakref.ref(self)
        try:
            self._disk.refresh()
            self._disk.heads.check_forks(self.run_id, doc_id)
        except BaseException:
            self._release(doc_id)
            raise
        self._heads[doc_id] = self._disk.heads.heads.get(doc_id, (-1, GENESIS))

    def _release(self, doc_id: str) -> None:
        key = (self._root_key, self.run_id, doc_id)
        with _CLAIMS_LOCK:
            ref = _CLAIMS.get(key)
            if ref is not None and ref() is self:
                del _CLAIMS[key]

    # --- appending ------------------------------------------------------------------

    def _check_usable(self) -> None:
        if self._closed:
            raise RuntimeError(f"writer {self.writer_id!r} is closed")
        if self._failed is not None:
            raise RuntimeError(f"writer {self.writer_id!r} failed earlier: {self._failed!r}")

    def append(
        self,
        *,
        doc_id: str,
        node: str,
        kind: str,
        agent: str | None = None,
        attempt: int = 0,
        model: str | None = None,
        prompt_id: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        tokens_cached: int | None = None,
        price_basis: str | None = None,
        latency_ms: float | None = None,
        decision: Any = None,
        cause: str | None = None,
        inputs: Iterable[BlobRef | Mapping[str, Any]] = (),
        outputs: Iterable[BlobRef | Mapping[str, Any]] = (),
        payload: Mapping[str, Any] | None = None,
        ts: str | None = None,
    ) -> Event:
        """Append one event to ``doc_id``'s chain and return it as stored.

        The event is buffered, not yet durable: it reaches disk (and readers) on the next
        flush, which ``flush()``, ``close()`` or every ``flush_every``-th append performs.
        Index or export callers must act on it only after that flush. Raises
        ``ForkedChain`` if this writer takes ``doc_id`` and its stored chain is forked.
        """
        fields: dict[str, Any] = copy.deepcopy(
            {
                "run_id": self.run_id,
                "doc_id": doc_id,
                "node": node,
                "kind": kind,
                "agent": agent,
                "attempt": attempt,
                "model": model,
                "prompt_id": prompt_id,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "tokens_cached": tokens_cached,
                "price_basis": price_basis,
                "latency_ms": latency_ms,
                "decision": decision,
                "cause": cause,
                "inputs": list(inputs),
                "outputs": list(outputs),
                "payload": {} if payload is None else payload,
            }
        )
        with self._lock:
            self._check_usable()
            if not isinstance(doc_id, str) or not doc_id:
                raise ValueError(f"doc_id must be a non-empty string, got {doc_id!r}")
            if doc_id not in self._heads:
                self._claim(doc_id)
            seq, prev = self._heads[doc_id]
            event = Event.build(**fields, seq=seq + 1, prev_hash=prev, ts=utc_now() if ts is None else ts)
            for ref in (*event.inputs, *event.outputs):
                if self.log.blobs.exists(ref.sha256) != "present":
                    raise BlobNotStored(f"{ref.role} blob {ref.sha256} is not present")
            line = canonical_json(event.model_dump(mode="json")) + b"\n"
            self._buffer.append(line)
            self._heads[doc_id] = (event.seq, event.hash)
            if len(self._buffer) >= self.flush_every:
                self._flush_locked()
            return Event.model_validate(json.loads(line))

    # --- flushing and closing -------------------------------------------------------

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        data = memoryview(b"".join(self._buffer))
        try:
            fd = self._file.fileno()
            while data:
                n = os.write(fd, data)
                data = data[n:]
            os.fsync(fd)
        except BaseException as exc:
            self._failed = exc
            raise
        self._buffer.clear()

    def flush(self) -> None:
        """Write every buffered event and fsync the segment.

        On return, every event this writer's ``append`` has returned is durable. If the
        write or fsync raises, the writer is failed (see the module doc).
        """
        with self._lock:
            self._check_usable()
            self._flush_locked()

    def close(self) -> None:
        """Flush, release the segment and every held document. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            try:
                if self._failed is None:
                    self._flush_locked()
            finally:
                self._closed = True
                self._file.close()
                for doc_id in list(self._heads):
                    self._release(doc_id)

    def __enter__(self) -> LogWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _last_byte(path: Path) -> bytes:
    with path.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1)
