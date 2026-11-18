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
JSON object, or has no usable ``doc_id``, ``seq``, ``hash`` and ``prev_hash``, is a
``corrupt_line`` (segment and byte offset); the scan continues after it. Each event's hash is
recomputed on the stored dict as parsed, before any upgrade (ADR-0005): a mismatch is
``bad_hash``. A line whose bytes are not the canonical encoding of what it parses to (a
writer only ever writes canonical lines) is a ``corrupt_line`` too. A line that hashes but
does not validate as an event after upgrading (a bad field, a newer ``schema_version``,
another run's ``run_id``) is ``invalid_event``.

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
reported, and every other document is still compared. A seal whose predecessor is
unreadable or missing cannot bound its rows, so its column file is not compared.

Blobs. Every blob an event names is checked once. Tombstoned (tombstone, no content):
passes, counted in ``tombstoned_blobs``. Neither content nor tombstone: ``missing_blob``.
Content next to its own tombstone (a crash between writing the tombstone and unlinking
the content, or content restored afterwards): ``tombstone_with_content``. Content that does
not decompress, or decompresses to bytes with another SHA-256 (bit rot; ``BlobStore.put``
never rewrites present content, so nothing else repairs it): ``corrupt_blob``. Content is
streamed through the hash, never held whole, and never appears in a report. A blob problem
names the first event (by ``(doc_id, seq)``) that refers to it and how many do.

Torn tails, and when a run is closed
------------------------------------
A torn tail (bytes after a segment's last newline) is always reported. Whether it fails
the run depends on whether the run is still being written:

- A run is **closed** when its header's ``kind`` is an experiment kind (anything but
  ``service_period``) and it has at least one seal file. An experiment run is sealed once,
  when it ends (plan Task 6), so its seal is its end marker. A ``service_period`` run is
  sealed on a timer and never ends in this package, so it is never closed. A run whose
  header cannot be read is treated as closed if it has a seal (the safe side).
- On a run that is **not closed**, a torn tail is benign (``fails`` false): a writer may be
  mid-write, or a crash left bytes that were never a whole event; the chain continued past
  them and nothing durable was lost.
- On a **closed** run no writer will ever finish those bytes, so a torn tail is final
  damage (``fails`` true): the run carries the trace of a crash, and verify says so instead
  of passing it quietly.

Every other kind always fails the run. ``ok`` is true when no problem fails.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
import zstandard

from mailroom_record.blobs import BlobStore
from mailroom_record.hashing import canonical_json
from mailroom_record.log import EventLog, RawLine, StoredLine, TornTail, iter_raw_segment
from mailroom_record.models import GENESIS, Event, Run, Seal, event_hash, seal_hash
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


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a run. ``fails`` is false only for a torn tail on a run not closed.

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
    """The outcome of ``verify_run``. ``ok`` is true when no problem ``fails``."""

    ok: bool
    problems: list[Problem]
    events_checked: int
    documents_checked: int
    tombstoned_blobs: int
    seals_checked: int = 0
    closed: bool = False


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
    damaged: set[str] = field(default_factory=set)
    events_checked: int = 0
    names: dict[str, str] = field(default_factory=dict)  # interned segment names

    def add(self, kind: ProblemKind, doc_id: str | None, seq: int | None, detail: str, **where: Any) -> None:
        self.problems.append(Problem(kind, doc_id, seq, detail, **where))
        if doc_id is not None and kind in _CHAIN_KINDS:
            self.damaged.add(doc_id)


def verify_run(log: EventLog, blobs: BlobStore, run_id: str) -> VerifyReport:
    """Verify ``run_id``'s chains, seals, column files and blobs (see the module doc).

    Raises ``FileNotFoundError`` only if the run directory does not exist, and
    ``ValueError`` for a malformed run id. Everything found on disk is reported, not raised.
    """
    run_dir = log.run_dir(run_id)
    if not run_dir.is_dir():
        raise FileNotFoundError(f"no run {run_id!r} under {log.root}")
    st = _State(run_id)
    run = _check_header(log, run_id, st)
    _scan(log, run_id, st)
    chains = _check_chains(st)
    seals, n_seal_files = _check_seals(log, run_id, st)
    _check_columns(log, run_id, st, seals, chains)
    tombstoned = _check_blobs(blobs, st)

    closed = n_seal_files > 0 and (run is None or run.kind != "service_period")
    for t in st.torn:
        if closed:
            detail = (
                f"{t.segment} at byte {t.byte_offset}: torn bytes on a closed run; no writer will "
                "complete them, so they are damage from a crash"
            )
        else:
            detail = (
                f"{t.segment} at byte {t.byte_offset}: torn bytes at the end of a segment of a run "
                "still being written (a crash or a write in progress); skipped, not an event"
            )
        st.problems.append(
            Problem("torn_tail", None, None, detail, segment=t.segment, byte_offset=t.byte_offset, fails=closed)
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
    except (ValueError, TypeError) as exc:
        st.add("corrupt_run_header", None, None, f"run.json is not a valid run header ({_why(exc)})")
        return None
    if run.run_id != run_id:
        st.add("corrupt_run_header", None, None, f"run.json names run {run.run_id!r}")
        return None
    if _canonical(raw) != data:
        st.add("corrupt_run_header", None, None, "run.json is not in canonical form")
    return run


# --- lines --------------------------------------------------------------------------


def _scan(log: EventLog, run_id: str, st: _State) -> None:
    for seg in log.segment_paths(run_id):
        try:
            for item in iter_raw_segment(seg):
                if isinstance(item, TornTail):
                    st.torn.append(item)
                else:
                    _check_line(item, st)
        except OSError as exc:
            st.add("corrupt_line", None, None, f"{seg.name} cannot be read ({_oserror(exc)})", segment=seg.name)


def _check_line(line: RawLine, st: _State) -> None:
    segment = st.names.setdefault(line.segment, line.segment)
    where = {"segment": segment, "byte_offset": line.byte_offset}
    at = f"{segment} at byte {line.byte_offset}"
    try:
        raw = json.loads(line.data)
    except (UnicodeDecodeError, ValueError) as exc:
        st.add("corrupt_line", None, None, f"{at}: not JSON ({type(exc).__name__})", **where)
        return
    if not isinstance(raw, dict):
        st.add("corrupt_line", None, None, f"{at}: not a JSON object", **where)
        return
    doc, seq, h, prev = raw.get("doc_id"), raw.get("seq"), raw.get("hash"), raw.get("prev_hash")
    if not (
        isinstance(doc, str)
        and doc
        and isinstance(seq, int)
        and not isinstance(seq, bool)
        and seq >= 0
        and isinstance(h, str)
        and isinstance(prev, str)
    ):
        st.add("corrupt_line", None, None, f"{at}: no usable doc_id, seq, hash and prev_hash", **where)
        return
    st.events_checked += 1

    true_hash: str | None
    try:
        true_hash = event_hash(raw)
        canonical = _canonical(raw) == line.data
    except (TypeError, ValueError):  # NaN or Infinity: json.loads accepts them, canonical JSON does not
        true_hash, canonical = None, False
    if true_hash != h:
        st.add("bad_hash", doc, seq, f"{at}: the event does not hash to its stored hash", **where)
    elif not canonical:
        st.add("corrupt_line", doc, seq, f"{at}: the line is not the canonical encoding of the event", **where)

    digest: bytes | None = None
    try:
        ev = Event.from_stored(raw)
    except ValueError as exc:  # pydantic ValidationError, UnknownSchemaVersion
        st.add("invalid_event", doc, seq, f"{at}: not a valid event ({_why(exc)})", **where)
    else:
        if ev.run_id != st.run_id:
            st.add("invalid_event", doc, seq, f"{at}: the event belongs to run {ev.run_id!r}", **where)
        else:
            for ref in (*ev.inputs, *ev.outputs):
                cur = st.refs.get(ref.sha256)
                if cur is None:
                    st.refs[ref.sha256] = [(doc, seq), ref.role, 1]
                else:
                    cur[0] = min(cur[0], (doc, seq))
                    cur[2] += 1
            try:
                digest = _digest(_row(StoredLine(segment, line.byte_offset, raw)))
            except ValueError as exc:  # a value Parquet cannot hold
                st.add("invalid_event", doc, seq, f"{at}: not a valid event ({_why(exc)})", **where)
    st.entries[doc].append(_Entry(seq, h, true_hash, prev, segment, line.byte_offset, digest))


# --- chains -------------------------------------------------------------------------


def _check_chains(st: _State) -> dict[str, dict[int, _Entry]]:
    """Check each document's chain; return ``doc -> seq -> entry`` for unforked documents."""
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
        for seq in sorted(by_seq):
            for e in by_seq[seq]:
                if seq == 0:
                    if e.prev_hash != GENESIS:
                        st.add("broken_link", doc, 0, f"{e.segment} at byte {e.byte_offset}: seq 0 does not start from GENESIS",
                               segment=e.segment, byte_offset=e.byte_offset)
                elif seq - 1 in by_seq:
                    linked = {p.true_hash for p in by_seq[seq - 1] if p.true_hash is not None}
                    if e.prev_hash not in linked:
                        st.add("broken_link", doc, seq,
                               f"{e.segment} at byte {e.byte_offset}: prev_hash is not the hash of seq {seq - 1}",
                               segment=e.segment, byte_offset=e.byte_offset)
        if all(len(es) == 1 for es in by_seq.values()):
            out[doc] = {seq: es[0] for seq, es in by_seq.items()}
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


def _check_seals(log: EventLog, run_id: str, st: _State) -> tuple[dict[int, _SealInfo], int]:
    """Check the seal chain and each seal against the log. Returns readable seals and the
    number of seal files (readable or not)."""
    seals_dir = _seals_dir(log, run_id)
    numbers: set[int] = set()
    readable: dict[int, tuple[Seal, dict[str, Any]]] = {}
    if seals_dir.is_dir():
        for p in sorted(seals_dir.iterdir(), key=lambda q: q.name):
            m = _SEAL_NAME_RE.fullmatch(p.name)
            if not m:
                continue
            n = int(m[1])
            numbers.add(n)
            parsed = _read_seal(p, n, run_id, st)
            if parsed is not None:
                readable[n] = parsed
    n_files = len(numbers)
    cols_dir = log.run_dir(run_id) / "columns"
    col_numbers: set[int] = set()
    if cols_dir.is_dir():
        for p in cols_dir.iterdir():
            m = _COLUMNS_NAME_RE.fullmatch(p.name)
            if m:
                col_numbers.add(int(m[1]))
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
                    st.add("document_missing_from_log", doc, None, f"seal {n} lists the document; the log has no event of it", seal=n)
                continue
            seq = hash_seq[doc].get(h)
            if seq is None:
                if doc not in reported:
                    reported.add(doc)
                    st.add("truncated_after_seal", doc, None,
                           f"seal {n} committed head {h[:16]}..., which the log no longer holds", seal=n)
                continue
            heads[doc] = seq

        prev = infos.get(n - 1)
        if prev is not None:
            for doc in sorted(prev.seal.doc_heads):
                if doc not in seal.doc_heads:
                    st.add("seal_mismatch", doc, None, f"seal {n} drops a document seal {n - 1} lists", seal=n)
                elif doc in heads and doc in prev.heads and heads[doc] < prev.heads[doc]:
                    st.add("seal_mismatch", doc, heads[doc], f"seal {n} commits an earlier head than seal {n - 1}", seal=n)
        complete = len(heads) == len(seal.doc_heads)
        infos[n] = _SealInfo(seal, raw, heads, complete)

    # event_count needs every head and undamaged chains; decided after all seals are read.
    for n, info in sorted(infos.items()):
        if info.complete and not (set(info.heads) & st.damaged):
            expected = sum(seq + 1 for seq in info.heads.values())
            if info.seal.event_count != expected:
                st.add("seal_mismatch", None, None,
                       f"seal {n} counts {info.seal.event_count} events; its heads cover {expected}", seal=n)
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
    except (ValueError, TypeError) as exc:
        st.add("seal_corrupt", None, None, f"{name} is not a valid seal ({_why(exc)})", seal=n)
        return None
    if seal.n != n or seal.run_id != run_id:
        st.add("seal_corrupt", None, None, f"{name} holds seal {seal.n} of run {seal.run_id!r}", seal=n)
        return None
    try:
        canonical = _canonical(raw) == data
    except (TypeError, ValueError):
        canonical = False
    if not canonical:
        st.add("seal_corrupt", None, None, f"{name} is not in canonical form", seal=n)
        return None
    return seal, raw


# --- column files -------------------------------------------------------------------


def _check_columns(
    log: EventLog, run_id: str, st: _State, seals: dict[int, _SealInfo], chains: dict[str, dict[int, _Entry]]
) -> None:
    for n, info in sorted(seals.items()):
        path = _columns_path(log, run_id, n)
        if not path.is_file():
            st.add("columns_missing", None, None, f"columns/events-{n}.parquet is missing; seal_run rebuilds it", seal=n)
            continue
        if n > 0 and n - 1 not in seals:
            continue  # no readable previous seal: the rows this seal added cannot be bounded
        before = seals[n - 1].heads if n > 0 else {}
        expected: dict[str, dict[int, bytes | None]] = {}
        for doc, head in info.heads.items():
            if doc in st.damaged or doc not in chains:
                continue
            if n > 0 and doc in seals[n - 1].seal.doc_heads and doc not in before:
                continue
            lo = before.get(doc, -1)
            expected[doc] = {s: chains[doc][s].row_digest for s in range(lo + 1, head + 1)}
        actual = _read_columns(path, n, st)
        if actual is None:
            continue
        for doc in sorted(set(expected) | set(actual)):
            if doc in st.damaged:
                continue
            want, got = expected.get(doc), actual.get(doc, {})
            if want is None:
                if doc not in info.heads:
                    st.add("columns_mismatch", doc, min(got),
                           f"columns/events-{n}.parquet holds {len(got)} rows of a document seal {n} does not list", seal=n)
                continue  # listed but its rows cannot be expected exactly (head missing)
            bad = sorted(s for s in set(want) | set(got) if want.get(s) is None or want.get(s) != got.get(s))
            if bad:
                st.add("columns_mismatch", doc, bad[0],
                       f"columns/events-{n}.parquet disagrees with the log on {len(bad)} row(s), first at seq {bad[0]}",
                       seal=n)


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
                    st.add("columns_mismatch", None, None, f"{name} holds a row without doc_id or seq", seal=n)
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
        has_tomb, has_content = tomb_path.exists(), content_path.exists()
        if has_tomb and has_content:
            st.add("tombstone_with_content", doc, seq, f"{named}: content sits next to its tombstone")
        elif has_tomb:
            tombstoned += 1
        elif not has_content:
            if tomb_path.exists():  # tombstoned between the two checks
                tombstoned += 1
            else:
                st.add("missing_blob", doc, seq, f"{named}: no content and no tombstone")
        else:
            why = _blob_damage(content_path, sha)
            if why is not None:
                st.add("corrupt_blob", doc, seq, f"{named}: {why}")
    return tombstoned


def _blob_damage(path: Path, sha: str) -> str | None:
    """Why the stored content does not hold the bytes ``sha`` names, or ``None`` if it does."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as reader:
            while chunk := reader.read(_CHUNK):
                digest.update(chunk)
    except FileNotFoundError:
        return "content vanished while it was read"
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
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return "; ".join(".".join(map(str, e["loc"])) + ": " + e["type"] for e in errors())
        except (TypeError, KeyError):
            pass
    return type(exc).__name__
