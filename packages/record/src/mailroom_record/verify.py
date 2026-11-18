"""Verify a run: document chains, seals, column files and blobs (ADR-0002, ADR-0003, ADR-0005).

``verify_run(log, blobs, run_id)`` reads a run from disk and returns a ``VerifyReport``. It
never raises on damaged data: every failure it meets becomes a ``Problem`` naming the
document, ``seq``, segment and byte offset, or seal, where known, and verification carries
on with everything else. Only programmer errors raise: an unknown ``run_id`` (no run
directory) raises ``FileNotFoundError``, a malformed one ``ValueError``.

What is checked
---------------
Lines. Each segment is streamed one line at a time (``EventLog.iter_raw``), so memory holds
one line plus a few small fields per event, never every payload. A whole line that is not a
JSON object, or has no usable ``doc_id``, ``hash`` and ``prev_hash``, is a
``corrupt_line`` (segment and byte offset); the scan continues after it. A line of a
document whose ``seq`` is not an integer in ``0..2**63-1`` (negative, past int64, not a
number) has no place in the chain: it is one ``invalid_event`` of that document, as the
index reports it, and adds no ``seq_gap``. Each event's hash is
recomputed on the stored dict as parsed, before any upgrade (ADR-0005): a mismatch is
``bad_hash``. A line whose bytes are not the canonical encoding of what it parses to (a
writer only ever writes canonical lines) is a ``corrupt_line`` too. A line that hashes but
does not validate as an event after upgrading (a bad field, a newer ``schema_version``,
another run's ``run_id``, an integer outside Parquet's int64 that ``seal_run`` could not
export) is ``invalid_event``. A line longer than ``MAX_LINE`` (64 MiB) is a
``corrupt_line`` and is skipped in chunks, never held in memory. Stored bytes that nest
deeper than the JSON parser allows are corrupt like any other unparsable bytes: no parse,
encode or hash of stored data lets an exception escape.

Chains, per document. ``seq`` must run from 0 with no gap (``seq_gap``, one per missing
range); a ``seq`` stored twice is ``forked_chain``; seq 0 links to ``GENESIS`` and every
other event's ``prev_hash`` must equal the recomputed hash of an event at ``seq - 1``
(``broken_link``). So an edited event whose stored hash was left alone gives ``bad_hash``
on it and ``broken_link`` on the next event; one whose hash was recomputed gives
``broken_link`` only.

Seals. Seal files are read as stored: unreadable JSON, a file that does not validate as a
seal, holds another number or run, or is not canonical JSON is ``seal_corrupt``. A number
missing from ``0..max`` (counting seal files and column files) is ``seal_missing``: a seal
deleted from the middle, or the last seal deleted while its column file remains (the seal
is written before its column file, so a column file never legitimately outlives it). Each
readable seal must hash to its stored hash and chain to the stored hash of the seal before
it, its ``document_ids`` must equal its sorted ``doc_heads`` keys, it must keep every
document of the previous seal at the same or a later head, and its ``event_count`` must
equal the sum of ``head seq + 1`` over its documents; any of these failing is
``seal_mismatch``. Against the log: a sealed document with no line at all is
``document_missing_from_log``; a sealed head hash the log no longer holds is
``truncated_after_seal`` (each reported once per document, at the first seal concerned).

Column files. For each readable seal, ``columns/events-<n>.parquet`` must exist
(``columns_missing``), be readable with exactly ``EVENT_SCHEMA``, and hold one row per event
the seal added: per document, the events after the previous seal's head up to this seal's
head. Rows are compared field by field with the rows the log's lines give
(``seal.EVENT_COLUMNS`` built the same way ``seal_run`` builds them); any difference, a row
the log does not account for, or a row it lacks is ``columns_mismatch`` (one problem per
document, naming the first ``seq``). Documents with a log-level problem are left out of
this comparison, because their rows cannot be expected exactly; their damage is already
reported. A document is left out only of the seals whose rows of it the damage touches
(a problem at ``seq`` s, or a gap over s, leaves it out of the seal that added s; a
problem with no ``seq`` leaves it out of every seal), so damage after the last seal never
masks a tampered column file. A seal whose predecessor is unreadable or missing cannot
bound its rows, so its column file is not compared. A missing column file of the latest
seal does not fail the run: ``seal_run`` writes it just after the seal file, so a crash
or a seal in progress leaves it missing for a while, and the next ``seal_run`` rebuilds
it. That holds on a closed run too: the closing seal may still be writing it, and the
export is derived from the log and the seal, which verify has checked. No later seal
comes on a closed run, so the detail says to run ``seal_run`` once more, which rebuilds
the file and commits nothing new. A missing older one fails (``seal_run`` rebuilds it too). A path that holds something other
than a regular file (a directory), or that cannot be checked (an ``OSError`` other than
"not found"), is ``columns_mismatch`` for that seal, latest or not, and verification goes
on with the other seals.

Blobs. Every blob an event names is checked once. Tombstoned (tombstone, no content):
passes, counted in ``tombstoned_blobs``, if the tombstone is the record ``BlobStore``
writes (a JSON object with this hash, a non-empty reason and a time); any other file (or
a directory) at the tombstone path is ``corrupt_tombstone``, because a tombstone that is
not a deletion record must not pass as a lawful deletion (``BlobStore.tombstone`` rewrites such a file with a new record). Neither content nor tombstone:
``missing_blob``. Content next to its own tombstone (a crash between writing the tombstone and unlinking
the content, or content restored afterwards): ``tombstone_with_content``. Content that does
not decompress, is not exactly one whole zstd frame, or decompresses to bytes with another SHA-256 (bit rot; ``BlobStore.put``
never rewrites present content, so nothing else repairs it): ``corrupt_blob``. Content is
streamed through the hash, never held whole, and never appears in a report. A blob problem
names the first event (by ``(doc_id, seq)``) that refers to it and how many do (an event
naming one blob twice, say as input and output, counts once). A blob whose paths cannot be
checked (an ``OSError`` such as a permission error on its shard) is ``corrupt_blob`` for
that blob only. Content deleted between the check and the read is judged as it is
afterwards: tombstoned (if the tombstone is lawful) or ``missing_blob``.

Torn tails, and when a run is closed
------------------------------------
A torn tail (bytes after a segment's last newline) is always reported and never fails
the run: its bytes were never a whole event, so they were never part of any chain, and a
writer that recovered continued every chain past them. Losing a sealed event is caught
elsewhere (``seq_gap``, ``truncated_after_seal``, ``document_missing_from_log``). An event
written after the last seal is committed by no seal, so losing it from the end of its
chain leaves nothing to detect; on a closed run, where no later seal will commit it,
verify notes it (``unsealed_events``, one problem per document naming the first ``seq``
and how many events are past the seal; it never fails the run). The torn-tail detail
says which case it is:

- A run is **closed** when its header's ``kind`` is an experiment kind (anything but
  ``service_period``) and it has at least one seal file. An experiment run is sealed once,
  when it ends (plan Task 6), so its seal is its end marker. A ``service_period`` run is
  sealed on a timer and never ends in this package, so it is never closed. A run whose
  header cannot be read is treated as closed if it has a seal.
- On a run that is **not closed**, a torn tail may be a write in progress.
- On a **closed** run it is final: a crash that was recovered from; no writer will
  complete those bytes.

A run being written while it is verified
----------------------------------------
Verify reads column files, then seal files, then segments, the reverse of the order they
are written in (events are flushed before a seal commits them, a seal file is written
before its column file), so a seal is never read ahead of the events it commits. If the
scan shows a ``seq`` gap, which a document handed between writers during the scan can
fake, the segments are scanned once more and only lines up to the per-document heads the
first scan saw are kept: each of those was on disk before the first scan ended, so a gap
still there is real. Lines written after the first scan are left to the next verify.

Every other kind always fails the run (``columns_missing`` of an older seal included).
``ok`` is true when no problem fails and the run is not pruned.

A pruned run
------------
A run with ``runs/<run_id>/prune.json`` (``prune_run`` removed its segments and seals, and
maybe its Parquet files, after packing it) is neither damaged nor an empty run: its events
are in the archive. ``verify_run`` checks nothing then and returns a report with
``pruned_archive`` (the archive ``pack.json`` names) and ``pruned_seal`` (the latest seal it
covers), no problems, and ``ok`` false. If a prune finishes while verify reads the run, the
report is that pruned report too. ``mailroom-record verify`` prints a line that starts
``PRUNED`` and exits 0. Unpack the archive to verify the run's contents.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
import zstandard

from mailroom_record.blobs import BlobStore, _zstd_frame_whole
from mailroom_record.hashing import canonical_json
from mailroom_record.log import (
    MAX_LINE,
    PACK_RECORD,
    EventLog,
    OverlongLine,
    RawLine,
    StoredLine,
    TornTail,
    iter_raw_segment,
    pruned_archive,
)
from mailroom_record.models import (
    GENESIS,
    INT64_MAX,
    Event,
    Run,
    Seal,
    event_hash,
    seal_hash,
    validation_detail,
)
from mailroom_record.seal import EVENT_SCHEMA, _columns_path, _row, _seals_dir

__all__ = ["PROBLEM_KINDS", "Problem", "ProblemKind", "VerifyReport", "verify_run"]

ProblemKind = Literal[
    # the plan's kinds (ADR-0002)
    "bad_hash",
    "broken_link",
    "seq_gap",
    "missing_blob",
    "seal_mismatch",
    "document_missing_from_log",
    "torn_tail",
    "truncated_after_seal",
    # added while building Task 7
    "corrupt_line",
    "invalid_event",
    "forked_chain",
    "corrupt_blob",
    "tombstone_with_content",
    "seal_corrupt",
    "seal_missing",
    "columns_missing",
    "columns_mismatch",
    "corrupt_run_header",
    "corrupt_tombstone",
    # added in fix pass 2: a note, never failing
    "unsealed_events",
]
PROBLEM_KINDS: tuple[str, ...] = ProblemKind.__args__  # type: ignore[attr-defined]

# Kinds that mean the document's own chain is damaged: its column rows are not compared.
_CHAIN_KINDS = frozenset(
    {
        "bad_hash",
        "broken_link",
        "seq_gap",
        "forked_chain",
        "invalid_event",
        "document_missing_from_log",
        "truncated_after_seal",
    }
)
_COLUMNS_NAME_RE = re.compile(r"events-(0|[1-9][0-9]*)\.parquet")
_SEAL_NAME_RE = re.compile(r"(0|[1-9][0-9]*)\.json")
_CHUNK = 4 * 1024 * 1024
_BATCH = 4096
# Exceptions a parse, encode or hash of stored bytes can raise: bad JSON or UTF-8, NaN,
# a non-str key, or nesting deeper than the interpreter's recursion limit.
_PARSE_ERRORS = (ValueError, TypeError, RecursionError)
# The longest line verify reads is log.MAX_LINE (64 MiB), the limit LogWriter.append
# enforces. Content belongs in blobs, so an event line is a few KB; a longer line is
# reported as corrupt_line without being held in memory.
_INT_COLUMNS = tuple(
    name for name, t in zip(EVENT_SCHEMA.names, EVENT_SCHEMA.types) if pa.types.is_integer(t)
)
_INT64 = (-(2**63), 2**63 - 1)
_INF = float("inf")


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a run. ``fails`` is false only for a torn tail (never final
    damage: its bytes were never a whole event), for the latest seal's missing column
    file (the next ``seal_run`` rebuilds it) and for ``unsealed_events`` (a note that a
    closed run holds events no seal commits).

    ``detail`` names hashes, field names, segments, offsets and counts, never content.
    """

    kind: ProblemKind
    doc_id: str | None
    seq: int | None
    detail: str
    segment: str | None = None
    byte_offset: int | None = None
    seal: int | None = None
    fails: bool = True


@dataclass(frozen=True)
class VerifyReport:
    """The outcome of ``verify_run``. ``ok`` is true when no problem ``fails`` and the run is
    not pruned.

    ``pruned_archive`` is set (and ``pruned_seal``, the latest seal the pack covers, when its
    ``pack.json`` says) when the run is pruned: then nothing was checked, ``problems`` is
    empty and ``ok`` is false, because the run's events are in the archive, not here. That
    is its own state, not damage: unpack the archive to verify the run.
    """

    ok: bool
    problems: list[Problem]
    events_checked: int
    documents_checked: int
    tombstoned_blobs: int
    seals_checked: int = 0
    closed: bool = False
    pruned_archive: str | None = None
    pruned_seal: int | None = None


@dataclass(slots=True)
class _Entry:
    """What verify keeps of one stored event: small fields only, never the payload."""

    seq: int
    stored_hash: str
    true_hash: str | None  # the recomputed hash; None if the line cannot be hashed
    prev_hash: str
    segment: str
    byte_offset: int
    row_digest: bytes | None  # digest of its column row; None if it is not a valid event


@dataclass
class _State:
    run_id: str
    problems: list[Problem] = field(default_factory=list)
    entries: dict[str, list[_Entry]] = field(default_factory=lambda: defaultdict(list))
    # sha256 -> [first (doc_id, seq), role, number of references]
    refs: dict[str, list[Any]] = field(default_factory=dict)
    torn: list[TornTail] = field(default_factory=list)
    # doc -> (first, last) seq ranges a chain problem touches; (0, inf) when unknown
    damage: dict[str, list[tuple[float, float]]] = field(default_factory=lambda: defaultdict(list))
    events_checked: int = 0
    names: dict[str, str] = field(default_factory=dict)  # interned segment names
    bound: dict[str, int] | None = None  # rescan: keep only seqs up to these heads

    def add(self, kind: ProblemKind, doc_id: str | None, seq: int | None, detail: str, **where: Any) -> None:
        self.problems.append(Problem(kind, doc_id, seq, detail, **where))
        if doc_id is not None and kind in _CHAIN_KINDS:
            self.damage[doc_id].append((0, _INF) if seq is None else (seq, seq))

    def damaged(self, doc: str, lo: float = 0, hi: float = _INF) -> bool:
        """Whether a chain problem of ``doc`` touches a seq in ``[lo, hi]``."""
        return any(a <= hi and lo <= b for a, b in self.damage.get(doc, ()))


def verify_run(log: EventLog, blobs: BlobStore, run_id: str) -> VerifyReport:
    """Verify ``run_id``'s chains, seals, column files and blobs (see the module doc).

    Raises ``FileNotFoundError`` only if the run directory does not exist, and
    ``ValueError`` for a malformed run id. Everything found on disk is reported, not raised.
    """
    run_dir = log.run_dir(run_id)
    if not run_dir.is_dir():
        raise FileNotFoundError(f"no run {run_id!r} under {log.root}")
    pruned = _pruned_report(log, run_id)
    if pruned is not None:
        return pruned
    report = _verify_contents(log, blobs, run_id)
    # A prune that finished while we read deleted what we read: report the run as pruned,
    # never the damage that deletion looks like.
    return _pruned_report(log, run_id) or report


def _pruned_report(log: EventLog, run_id: str) -> VerifyReport | None:
    archive = pruned_archive(log, run_id)
    if archive is None:
        return None
    seal: int | None = None
    try:
        n = json.loads((log.run_dir(run_id) / PACK_RECORD).read_bytes())["last_seal"]["n"]
        seal = n if isinstance(n, int) and not isinstance(n, bool) else None
    except (OSError, ValueError, RecursionError, KeyError, TypeError):
        pass
    return VerifyReport(
        ok=False,
        problems=[],
        events_checked=0,
        documents_checked=0,
        tombstoned_blobs=0,
        pruned_archive=archive,
        pruned_seal=seal,
    )


def _verify_contents(log: EventLog, blobs: BlobStore, run_id: str) -> VerifyReport:
    """Verify what is on disk for ``run_id``, pruned or not (``unpack_run`` checks a run it
    placed this way before it removes ``prune.json``)."""
    st = _State(run_id)
    run = _check_header(log, run_id, st)
    # Column files, then seal files, then segments: each is written only after the one
    # read after it here (events are flushed before a seal commits them; a seal file is
    # written before its column file), so a run written while verify reads it never
    # shows a seal ahead of its events or a column file ahead of its seal.
    col_numbers = _list_columns(log, run_id, st)
    seal_files = _read_seal_files(log, run_id, st)
    before_scan = list(st.problems)
    _scan(log, run_id, st)
    chains = _check_chains(st)
    if any(p.kind == "seq_gap" for p in st.problems):
        # A document handed between writers while the scan ran can show a gap that is
        # not on disk. Scan once more, keeping only seqs up to the heads the first scan
        # saw: each of those was on disk before the first scan ended, so a gap the second
        # scan still shows is real (the same rule as seal_run's snapshot).
        bound = {doc: max(e.seq for e in es) for doc, es in st.entries.items()}
        st = _State(run_id, problems=before_scan, bound=bound)
        _scan(log, run_id, st)
        chains = _check_chains(st)
    seals, n_seal_files = _check_seals(st, seal_files, col_numbers)
    closed = n_seal_files > 0 and (run is None or run.kind != "service_period")
    _check_columns(log, run_id, st, seals, chains, closed)
    tombstoned = _check_blobs(blobs, st)
    if closed:
        _note_unsealed(st, seals, max(seal_files[0] | col_numbers))
    for t in st.torn:
        if closed:
            detail = (
                f"{t.segment} at byte {t.byte_offset}: torn bytes on a closed run, left by a crash and "
                "final (no writer will complete them); they were never a whole event, so no committed "
                "event is lost"
            )
        else:
            detail = (
                f"{t.segment} at byte {t.byte_offset}: torn bytes at the end of a segment of a run "
                "still being written (a crash or a write in progress); skipped, not an event"
            )
        st.problems.append(
            Problem(
                "torn_tail", None, None, detail, segment=t.segment, byte_offset=t.byte_offset, fails=False
            )
        )

    return VerifyReport(
        ok=not any(p.fails for p in st.problems),
        problems=st.problems,
        events_checked=st.events_checked,
        documents_checked=len(st.entries),
        tombstoned_blobs=tombstoned,
        seals_checked=len(seals),
        closed=closed,
    )


# --- run header ---------------------------------------------------------------------


def _check_header(log: EventLog, run_id: str, st: _State) -> Run | None:
    path = log.run_dir(run_id) / "run.json"
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        st.add("corrupt_run_header", None, None, "run.json is missing")
        return None
    except OSError as exc:
        st.add("corrupt_run_header", None, None, f"run.json cannot be read ({_oserror(exc)})")
        return None
    try:
        raw = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
        run = Run.from_stored(raw)
    except _PARSE_ERRORS as exc:
        st.add("corrupt_run_header", None, None, f"run.json is not a valid run header ({_why(exc)})")
        return None
    if run.run_id != run_id:
        st.add("corrupt_run_header", None, None, f"run.json names run {run.run_id!r}")
        return None
    try:
        canonical = _canonical(raw) == data
    except _PARSE_ERRORS:
        canonical = False
    if not canonical:
        st.add("corrupt_run_header", None, None, "run.json is not in canonical form")
    return run


# --- lines --------------------------------------------------------------------------


def _scan(log: EventLog, run_id: str, st: _State) -> None:
    try:
        segments = log.segment_paths(run_id)
    except OSError as exc:
        st.add("corrupt_line", None, None, f"segments/ cannot be listed ({_oserror(exc)})")
        return
    for seg in segments:
        try:
            for item in iter_raw_segment(seg, max_line=MAX_LINE):
                if isinstance(item, TornTail):
                    st.torn.append(item)
                elif isinstance(item, OverlongLine):
                    st.add(
                        "corrupt_line",
                        None,
                        None,
                        f"{item.segment} at byte {item.byte_offset}: a line of {item.length} bytes, over the "
                        f"{MAX_LINE}-byte limit; not read",
                        segment=item.segment,
                        byte_offset=item.byte_offset,
                    )
                else:
                    _check_line(item, st)
        except OSError as exc:
            st.add(
                "corrupt_line", None, None, f"{seg.name} cannot be read ({_oserror(exc)})", segment=seg.name
            )


def _check_line(line: RawLine, st: _State) -> None:
    segment = st.names.setdefault(line.segment, line.segment)
    where = {"segment": segment, "byte_offset": line.byte_offset}
    at = f"{segment} at byte {line.byte_offset}"
    try:
        raw = json.loads(line.data)
    except _PARSE_ERRORS as exc:  # RecursionError: nesting deeper than the parser allows
        st.add("corrupt_line", None, None, f"{at}: not JSON ({type(exc).__name__})", **where)
        return
    if not isinstance(raw, dict):
        st.add("corrupt_line", None, None, f"{at}: not a JSON object", **where)
        return
    doc, seq, h, prev = raw.get("doc_id"), raw.get("seq"), raw.get("hash"), raw.get("prev_hash")
    if not (isinstance(doc, str) and doc and isinstance(h, str) and isinstance(prev, str)):
        st.add("corrupt_line", None, None, f"{at}: no usable doc_id, seq, hash and prev_hash", **where)
        return
    if not (isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq <= INT64_MAX):
        # The document's line, but with no place in its chain (negative, past int64, not an
        # integer): one invalid_event, as the index reports it, and no seq_gap of its own.
        st.add(
            "invalid_event",
            doc,
            None,
            f"{at}: not a valid event (seq: not an integer in 0..2**63-1)",
            **where,
        )
        return
    if st.bound is not None and seq > st.bound.get(doc, -1):
        return  # written after the first scan: left to the next verify
    st.events_checked += 1

    true_hash: str | None
    try:
        true_hash = event_hash(raw)
        canonical = _canonical(raw) == line.data
    except _PARSE_ERRORS:  # NaN or Infinity (json.loads accepts them, canonical JSON does not), deep nesting
        true_hash, canonical = None, False
    if true_hash != h:
        st.add("bad_hash", doc, seq, f"{at}: the event does not hash to its stored hash", **where)
    elif not canonical:
        st.add(
            "corrupt_line", doc, seq, f"{at}: the line is not the canonical encoding of the event", **where
        )

    digest: bytes | None = None
    try:
        ev = Event.from_stored(raw)
    except _PARSE_ERRORS as exc:  # pydantic ValidationError, UnknownSchemaVersion, deep nesting
        st.add("invalid_event", doc, seq, f"{at}: not a valid event ({_why(exc)})", **where)
    else:
        if ev.run_id != st.run_id:
            st.add("invalid_event", doc, seq, f"{at}: the event belongs to run {ev.run_id!r}", **where)
        else:
            seen: set[str] = set()
            for ref in (*ev.inputs, *ev.outputs):
                if ref.sha256 in seen:
                    continue  # one event naming a blob twice (input and output) is one reference
                seen.add(ref.sha256)
                cur = st.refs.get(ref.sha256)
                if cur is None:
                    st.refs[ref.sha256] = [(doc, seq), ref.role, 1]
                else:
                    cur[0] = min(cur[0], (doc, seq))
                    cur[2] += 1
            try:
                row = _row(StoredLine(segment, line.byte_offset, raw))
                big = [c for c in _INT_COLUMNS if row[c] is not None and not _INT64[0] <= row[c] <= _INT64[1]]
                if big:  # seal_run cannot export it either (Parquet int64)
                    st.add(
                        "invalid_event",
                        doc,
                        seq,
                        f"{at}: not a valid event ({', '.join(big)}: out of int64 range)",
                        **where,
                    )
                else:
                    digest = _digest(row)
            except _PARSE_ERRORS as exc:  # a value Parquet cannot hold, deep nesting
                st.add("invalid_event", doc, seq, f"{at}: not a valid event ({_why(exc)})", **where)
    st.entries[doc].append(_Entry(seq, h, true_hash, prev, segment, line.byte_offset, digest))


# --- chains -------------------------------------------------------------------------


def _check_chains(st: _State) -> dict[str, dict[int, _Entry]]:
    """Check each document's chain; return ``doc -> seq -> entry`` of its unforked seqs."""
    out: dict[str, dict[int, _Entry]] = {}
    for doc in sorted(st.entries):
        by_seq: dict[int, list[_Entry]] = defaultdict(list)
        for e in st.entries[doc]:
            by_seq[e.seq].append(e)
        for seq in sorted(by_seq):
            es = by_seq[seq]
            if len(es) > 1:
                places = ", ".join(f"{e.segment}@{e.byte_offset}" for e in es)
                st.add("forked_chain", doc, seq, f"seq {seq} is stored {len(es)} times ({places})")
        seqs = sorted(by_seq)
        top = seqs[-1]
        for first, last in _gaps(seqs):
            what = f"seq {first}" if first == last else f"seqs {first}..{last}"
            st.add("seq_gap", doc, first, f"{what} missing although seq {top} is stored")
            st.damage[doc].append((first, last))
        for seq in sorted(by_seq):
            for e in by_seq[seq]:
                if seq == 0:
                    if e.prev_hash != GENESIS:
                        st.add(
                            "broken_link",
                            doc,
                            0,
                            f"{e.segment} at byte {e.byte_offset}: seq 0 does not start from GENESIS",
                            segment=e.segment,
                            byte_offset=e.byte_offset,
                        )
                elif seq - 1 in by_seq:
                    linked = {p.true_hash for p in by_seq[seq - 1] if p.true_hash is not None}
                    if e.prev_hash not in linked:
                        st.add(
                            "broken_link",
                            doc,
                            seq,
                            f"{e.segment} at byte {e.byte_offset}: prev_hash is not the hash of seq {seq - 1}",
                            segment=e.segment,
                            byte_offset=e.byte_offset,
                        )
        # Forked seqs are left out; a forked_chain problem marks them damaged, so only the
        # seal whose rows they touch skips this document (st.damaged in _check_columns).
        out[doc] = {seq: es[0] for seq, es in by_seq.items() if len(es) == 1}
    return out


def _gaps(seqs: list[int]) -> Iterable[tuple[int, int]]:
    """``(first, last)`` of each run of seqs missing below the highest, from sorted ``seqs``.

    Walks the stored seqs only, so one line with a huge ``seq`` costs nothing extra.
    """
    expected = 0
    for seq in seqs:
        if seq > expected:
            yield expected, seq - 1
        expected = seq + 1


# --- seals --------------------------------------------------------------------------


@dataclass
class _SealInfo:
    seal: Seal
    raw: dict[str, Any]
    heads: dict[str, int]  # doc -> seq of the head found in the log
    complete: bool  # every head found and no document damaged


def _list_columns(log: EventLog, run_id: str, st: _State) -> set[int]:
    """The numbers of the column files present."""
    cols_dir = log.run_dir(run_id) / "columns"
    numbers: set[int] = set()
    try:
        if cols_dir.is_dir():
            for p in cols_dir.iterdir():
                m = _COLUMNS_NAME_RE.fullmatch(p.name)
                if m:
                    numbers.add(int(m[1]))
    except OSError as exc:
        st.add("columns_missing", None, None, f"columns/ cannot be listed ({_oserror(exc)})")
    return numbers


def _read_seal_files(
    log: EventLog, run_id: str, st: _State
) -> tuple[set[int], dict[int, tuple[Seal, dict[str, Any]]]]:
    """The numbers of the seal files present, and the readable ones."""
    seals_dir = _seals_dir(log, run_id)
    numbers: set[int] = set()
    readable: dict[int, tuple[Seal, dict[str, Any]]] = {}
    try:
        paths = sorted(seals_dir.iterdir(), key=lambda q: q.name) if seals_dir.is_dir() else []
    except OSError as exc:
        st.add("seal_corrupt", None, None, f"seals/ cannot be listed ({_oserror(exc)})")
        paths = []
    for p in paths:
        m = _SEAL_NAME_RE.fullmatch(p.name)
        if not m:
            continue
        n = int(m[1])
        numbers.add(n)
        parsed = _read_seal(p, n, run_id, st)
        if parsed is not None:
            readable[n] = parsed
    return numbers, readable


def _check_seals(
    st: _State, seal_files: tuple[set[int], dict[int, tuple[Seal, dict[str, Any]]]], col_numbers: set[int]
) -> tuple[dict[int, _SealInfo], int]:
    """Check the seal chain and each seal against the log. Returns readable seals and the
    number of seal files (readable or not)."""
    numbers, readable = seal_files
    n_files = len(numbers)
    # Walk the numbers that exist (never range(max)): a file named 1000000000.json is cheap.
    present = sorted(numbers | col_numbers)
    missing: list[tuple[int, str]] = [(n, "its column file remains") for n in col_numbers - numbers]
    for first, last in _gaps(present):
        what = f"seal {first}" if first == last else f"seals {first}..{last}"
        missing.append((first, f"{what} missing although seal {present[-1]} exists"))
    for n, why in sorted(missing):
        text = why if why.startswith("seal") else f"seal {n} is missing although {why}"
        st.add("seal_missing", None, None, text, seal=n)

    # Each document's stored hash -> seq, to find sealed heads in the log.
    hash_seq: dict[str, dict[str, int]] = {
        doc: {e.stored_hash: e.seq for e in es} for doc, es in st.entries.items()
    }
    reported: set[str] = set()
    infos: dict[int, _SealInfo] = {}
    for n in sorted(readable):
        seal, raw = readable[n]
        if seal_hash(raw) != raw.get("hash"):
            st.add("seal_mismatch", None, None, f"seal {n} does not hash to its stored hash", seal=n)
        want = GENESIS if n == 0 else (readable[n - 1][1]["hash"] if n - 1 in readable else None)
        if want is not None and raw.get("prev_seal_hash") != want:
            st.add("seal_mismatch", None, None, f"seal {n} does not chain to seal {n - 1}", seal=n)
        if seal.document_ids != sorted(seal.doc_heads):
            st.add("seal_mismatch", None, None, f"seal {n}: document_ids do not match its doc_heads", seal=n)

        heads: dict[str, int] = {}
        for doc, h in sorted(seal.doc_heads.items()):
            if doc not in st.entries:
                if doc not in reported:
                    reported.add(doc)
                    st.add(
                        "document_missing_from_log",
                        doc,
                        None,
                        f"seal {n} lists the document; the log has no event of it",
                        seal=n,
                    )
                continue
            seq = hash_seq[doc].get(h)
            if seq is None:
                if doc not in reported:
                    reported.add(doc)
                    st.add(
                        "truncated_after_seal",
                        doc,
                        None,
                        f"seal {n} committed head {h[:16]}..., which the log no longer holds",
                        seal=n,
                    )
                continue
            heads[doc] = seq

        prev = infos.get(n - 1)
        if prev is not None:
            for doc in sorted(prev.seal.doc_heads):
                if doc not in seal.doc_heads:
                    st.add(
                        "seal_mismatch", doc, None, f"seal {n} drops a document seal {n - 1} lists", seal=n
                    )
                elif doc in heads and doc in prev.heads and heads[doc] < prev.heads[doc]:
                    st.add(
                        "seal_mismatch",
                        doc,
                        heads[doc],
                        f"seal {n} commits an earlier head than seal {n - 1}",
                        seal=n,
                    )
        complete = len(heads) == len(seal.doc_heads)
        infos[n] = _SealInfo(seal, raw, heads, complete)

    # event_count needs every head and undamaged chains; decided after all seals are read.
    for n, info in sorted(infos.items()):
        if info.complete and not any(st.damaged(d, 0, q) for d, q in info.heads.items()):
            expected = sum(seq + 1 for seq in info.heads.values())
            if info.seal.event_count != expected:
                st.add(
                    "seal_mismatch",
                    None,
                    None,
                    f"seal {n} counts {info.seal.event_count} events; its heads cover {expected}",
                    seal=n,
                )
    return infos, n_files


def _read_seal(path: Path, n: int, run_id: str, st: _State) -> tuple[Seal, dict[str, Any]] | None:
    name = f"seals/{path.name}"
    try:
        data = path.read_bytes()
    except OSError as exc:
        st.add("seal_corrupt", None, None, f"{name} cannot be read ({_oserror(exc)})", seal=n)
        return None
    try:
        raw = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
        seal = Seal.from_stored(raw)
    except _PARSE_ERRORS as exc:
        st.add("seal_corrupt", None, None, f"{name} is not a valid seal ({_why(exc)})", seal=n)
        return None
    if seal.n != n or seal.run_id != run_id:
        st.add("seal_corrupt", None, None, f"{name} holds seal {seal.n} of run {seal.run_id!r}", seal=n)
        return None
    try:
        canonical = _canonical(raw) == data
    except _PARSE_ERRORS:
        canonical = False
    if not canonical:
        st.add("seal_corrupt", None, None, f"{name} is not in canonical form", seal=n)
        return None
    return seal, raw


# --- column files -------------------------------------------------------------------


def _check_columns(
    log: EventLog,
    run_id: str,
    st: _State,
    seals: dict[int, _SealInfo],
    chains: dict[str, dict[int, _Entry]],
    closed: bool,
) -> None:
    for n, info in sorted(seals.items()):
        path = _columns_path(log, run_id, n)
        name = f"columns/events-{n}.parquet"
        try:
            mode: int | None = path.stat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            mode = None
        except OSError as exc:
            st.add("columns_mismatch", None, None, f"{name} cannot be checked ({_oserror(exc)})", seal=n)
            continue
        if mode is not None and not stat.S_ISREG(mode):
            # Not the recoverable "missing" case: seal_run cannot write the file over it.
            st.add(
                "columns_mismatch",
                None,
                None,
                f"{name} is not a regular file; remove it, then seal_run rebuilds it",
                seal=n,
            )
            continue
        if mode is None:
            latest = n == max(seals)
            # The latest seal's file is written just after the seal: a crash or a seal_run
            # still running leaves it missing for a while, and the next seal_run writes it.
            # On a closed run no later seal comes, so the detail asks for one more seal_run.
            if not latest:
                why = ""
            elif closed:
                why = (
                    " (the closing seal of a closed run: a crash or the seal still in progress; "
                    "no later seal will come, so run seal_run once more, which commits nothing new)"
                )
            else:
                why = " (the latest seal: a crash or a seal in progress, recoverable)"
            st.problems.append(
                Problem(
                    "columns_missing",
                    None,
                    None,
                    f"columns/events-{n}.parquet is missing; seal_run rebuilds it{why}",
                    seal=n,
                    fails=not latest,
                )
            )
            continue
        if n > 0 and n - 1 not in seals:
            continue  # no readable previous seal: the rows this seal added cannot be bounded
        before = seals[n - 1].heads if n > 0 else {}
        expected: dict[str, dict[int, bytes | None]] = {}
        skipped: set[str] = set()
        for doc, head in info.heads.items():
            lo = before.get(doc, -1)
            # Leave a document out only if a chain problem touches this seal's rows of it.
            if st.damaged(doc, lo + 1, head) or doc not in chains:
                skipped.add(doc)
                continue
            if n > 0 and doc in seals[n - 1].seal.doc_heads and doc not in before:
                skipped.add(doc)
                continue
            expected[doc] = {s: chains[doc][s].row_digest for s in range(lo + 1, head + 1)}
        actual = _read_columns(path, n, st)
        if actual is None:
            continue
        for doc in sorted(set(expected) | set(actual)):
            if doc in skipped or (doc not in expected and st.damaged(doc, before.get(doc, -1) + 1)):
                continue  # damage touches rows of it this seal may hold
            want, got = expected.get(doc), actual.get(doc, {})
            if want is None:
                if doc not in info.heads:
                    st.add(
                        "columns_mismatch",
                        doc,
                        min(got),
                        f"columns/events-{n}.parquet holds {len(got)} rows of a document seal {n} does not list",
                        seal=n,
                    )
                continue  # listed but its rows cannot be expected exactly (head missing)
            bad = sorted(s for s in set(want) | set(got) if want.get(s) is None or want.get(s) != got.get(s))
            if bad:
                st.add(
                    "columns_mismatch",
                    doc,
                    bad[0],
                    f"columns/events-{n}.parquet disagrees with the log on {len(bad)} row(s), first at seq {bad[0]}",
                    seal=n,
                )


def _note_unsealed(st: _State, seals: dict[int, _SealInfo], last: int) -> None:
    """On a closed run, note each document's events past every seal (never failing).

    A document's sealed head is the highest head any readable seal commits for it, so a
    seal that drops a document or goes backwards (``seal_mismatch``) adds no note. Only
    when the last seal number (seal or column file) is a readable seal: otherwise
    ``seal_corrupt`` or ``seal_missing`` already fails the run and what it committed is
    unknown. A document whose sealed head the log no longer holds is already reported
    (``truncated_after_seal``, ``document_missing_from_log``) and is not noted again.
    """
    if last not in seals:
        return
    sealed: dict[str, int] = {}
    lost: set[str] = set()
    for info in seals.values():
        for doc in info.seal.doc_heads:
            if doc in info.heads:
                sealed[doc] = max(sealed.get(doc, -1), info.heads[doc])
            else:
                lost.add(doc)
    for doc in sorted(st.entries.keys() - lost):
        head = sealed.get(doc, -1)
        above = sorted({e.seq for e in st.entries[doc] if e.seq > head})
        if above:
            n = len(above)
            st.add(
                "unsealed_events",
                doc,
                above[0],
                f"{n} event{'s' if n != 1 else ''} from seq {above[0]} after seal {last}, the last seal "
                "of this closed run; no seal commits them, so losing them would go undetected "
                "(seal_run commits them)",
                seal=last,
                fails=False,
            )


def _read_columns(path: Path, n: int, st: _State) -> dict[str, dict[int, bytes | None]] | None:
    """``doc -> seq -> row digest`` of a column file; a duplicate row maps to ``None``."""
    name = f"columns/events-{n}.parquet"
    out: dict[str, dict[int, bytes | None]] = defaultdict(dict)
    try:
        f = pq.ParquetFile(path)
        if not f.schema_arrow.equals(EVENT_SCHEMA):
            st.add("columns_mismatch", None, None, f"{name} does not have the event column schema", seal=n)
            return None
        for batch in f.iter_batches(batch_size=_BATCH):
            for row in batch.to_pylist():
                doc, seq = row["doc_id"], row["seq"]
                if not isinstance(doc, str) or not isinstance(seq, int):
                    st.add(
                        "columns_mismatch", None, None, f"{name} holds a row without doc_id or seq", seal=n
                    )
                    return None
                out[doc][seq] = None if seq in out[doc] else _digest(row)
    except (OSError, pa.ArrowException, ValueError, TypeError, KeyError) as exc:
        st.add("columns_mismatch", None, None, f"{name} cannot be read ({type(exc).__name__})", seal=n)
        return None
    return out


def _digest(row: dict[str, Any]) -> bytes:
    """Digest of a column row, after the type normalisation Parquet applies (floats)."""
    norm = dict(row)
    if norm.get("latency_ms") is not None:
        norm["latency_ms"] = float(norm["latency_ms"])
    return hashlib.sha256(canonical_json(norm)).digest()


# --- blobs --------------------------------------------------------------------------


def _check_blobs(blobs: BlobStore, st: _State) -> int:
    tombstoned = 0
    for sha in sorted(st.refs):
        (doc, seq), role, count = st.refs[sha]
        named = f"{role} blob {sha} (named by {count} event{'s' if count != 1 else ''})"
        tomb_path, content_path = blobs.tombstone_path(sha), blobs.content_path(sha)
        try:
            has_tomb, has_content = tomb_path.exists(), content_path.exists()
        except OSError as exc:  # an unreadable shard: this blob only, the report goes on
            st.add("corrupt_blob", doc, seq, f"{named}: cannot be checked ({_oserror(exc)})")
            continue
        why: str | None = None
        if has_content and not has_tomb:
            why = _blob_damage(content_path, sha)
            if why is _VANISHED:
                # Deleted between the probe and the open: judge it as it is now.
                has_content = False
                try:
                    has_tomb = tomb_path.exists()
                except OSError as exc:
                    st.add("corrupt_blob", doc, seq, f"{named}: cannot be checked ({_oserror(exc)})")
                    continue
        if has_tomb and has_content:
            st.add("tombstone_with_content", doc, seq, f"{named}: content sits next to its tombstone")
        elif has_tomb or (not has_content and _exists(tomb_path)):  # or tombstoned between the checks
            why = _tombstone_damage(tomb_path, sha)
            if why is None:
                tombstoned += 1
            else:
                st.add(
                    "corrupt_tombstone",
                    doc,
                    seq,
                    f"{named}: {why}; BlobStore.tombstone(sha, reason) rewrites the record (remove a non-empty directory there first)",
                )
        elif not has_content:
            st.add("missing_blob", doc, seq, f"{named}: no content and no tombstone")
        elif why is not None:
            st.add("corrupt_blob", doc, seq, f"{named}: {why}")
    return tombstoned


def _exists(path: Path) -> bool:
    """``path.exists()``, with an error that is not "absent" counted as present (and then
    reported by ``_tombstone_damage``, which reads it)."""
    try:
        return path.exists()
    except OSError:
        return True


def _tombstone_damage(path: Path, sha: str) -> str | None:
    """Why ``path`` is not the record ``BlobStore.tombstone`` writes, or ``None`` if it is.

    ``BlobStore.exists`` counts any file at the path as a tombstone; verify is stricter: a
    lawful deletion is a JSON object naming this hash, a non-empty reason and a time.
    """
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):  # a symlink, directory, FIFO or device: never opened (a FIFO would block)
            return "the tombstone is not a regular file"
        data = path.read_bytes()
    except OSError as exc:  # unreadable
        return f"the tombstone cannot be read ({_oserror(exc)})"
    try:
        rec = json.loads(data)
    except _PARSE_ERRORS:
        return "the tombstone is not JSON"
    if not isinstance(rec, dict):
        return "the tombstone is not a JSON object"
    if rec.get("sha256") != sha:
        return "the tombstone names another hash"
    reason, when = rec.get("reason"), rec.get("tombstoned_at")
    if not isinstance(reason, str) or not reason.strip():
        return "the tombstone has no reason"
    if not isinstance(when, str) or not when:
        return "the tombstone has no time"
    return None


# _blob_damage's answer when the content was deleted between the probe and the open.
_VANISHED = "content vanished while it was read"


def _blob_damage(path: Path, sha: str) -> str | None:
    """Why the stored content does not hold the bytes ``sha`` names, or ``None`` if it does
    (``_VANISHED`` if it was deleted before it could be opened)."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as f:
            if not _zstd_frame_whole(f):  # a frame cut in its last block still decodes in full
                return "content is not one whole zstd frame (truncated or with trailing bytes)"
            f.seek(0)
            with zstandard.ZstdDecompressor().stream_reader(f, closefd=False) as reader:
                while chunk := reader.read(_CHUNK):
                    digest.update(chunk)
    except FileNotFoundError:
        return _VANISHED
    except zstandard.ZstdError:
        return "content does not decompress"
    except OSError as exc:
        return f"content cannot be read ({_oserror(exc)})"
    if digest.hexdigest() != sha:
        return "content decompresses to bytes with another SHA-256"
    return None


# --- helpers ------------------------------------------------------------------------


def _canonical(raw: dict[str, Any]) -> bytes:
    return canonical_json(raw)


def _oserror(exc: OSError) -> str:
    return f"{type(exc).__name__}, errno {exc.errno}"


def _why(exc: BaseException) -> str:
    """Field locations and error types only: values may hold document text."""
    detail = validation_detail(exc)  # stored keys become <key>, values never appear
    return type(exc).__name__ if detail is None else detail
