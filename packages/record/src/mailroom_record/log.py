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
- A short write is continued until every byte is written; a write that makes no progress
  (returns 0) raises ``OSError``. If a write or fsync fails the writer is marked failed
  and refuses further use: its in-memory heads may be ahead of disk, so a new writer must
  take over. A write that fails part way leaves a torn line, reported by ``torn_tails``. If
  the segment's directory fsync fails while opening a writer, the segment's ``flock`` is
  released before the error propagates, so the writer id can be opened again. A failed
  fsync does not mean the bytes are absent: they may sit in the page cache, visible to readers.
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
verifier can hash the stored form (ADR-0005). ``_run`` sorts like any other doc id.

Every reader streams: a segment is read one line at a time, never whole, and a reader
reads a segment only up to its size when the reader opened it and stops at the first line
that has no newline yet, so a writer appending meanwhile can never hand it half a line
(the rest of a line it saw the start of). Memory is bounded by the longest line plus, for
the ordered readers, a few machine words per event; never by the size of the run.

Streaming means a strict reader can fail part way. ``iter_stored`` yields the lines before a
corrupt one and then raises ``CorruptLine``; ``iter_events`` (pass two) yields the valid
events before an invalid one and then raises its validation error. A caller that commits
something derived from what it read (``seal_run``, an export) must therefore read
everything first (``list(...)``) and act only once the read has finished, never while it is
still yielding.

Ordering contract. ``iter_stored``, ``iter_raw`` and the head readers go in segment-name
order, then file order. ``iter_events`` (and ``scan(...).events()``) yield grouped by
document in ``doc_id`` order, each document by ``seq``; two stored events with one
``(doc_id, seq)`` (a fork) both come back, in segment-name then file order. A segment is
not sorted by document, so this order cannot be a streaming merge: ``scan`` makes two
passes. Pass one streams every segment and keeps only ``(seq, segment, offset, length)``
per event, packed in one ``array`` per document (32 bytes per event); pass two walks each
document's rows in order without copying them (they are a few ascending runs, one per
hand-over, merged lazily; ``RunScan._ordered``), seeks to each line and parses it. Pass one parses every line, so a corrupt line raises ``CorruptLine`` before the
first event is yielded; an event that fails validation raises when pass two reaches it.
The tolerant mode (``strict=False``, for the index) records such lines as ``BadLine`` and
skips them instead. ``spans`` limits a scan to byte ranges of named segments (the index's
per-document locations, Task 8), so a per-document read need not read the whole run.

``iter_raw`` is the tolerant reader for verification (Task 7): it streams every segment one
line at a time and parses nothing, yielding each whole line as bytes (``RawLine``) and each
segment's torn tail (``TornTail``) in file order. A corrupt line is just bytes to it, so one
bad line never stops the scan of the lines after it; deciding what is corrupt is the
caller's job. The strict readers above are what writers and ``seal_run`` use.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import heapq
import json
import os
import re
import threading
import weakref
from array import array
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mailroom_record.blobs import BlobStore, _fsync_dir, _makedirs_durable, write_exclusive
from mailroom_record.hashing import canonical_json
from mailroom_record.models import GENESIS, INT64_MAX, BlobRef, Event, Run, utc_now, validation_detail

__all__ = [
    "BadLine",
    "BlobNotStored",
    "CorruptLine",
    "DocumentInUse",
    "EventLog",
    "ForkedChain",
    "LocatedEvent",
    "LogWriter",
    "OverlongLine",
    "RawLine",
    "RunExists",
    "RunScan",
    "StoredLine",
    "TornTail",
    "WriterInUse",
]

# Above this many ascending runs in one document's rows (a damaged log), pass two sorts a
# copy instead of merging the runs lazily (RunScan._ordered).
_MAX_RUNS = 1024

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


@dataclass(frozen=True)
class OverlongLine:
    """A whole line longer than the reader's limit: skipped unread, only its size is known."""

    segment: str
    byte_offset: int
    length: int


@dataclass(frozen=True)
class RawLine:
    """One whole stored line as bytes, without its newline, not parsed."""

    segment: str
    byte_offset: int
    data: bytes


@dataclass(frozen=True)
class BadLine:
    """A whole line a tolerant scan skipped: not a JSON object, or not a valid event.

    ``detail`` names the parse or validation error only (field names and error types),
    never the line's content.
    """

    segment: str
    byte_offset: int
    kind: Literal["corrupt_line", "invalid_event"]
    detail: str


@dataclass(frozen=True)
class LocatedEvent:
    """An event and where its line is stored (``length`` counts the newline)."""

    segment: str
    byte_offset: int
    length: int
    event: Event


def _stream_lines(path: Path, start: int = 0, end: int | None = None) -> Iterator[tuple[int, bytes, bool]]:
    """Stream ``(offset, line without newline, whole)`` from ``start`` up to ``end``.

    Never reads past ``end`` or the file's size when it was opened, and stops after the
    first line that has no newline within that bound (a torn tail, or a line a writer is
    still writing), so it can never return the rest of a line it saw the start of.
    """
    with path.open("rb") as f:
        size = os.fstat(f.fileno()).st_size
        stop = size if end is None else min(end, size)
        f.seek(start)
        offset = start
        while offset < stop:
            line = f.readline(stop - offset)
            if not line:
                return
            if not line.endswith(b"\n"):
                yield offset, line, False
                return
            yield offset, line[:-1], True
            offset += len(line)


def iter_raw_segment(path: Path, max_line: int | None = None) -> Iterator[RawLine | OverlongLine | TornTail]:
    """Stream one segment: each whole line as a ``RawLine``, then its torn tail, if any.

    Reads one line at a time (a line ends at ``\\n`` and nowhere else), so memory is bounded
    by the longest line, not the segment. With ``max_line``, a whole line longer than that
    many bytes (newline excluded) is not held: it is skipped in chunks up to its newline and
    yielded as an ``OverlongLine``, so memory stays bounded by ``max_line`` (a long run of
    bytes with no newline at all is the segment's torn tail). Raises ``OSError`` if the file
    cannot be read.
    """
    with path.open("rb") as f:
        stop = os.fstat(f.fileno()).st_size  # never read past the size at open
        offset = 0
        while offset < stop:
            want = stop - offset if max_line is None else min(stop - offset, max_line + 1)
            line = f.readline(want)
            if not line:
                return
            if line.endswith(b"\n"):
                yield RawLine(path.name, offset, line[:-1])
                offset += len(line)
                continue
            if max_line is None or len(line) <= max_line:  # no newline before the bound: a torn tail
                yield TornTail(path.name, offset)
                return
            length, ended = len(line), False  # an over-long line: skip it in chunks up to its newline
            while not ended and offset + length < stop:
                chunk = f.readline(min(1 << 20, stop - offset - length))
                if not chunk:
                    break
                length += len(chunk)
                ended = chunk.endswith(b"\n")
            if not ended:  # no newline before the bound: a torn tail
                yield TornTail(path.name, offset)
                return
            yield OverlongLine(path.name, offset, length - 1)
            offset += length


def _check_writer_id(writer_id: Any) -> str:
    if not isinstance(writer_id, str) or not _WRITER_ID_RE.fullmatch(writer_id):
        raise ValueError(f"writer_id must match {_WRITER_ID_RE.pattern}, got {writer_id!r}")
    return writer_id


def _parse_line(segment: str, offset: int, line: bytes) -> dict[str, Any]:
    """Parse one whole line. Raises ``CorruptLine`` if it is not a JSON object."""
    try:
        raw = json.loads(line)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:  # deep nesting too
        raise CorruptLine(f"{segment} at byte {offset}: not JSON ({exc})") from None
    if not isinstance(raw, dict):
        raise CorruptLine(f"{segment} at byte {offset}: not a JSON object")
    return raw


def _iter_segment_stored(path: Path) -> Iterator[StoredLine]:
    for offset, data, whole in _stream_lines(path):
        if whole:
            yield StoredLine(path.name, offset, _parse_line(path.name, offset, data))


def _safe_detail(exc: BaseException) -> str:
    """An error description without the input it rejected (no stored content leaks)."""
    detail = validation_detail(exc)  # field names and error types; stored keys become <key>
    if detail is not None:
        return "invalid event: " + detail
    if isinstance(exc, CorruptLine):
        return str(exc).split(": ", 1)[-1]
    # UnknownSchemaVersion names the version's type or number, never a stored value.
    return type(exc).__name__ + (f": {exc}" if type(exc).__name__ == "UnknownSchemaVersion" else "")


def _is_seq(value: Any) -> bool:
    """A ``seq`` the readers can hold: a non-bool int in ``0..INT64_MAX`` (``Count``)."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= INT64_MAX


class RunScan:
    """Pass one of an ordered read: where each event of a run (or of one document) lives.

    Built by ``EventLog.scan``. Holds ``(seq, segment, offset, length)`` per event packed in
    one ``array`` per document, so it is a few dozen bytes per event, whatever the payloads.
    ``events()`` is pass two (see "Ordering contract" in the module doc).
    """

    def __init__(self, log: EventLog, run_id: str, doc_id: str | None, strict: bool) -> None:
        self.log = log
        self.run_id = run_id
        self.doc_id = doc_id
        self.strict = strict
        self.segments: list[Path] = []
        self.ends: dict[str, int] = {}  # segment -> end of the last whole line scanned
        self.torn: list[TornTail] = []
        self.bad: list[BadLine] = []
        self._docs: dict[str, array[int]] = {}

    def _bad(self, segment: str, offset: int, kind: Literal["corrupt_line", "invalid_event"], exc: BaseException) -> None:
        if self.strict:
            raise exc
        self.bad.append(BadLine(segment, offset, kind, _safe_detail(exc)))

    def _scan_span(self, seg_idx: int, path: Path, start: int, end: int | None) -> None:
        name = path.name
        reached = start
        for offset, data, whole in _stream_lines(path, start, end):
            if not whole:
                if end is None:
                    self.torn.append(TornTail(name, offset))
                break
            reached = offset + len(data) + 1
            try:
                raw = _parse_line(name, offset, data)
            except CorruptLine as exc:
                self._bad(name, offset, "corrupt_line", exc)
                continue
            doc, seq = raw.get("doc_id"), raw.get("seq")
            if self.doc_id is not None and isinstance(doc, str) and doc != self.doc_id:
                continue  # another document's line: a per-document read validates only its own
            if not isinstance(doc, str) or not _is_seq(seq):
                try:
                    Event.from_stored(raw)  # raises the validation error a reader expects
                    exc: BaseException = CorruptLine(f"{name} at byte {offset}: missing doc_id or seq")
                except (ValueError, TypeError, RecursionError) as err:
                    exc = err
                self._bad(name, offset, "invalid_event", exc)
                continue
            arr = self._docs.get(doc)
            if arr is None:
                arr = self._docs[doc] = array("q")
            arr.extend((seq, seg_idx, offset, len(data) + 1))
        self.ends[name] = max(self.ends.get(name, 0), reached)

    def doc_ids(self) -> list[str]:
        return sorted(self._docs)

    def seqs(self, doc_id: str) -> list[int]:
        """Every stored ``seq`` of ``doc_id`` in this scan, sorted (a fork repeats one)."""
        return [row[0] for row in self._ordered(doc_id)]

    def gaps(self) -> dict[str, int]:
        """``doc_id -> first missing seq`` for each document whose seqs skip a number.

        Walks each document's rows in order (``_ordered``): no list, set or ``range(max)``.
        """
        out: dict[str, int] = {}
        for doc in self.doc_ids():
            expect = 0
            for seq, *_ in self._ordered(doc):
                if seq > expect:
                    out[doc] = expect
                    break
                expect = max(expect, seq + 1)
        return out

    def _ordered(self, doc_id: str) -> Iterator[tuple[int, int, int, int]]:
        """``doc_id``'s rows ``(seq, segment, offset, length)`` in ``(seq, segment, offset)`` order.

        Pass one appends rows in segment then file order, and an owner writes a document's
        events in ``seq`` order, so the rows are a few ascending runs (one per hand-over
        between segments). Those runs are merged lazily: memory is one small iterator per
        run, not a copy of the rows. Only a damaged log (forks, lines out of order) with
        more than ``_MAX_RUNS`` runs is sorted as a copy (a few hundred bytes per event).
        """
        arr = self._docs.get(doc_id)
        if arr is None:
            return iter(())
        n = len(arr) // 4
        starts = [0]
        for i in range(1, n):
            j = 4 * i
            if (arr[j], arr[j + 1], arr[j + 2]) < (arr[j - 4], arr[j - 3], arr[j - 2]):
                starts.append(i)
                if len(starts) > _MAX_RUNS:
                    return iter(sorted(tuple(arr[k : k + 4]) for k in range(0, len(arr), 4)))  # type: ignore[misc]

        def run(a: int, b: int) -> Iterator[tuple[int, int, int, int]]:
            for i in range(a, b):
                j = 4 * i
                yield arr[j], arr[j + 1], arr[j + 2], arr[j + 3]

        bounds = list(zip(starts, starts[1:] + [n]))
        if len(bounds) == 1:
            return run(0, n)
        return heapq.merge(*(run(a, b) for a, b in bounds))

    def stored_hashes(self, doc_id: str, seq: int) -> list[str | None]:
        """The stored ``hash`` of each line this scan holds for ``(doc_id, seq)`` (a fork has two).

        Reads only those lines; a line that no longer parses, or no longer names
        ``(doc_id, seq)``, gives ``None``.
        """
        arr = self._docs.get(doc_id)
        out: list[str | None] = []
        if arr is None:
            return out
        for j in range(0, len(arr), 4):
            if arr[j] != seq:
                continue
            path, offset, length = self.segments[arr[j + 1]], arr[j + 2], arr[j + 3]
            with path.open("rb") as f:
                f.seek(offset)
                data = f.read(length)
            try:
                raw = _parse_line(path.name, offset, data[:-1]) if data.endswith(b"\n") else None
            except CorruptLine:
                raw = None
            ok = raw is not None and raw.get("doc_id") == doc_id and raw.get("seq") == seq
            h = raw.get("hash") if ok and raw is not None else None
            out.append(h if isinstance(h, str) else None)
        return out

    def __len__(self) -> int:
        return sum(len(a) // 4 for a in self._docs.values())

    def events(self) -> Iterator[LocatedEvent]:
        """Pass two: every scanned event, upgraded and validated, in the documented order."""
        files: dict[int, Any] = {}
        try:
            for doc in self.doc_ids():
                for seq, seg_idx, offset, length in self._ordered(doc):
                    path = self.segments[seg_idx]
                    f = files.get(seg_idx)
                    if f is None:
                        f = files[seg_idx] = path.open("rb")
                    f.seek(offset)
                    data = f.read(length)
                    try:
                        if len(data) != length or not data.endswith(b"\n"):
                            raise CorruptLine(f"{path.name} at byte {offset}: line changed while reading")
                        raw = _parse_line(path.name, offset, data[:-1])
                        if raw.get("doc_id") != doc or raw.get("seq") != seq:
                            raise CorruptLine(f"{path.name} at byte {offset}: line changed while reading")
                    except CorruptLine as exc:
                        self._bad(path.name, offset, "corrupt_line", exc)
                        continue
                    try:
                        event = Event.from_stored(raw)
                    except (ValueError, TypeError, RecursionError) as exc:
                        self._bad(path.name, offset, "invalid_event", exc)
                        continue
                    yield LocatedEvent(path.name, offset, length, event)
        finally:
            for f in files.values():
                f.close()


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
        _makedirs_durable(run_dir / "segments", self.root)
        body = canonical_json(run.model_dump(mode="json"))
        if not write_exclusive(run_dir / "run.json", body, self.root):
            raise RunExists(run.run_id)

    def read_run(self, run_id: str) -> Run:
        """The run header. ``FileNotFoundError`` for an unknown run; ``ValueError`` or
        ``TypeError`` for a damaged ``run.json`` (never ``RecursionError``)."""
        try:
            raw = json.loads((self.run_dir(run_id) / "run.json").read_bytes())
        except FileNotFoundError:
            raise FileNotFoundError(f"no run {run_id!r} under {self.root}") from None
        except RecursionError:
            raise ValueError(f"run.json of run {run_id!r} nests deeper than the JSON parser allows") from None
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
        """Every whole stored line of a run, in segment-name then file order, not upgraded.

        Streamed: lines before a corrupt one are yielded before ``CorruptLine`` is raised.
        """
        for seg in self._segments(run_id):
            yield from _iter_segment_stored(seg)

    def segment_paths(self, run_id: str) -> list[Path]:
        """Every segment file of a run, sorted by name."""
        return self._segments(run_id)

    def iter_raw(self, run_id: str) -> Iterator[RawLine | TornTail]:
        """Every segment's whole lines and torn tail, streamed and unparsed (see module doc)."""
        for seg in self._segments(run_id):
            yield from iter_raw_segment(seg)

    def torn_tails(self, run_id: str) -> list[TornTail]:
        """Segments whose last bytes do not end in a newline, with where the torn part starts."""
        out: list[TornTail] = []
        for seg in self._segments(run_id):
            start = _torn_start(seg)
            if start is not None:
                out.append(TornTail(seg.name, start))
        return out

    def scan(
        self,
        run_id: str,
        doc_id: str | None = None,
        *,
        spans: Mapping[str, Iterable[tuple[int, int | None]]] | None = None,
        strict: bool = True,
    ) -> RunScan:
        """Pass one of an ordered read (see "Ordering contract" in the module doc).

        ``spans`` maps a segment name to the ``(start, end)`` byte ranges to read (``end``
        ``None`` reads to the end); segments it does not name are not read. Every ``start``
        must be a line boundary. Without ``spans`` every segment is read whole. Strict
        (the default) raises ``CorruptLine`` and validation errors like ``iter_events``;
        ``strict=False`` records them in ``RunScan.bad`` and skips the line.
        """
        out = RunScan(self, run_id, doc_id, strict)
        paths = self._segments(run_id)
        if spans is not None:
            paths = [p for p in paths if p.name in spans]
        out.segments = paths
        for i, path in enumerate(paths):
            ranges = [(0, None)] if spans is None else sorted(spans[path.name], key=lambda r: r[0])
            for start, end in ranges:
                out._scan_span(i, path, start, end)
        return out

    def iter_located(
        self,
        run_id: str,
        doc_id: str | None = None,
        *,
        spans: Mapping[str, Iterable[tuple[int, int | None]]] | None = None,
    ) -> Iterator[LocatedEvent]:
        """Like ``iter_events``, with each event's segment, offset and line length."""
        yield from self.scan(run_id, doc_id, spans=spans).events()

    def iter_events(self, run_id: str, doc_id: str | None = None) -> Iterator[Event]:
        """Flushed events, upgraded and validated, grouped by ``doc_id`` and ordered by ``seq``.

        Streamed in two passes (see "Ordering contract" in the module doc): memory is a few
        machine words per event plus one line, never the run's payloads.
        """
        for located in self.scan(run_id, doc_id).events():
            yield located.event

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
        if not _is_seq(seq):  # negative, or past int64: no writer stores it, no reader can hold it
            raise CorruptLine(f"{where.segment} at byte {where.byte_offset}: seq out of range 0..2**63-1")
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
            for offset, data, whole in _stream_lines(seg, self._offsets.get(seg.name, 0)):
                if not whole:
                    break
                raw = _parse_line(seg.name, offset, data)
                self.heads.add(raw, StoredLine(seg.name, offset, raw))
                # Advance per line: a corrupt line raises here and is re-read next time,
                # while the lines before it are never added twice (which would look forked).
                self._offsets[seg.name] = offset + len(data) + 1


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
        _makedirs_durable(seg_dir, self.log.root)
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
            try:
                # Every open, not only on create: the process that created the segment may
                # have crashed before its directory fsync, leaving the entry undurable.
                _fsync_dir(seg_dir)
                size = os.fstat(fd).st_size
                if size == 0 or _last_byte(path) == b"\n":
                    return f, path
            except BaseException:
                f.close()  # release the flock now, not when the traceback is collected
                raise
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
            while data:  # a short write is continued until every byte is written
                n = os.write(fd, data)
                if n <= 0:  # no progress: fail loudly instead of retrying forever
                    raise OSError(errno.EIO, f"os.write wrote 0 bytes to {self.segment.name}")
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


def _torn_start(path: Path, chunk: int = 1 << 16) -> int | None:
    """Where the torn tail of ``path`` starts, or ``None`` if it ends in a newline (or is empty).

    Reads backwards from the end in ``chunk``-sized pieces, so a large segment is not read.
    """
    with path.open("rb") as f:
        end = os.fstat(f.fileno()).st_size
        if end == 0:
            return None
        f.seek(end - 1)
        if f.read(1) == b"\n":
            return None
        pos = end
        while pos > 0:
            start = max(0, pos - chunk)
            f.seek(start)
            nl = f.read(pos - start).rfind(b"\n")
            if nl >= 0:
                return start + nl + 1
            pos = start
        return 0


def _last_byte(path: Path) -> bytes:
    with path.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1)
