"""Chained seals of a run and the Parquet column export (ADR-0002, ADR-0004).

Layout under the store root::

    runs/<run_id>/seals/<n>.json               seal n, canonical JSON, written once
    runs/<run_id>/seals/.lock                  serializes seal_run calls (flock)
    runs/<run_id>/columns/events-<n>.parquet   the events seal n added, no payload

What a seal covers
------------------
Seal ``n`` commits, for every document with at least one flushed event, the hash of the
document's head (its highest-``seq`` stored event) in ``doc_heads``; ``document_ids`` is
the sorted list of those documents. A seal covers, per document, every event with ``seq``
up to that head. The events *since the previous seal* are therefore, per document, those
whose ``seq`` is greater than the ``seq`` of the previous seal's head for that document
(or every event, for a document the previous seal did not list). The head ``seq`` is not
stored in the seal: it is found in the log by the head hash, and if the log no longer
holds that hash ``seal_run`` refuses (``SealRefused``), because sealing on top of a
log that lost a sealed event would hide the loss. Events a writer still holds in its
buffer are not on disk, so they are not sealed; once flushed they go into the next seal,
whichever writer appends them and whichever segment holds them.

``event_count`` is cumulative: the previous seal's count plus the events this seal added
(the rows of its Parquet file). With intact chains it equals the sum over documents of
``head seq + 1``; verification (Task 7) compares the two.

What verification can check from a seal: a document in ``document_ids`` with no event in
the log (``document_missing_from_log``); a document whose sealed head hash is no longer in
its chain while earlier events are (``truncated_after_seal``); ``prev_seal_hash`` links and
each seal's own hash (``seal_mismatch``).

Rules
-----
- ``seal_run`` holds an exclusive ``flock`` on ``seals/.lock`` for its whole run, so
  concurrent calls (threads or processes) produce seals ``0, 1, 2, ...`` each chained to
  the one before. The seal file is written write-once (fsynced temp file hard-linked into
  place, directory fsynced); if ``<n>.json`` already exists the call fails instead of
  replacing it.
- The seal file is the commit point; the Parquet file is derived. It is written after the
  seal (temp file, fsync, rename, directory fsync). If a crash leaves the latest seal
  without its Parquet file, the next ``seal_run`` rebuilds it from the log.
- No new events: the previous seal is returned unchanged and nothing is written.
- A run with no events at all gets seal 0 with no documents and ``event_count`` 0 (a
  commitment that the run was empty) and an empty Parquet file with the full schema.
- The log is read as one consistent snapshot. Segments are read one at a time, so a
  document handed from one writer to another during the read can show a ``seq`` gap (the
  handed-over event sits in a segment already read, a later one in a segment read after).
  Every document's chain must be gap-free from ``seq`` 0 to its head; if a read shows a
  gap, the log is read again until a read is gap-free or two reads in a row agree (at most
  ``_SNAPSHOT_TRIES`` reads). A gap that persists is a real loss on disk: ``SealRefused``
  ("run verify"), and nothing is written.
- Every row of the new Parquet file is built and validated before the seal file is
  written. A whole line that hashes but is not a valid event (unknown ``kind``, a bad
  field, a ``schema_version`` newer than this code, a value Parquet cannot hold) raises
  ``CorruptLine`` naming its segment and byte offset, and nothing is written, so a bad line
  never leaves a seal without its Parquet file.
- A forked chain (two stored events with one ``(doc_id, seq)``) raises ``ForkedChain`` and
  nothing is written. A torn tail is not a failure: its bytes are not a whole event, so
  they are not sealed (``EventLog.torn_tails`` reports them).
- A broken seal chain on disk (a gap in ``n``, a wrong ``prev_seal_hash``, a seal whose
  hash does not recompute) raises ``SealRefused``; an unreadable seal file raises
  ``SealCorrupt``. Sealing never builds on a seal chain it cannot trust. Seal hashes are
  checked on the stored dict as parsed from the file, never on the upgraded model, so a
  seal stored at an older ``schema_version`` still chains (ADR-0005).

Columns
-------
``EVENT_COLUMNS`` is every ``Event`` field except ``payload``, in model order. Rows are
the events as read through the upgrade path (``Event.from_stored``), except
``schema_version``, which is the version the line was stored in. So a row of a line stored
at an older version holds the upgraded values (for example ``tokens_cached`` null for a v0
line) next to the stored ``schema_version``; the stored form itself is only in the log. ``decision`` is its
canonical JSON string (``None`` stays null); ``inputs`` and ``outputs`` are lists of blob
SHA-256s. The Arrow schema is explicit, so a column that is null in every row keeps its
type. Rows are sorted by ``(doc_id, seq)``. No document text reaches this file: content
lives in blobs, and ``payload`` is not exported.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from mailroom_record.blobs import _fsync_dir, _makedirs_durable, write_exclusive
from mailroom_record.hashing import canonical_json
from mailroom_record.log import CorruptLine, EventLog, StoredLine, _Heads
from mailroom_record.models import GENESIS, Event, Seal, seal_hash, utc_now

__all__ = ["EVENT_COLUMNS", "SealCorrupt", "SealRefused", "list_seals", "seal_run"]

EVENT_COLUMNS: tuple[str, ...] = tuple(f for f in Event.model_fields if f != "payload")

_COLUMN_TYPES: dict[str, pa.DataType] = {
    "schema_version": pa.int64(),
    "run_id": pa.string(),
    "doc_id": pa.string(),
    "seq": pa.int64(),
    "ts": pa.string(),
    "node": pa.string(),
    "agent": pa.string(),
    "attempt": pa.int64(),
    "kind": pa.string(),
    "model": pa.string(),
    "prompt_id": pa.string(),
    "tokens_in": pa.int64(),
    "tokens_out": pa.int64(),
    "tokens_cached": pa.int64(),
    "price_basis": pa.string(),
    "latency_ms": pa.float64(),
    "decision": pa.string(),
    "cause": pa.string(),
    "inputs": pa.list_(pa.string()),
    "outputs": pa.list_(pa.string()),
    "prev_hash": pa.string(),
    "hash": pa.string(),
}
if set(_COLUMN_TYPES) != set(EVENT_COLUMNS):  # an Event field was added or removed
    raise RuntimeError(f"column types out of step with Event: {sorted(set(_COLUMN_TYPES) ^ set(EVENT_COLUMNS))}")
EVENT_SCHEMA = pa.schema([(name, _COLUMN_TYPES[name]) for name in EVENT_COLUMNS])

_SEAL_NAME_RE = re.compile(r"(0|[1-9][0-9]*)\.json")
_SNAPSHOT_TRIES = 5


class SealCorrupt(ValueError):
    """A seal file cannot be read as a seal of this run under its own number."""


class SealRefused(RuntimeError):
    """Sealing would build on a log or seal chain that disagrees with the stored seals."""


# --- paths ------------------------------------------------------------------------


def _seals_dir(log: EventLog, run_id: str) -> Path:
    return log.run_dir(run_id) / "seals"


def _columns_path(log: EventLog, run_id: str, n: int) -> Path:
    return log.run_dir(run_id) / "columns" / f"events-{n}.parquet"


# --- reading seals ----------------------------------------------------------------


def list_seals(log: EventLog, run_id: str) -> list[Seal]:
    """Every stored seal of a run, ordered by ``n``. Does not check hashes or links.

    Raises ``SealCorrupt`` for a seal file that is not a valid seal of this run whose
    ``n`` matches its file name.
    """
    return [seal for seal, _ in _load_seals(log, run_id)]


def _load_seals(log: EventLog, run_id: str) -> list[tuple[Seal, dict[str, Any]]]:
    """Every stored seal as ``(upgraded model, stored dict)``, ordered by ``n``."""
    d = _seals_dir(log, run_id)
    if not d.is_dir():
        return []
    seals: list[tuple[Seal, dict[str, Any]]] = []
    for p in d.iterdir():
        m = _SEAL_NAME_RE.fullmatch(p.name)
        if not m or not p.is_file():
            continue
        try:
            raw = json.loads(p.read_bytes())
            seal = Seal.from_stored(raw)
        except (ValueError, TypeError) as exc:  # bad JSON, failed validation, unknown version
            raise SealCorrupt(f"{p.name}: {exc}") from None
        if seal.n != int(m[1]) or seal.run_id != run_id:
            raise SealCorrupt(f"{p.name}: holds seal {seal.n} of run {seal.run_id!r}")
        seals.append((seal, raw))
    return sorted(seals, key=lambda pair: pair[0].n)


def _check_seal_chain(seals: Sequence[tuple[Seal, dict[str, Any]]]) -> None:
    """Check numbering, links and each seal's hash, hashing the stored dict (ADR-0005)."""
    prev = GENESIS
    for i, (s, raw) in enumerate(seals):
        if s.n != i:
            raise SealRefused(f"seal chain has a gap: expected seal {i}, found {s.n}")
        if raw.get("prev_seal_hash") != prev:
            raise SealRefused(f"seal {s.n} does not chain to seal {i - 1}")
        if seal_hash(raw) != raw.get("hash"):
            raise SealRefused(f"seal {s.n} does not hash to its stored hash")
        prev = raw["hash"]


def _head_seqs(seal: Seal | None, lines: Sequence[StoredLine], run_id: str) -> dict[str, int]:
    """``doc_id -> seq`` of each head ``seal`` committed, found in the log by its hash."""
    if seal is None:
        return {}
    wanted = dict(seal.doc_heads)
    found: dict[str, int] = {}
    for s in lines:
        doc = s.raw["doc_id"]
        if wanted.get(doc) == s.raw["hash"]:
            found[doc] = s.raw["seq"]
    lost = sorted(set(wanted) - set(found))
    if lost:
        raise SealRefused(
            f"run {run_id!r}: the log no longer holds the head that seal {seal.n} committed for "
            f"document {lost[0]!r}" + (f" (and {len(lost) - 1} more)" if len(lost) > 1 else "")
            + "; run verify before sealing"
        )
    return found


def _first_gap(lines: Sequence[StoredLine]) -> tuple[str, int] | None:
    """The first ``(doc_id, seq)`` missing between 0 and its document's head, if any."""
    seqs: dict[str, set[int]] = {}
    for s in lines:
        seqs.setdefault(s.raw["doc_id"], set()).add(s.raw["seq"])
    for doc in sorted(seqs):
        have = seqs[doc]
        if len(have) != max(have) + 1:
            return doc, min(set(range(max(have) + 1)) - have)
    return None


def _snapshot(log: EventLog, run_id: str) -> tuple[list[StoredLine], _Heads]:
    """A gap-free read of every stored line of the run (see "Rules" in the module doc)."""
    previous: list[tuple[str, int, str]] | None = None
    for _ in range(_SNAPSHOT_TRIES):
        lines = list(log.iter_stored(run_id))
        heads = _Heads()
        for s in lines:
            heads.add(s.raw, s)  # CorruptLine for a line without doc_id, seq or hash
        heads.check_forks(run_id)
        gap = _first_gap(lines)
        if gap is None:
            return lines, heads
        key = [(s.segment, s.byte_offset, s.raw["hash"]) for s in lines]
        if key == previous:
            break  # two reads agree: the gap is on disk, not a race with a writer
        previous = key
    doc, seq = gap
    raise SealRefused(
        f"run {run_id!r}: document {doc!r} has no event with seq {seq} on disk although later "
        "events exist; run verify before sealing"
    )


# --- writing ----------------------------------------------------------------------


@contextlib.contextmanager
def _seal_lock(seals_dir: Path) -> Iterator[None]:
    fd = os.open(seals_dir / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the flock


def _invalid(s: StoredLine, exc: Exception) -> CorruptLine:
    # Name the fields and error types only: input values may hold document text.
    errors = getattr(exc, "errors", None)
    if callable(errors):
        what = "; ".join(".".join(map(str, e["loc"])) + ": " + e["type"] for e in errors())
    else:
        what = f"{type(exc).__name__}: {exc}"
    return CorruptLine(f"{s.segment} at byte {s.byte_offset}: not a valid event ({what})")


def _row(s: StoredLine) -> dict[str, Any]:
    try:
        ev = Event.from_stored(s.raw).model_dump(mode="json")
    except ValueError as exc:  # pydantic ValidationError, UnknownSchemaVersion
        raise _invalid(s, exc) from None
    row = {c: ev[c] for c in EVENT_COLUMNS}
    row["schema_version"] = s.raw["schema_version"]
    row["decision"] = None if ev["decision"] is None else canonical_json(ev["decision"]).decode("utf-8")
    row["inputs"] = [r["sha256"] for r in ev["inputs"]]
    row["outputs"] = [r["sha256"] for r in ev["outputs"]]
    return row


def _table(lines: Sequence[StoredLine]) -> pa.Table:
    """The Parquet table of ``lines``. Raises ``CorruptLine`` for a line that is not a valid event."""
    pairs = sorted(((_row(s), s) for s in lines), key=lambda p: (p[0]["doc_id"], p[0]["seq"]))
    try:
        return pa.Table.from_pylist([r for r, _ in pairs], schema=EVENT_SCHEMA)
    except (pa.ArrowException, OverflowError, TypeError, ValueError) as whole:
        for r, s in pairs:  # find the offending line
            try:
                pa.Table.from_pylist([r], schema=EVENT_SCHEMA)
            except (pa.ArrowException, OverflowError, TypeError, ValueError) as exc:
                raise _invalid(s, exc) from None
        raise whole


def _write_columns(path: Path, table: pa.Table) -> None:
    directory = path.parent
    _makedirs_durable(directory)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            pq.write_table(table, f, compression="zstd")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    _fsync_dir(directory)


def _between(lines: Sequence[StoredLine], after: dict[str, int], upto: dict[str, int] | None) -> list[StoredLine]:
    """Lines with ``after[doc] < seq`` (``-1`` if absent) and, if given, ``seq <= upto[doc]``."""
    out = []
    for s in lines:
        doc, seq = s.raw["doc_id"], s.raw["seq"]
        if seq <= after.get(doc, -1):
            continue
        if upto is not None and (doc not in upto or seq > upto[doc]):
            continue
        out.append(s)
    return out


def seal_run(log: EventLog, run_id: str, *, sealed_at: str | None = None) -> Seal:
    """Seal every flushed event of ``run_id`` not covered by an earlier seal (see module doc).

    Returns the new seal, or the previous seal unchanged when there is nothing new.
    Raises ``FileNotFoundError`` for an unknown run, ``ForkedChain`` for a forked document
    chain, ``SealRefused`` when the log or the seal chain disagrees with the stored seals or
    a document's chain has a gap on disk, ``CorruptLine`` for a stored line that is not a
    valid event, ``SealCorrupt`` for an unreadable seal file, and ``ValueError`` for a bad
    ``sealed_at``.
    """
    sealed_at = utc_now() if sealed_at is None else sealed_at
    if not (log.run_dir(run_id) / "run.json").is_file():
        raise FileNotFoundError(f"no run {run_id!r} under {log.root}")
    seals_dir = _seals_dir(log, run_id)
    _makedirs_durable(seals_dir)
    with _seal_lock(seals_dir):
        loaded = _load_seals(log, run_id)
        _check_seal_chain(loaded)
        seals = [seal for seal, _ in loaded]
        prev = seals[-1] if seals else None

        lines, heads = _snapshot(log, run_id)
        prev_seqs = _head_seqs(prev, lines, run_id)

        if prev is not None:
            # The seal is the commit point; rebuild its Parquet file if a crash lost it.
            if not _columns_path(log, run_id, prev.n).exists():
                before = _head_seqs(seals[-2], lines, run_id) if len(seals) > 1 else {}
                _write_columns(_columns_path(log, run_id, prev.n), _table(_between(lines, before, prev_seqs)))
            new = _between(lines, prev_seqs, None)
            if not new:
                return prev
        else:
            new = lines

        table = _table(new)  # validates every new row before anything is written

        doc_heads = {d: h for d, (_, h) in sorted(heads.heads.items())}
        seal = Seal.build(
            run_id=run_id,
            n=0 if prev is None else prev.n + 1,
            sealed_at=sealed_at,
            doc_heads=doc_heads,
            document_ids=sorted(doc_heads),
            event_count=(0 if prev is None else prev.event_count) + len(new),
            prev_seal_hash=GENESIS if prev is None else prev.hash,
        )
        if not write_exclusive(seals_dir / f"{seal.n}.json", canonical_json(seal.model_dump(mode="json"))):
            raise SealRefused(f"run {run_id!r}: seal {seal.n} already exists; it is never replaced")
        _write_columns(_columns_path(log, run_id, seal.n), table)
        return seal
