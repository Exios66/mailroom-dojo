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
  seal (temp file, fsync, rename, directory fsync). Any seal whose Parquet file is missing
  (the latest, after a crash between the two writes; any other, after a deletion) or
  damaged has it rebuilt from the log by the next ``seal_run`` (temp file and rename, so
  the damaged file is replaced whole), from the rows between that seal's heads and the
  previous seal's, checked like new rows (below). The latest seal's file must read back
  as exactly those rows; an older seal's file must open with ``EVENT_SCHEMA`` and the row
  count its seal implies (a cheap footer check, so sealing cost does not grow with the
  number of seals). Damage that keeps an older file readable with the right count is
  verify's to find (``columns_mismatch``).
- Because the seal file always comes first, a column file ``events-<m>.parquet`` with no
  ``seals/<m>.json`` (``m`` above the latest seal) is never a crash state: the seal file was
  deleted. ``seal_run`` refuses (``SealRefused``) instead of sealing ``m`` again with a new
  hash, which would hide the deletion; verify reports it as ``seal_missing``. If both files
  of the latest seal are deleted nothing on disk shows the seal existed, so ``seal_run``
  seals again; only a copy of the old seal hash held elsewhere can show that deletion.
- No new events: the previous seal is returned unchanged and nothing is written (apart
  from rebuilding lost Parquet files).
- A run with no events at all gets seal 0 with no documents and ``event_count`` 0 (a
  commitment that the run was empty) and an empty Parquet file with the full schema.
- The log is read as one consistent snapshot. Segments are read one at a time, so a
  document handed from one writer to another during the read can show a ``seq`` gap (the
  handed-over event sits in a segment already read, a later one in a segment read after).
  Every document's chain must be gap-free from ``seq`` 0 to its head. A read that shows
  no gap is the snapshot. If it shows one, the log is read once more and only lines up to
  the first read's per-document heads are kept: an owner writes a document's events in
  ``seq`` order, so every event at or below a head the first read saw was on disk before
  that read ended, and the second read holds it. A gap still there is a real loss on disk:
  ``SealRefused`` ("run verify"), and nothing is written. So the log is read at most
  twice, a busy log is never refused for a race, and events the first read did not see
  go into the next seal. A head the first read saw that the second does not is refused
  too (the log lost a line while it was read).
- Every line about to go into a Parquet file (new, or rebuilt) is hashed once, on its
  stored dict (ADR-0005), and its ``prev_hash`` is checked against the stored hash of the
  event before it (``GENESIS`` for ``seq`` 0). A line that does not hash to its stored
  hash, or does not link, raises ``SealRefused`` naming the document, ``seq``, segment and
  byte offset, and nothing is written: a seal never commits over a tampered line. Lines
  covered by earlier seals are not re-hashed (verify checks them).
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

from mailroom_record.blobs import _fsync_dir, _makedirs_durable, _open_fd_for_write, write_exclusive
from mailroom_record.hashing import canonical_json
from mailroom_record.log import CorruptLine, EventLog, StoredLine, _Heads, _safe_detail
from mailroom_record.models import GENESIS, Event, Seal, event_hash, seal_hash, utc_now

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
_COLUMNS_NAME_RE = re.compile(r"events-(0|[1-9][0-9]*)\.parquet")


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
        except (ValueError, TypeError, RecursionError) as exc:  # bad JSON, failed validation, unknown version, deep nesting
            raise SealCorrupt(f"{p.name}: {_safe_detail(exc)}") from None
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


def _head_seqs(seal: Seal | None, where: dict[tuple[str, str], int], run_id: str) -> dict[str, int]:
    """``doc_id -> seq`` of each head ``seal`` committed, found in the log by its hash.

    ``where`` maps ``(doc_id, hash)`` to ``seq`` for every line of the snapshot.
    """
    if seal is None:
        return {}
    found: dict[str, int] = {}
    lost: list[str] = []
    for doc, h in seal.doc_heads.items():
        seq = where.get((doc, h))
        if seq is None:
            lost.append(doc)
        else:
            found[doc] = seq
    if lost:
        lost.sort()
        raise SealRefused(
            f"run {run_id!r}: the log no longer holds the head that seal {seal.n} committed for "
            f"document {lost[0]!r}" + (f" (and {len(lost) - 1} more)" if len(lost) > 1 else "")
            + "; run verify before sealing"
        )
    return found


def _first_gap(lines: Sequence[StoredLine]) -> tuple[str, int] | None:
    """The first ``(doc_id, seq)`` missing between 0 and its document's head, if any.

    Walks each document's sorted stored seqs, so one line with a huge ``seq`` costs nothing.
    """
    seqs: dict[str, list[int]] = {}
    for s in lines:
        seqs.setdefault(s.raw["doc_id"], []).append(s.raw["seq"])
    for doc in sorted(seqs):
        expected = 0
        for seq in sorted(seqs[doc]):
            if seq > expected:
                return doc, expected
            expected = max(expected, seq + 1)
    return None


def _index(lines: Sequence[StoredLine], run_id: str) -> _Heads:
    heads = _Heads()
    for s in lines:
        heads.add(s.raw, s)  # CorruptLine for a line without doc_id, seq or hash
    heads.check_forks(run_id)
    return heads


def _snapshot(log: EventLog, run_id: str) -> tuple[list[StoredLine], _Heads]:
    """A gap-free read of the run's stored lines (see "Rules" in the module doc).

    One read that shows no gap is a snapshot. A read that shows one may have raced a
    hand-off, so the log is read once more and only the lines up to the first read's
    heads are kept: every event at or below a head the first read saw was on disk before
    that read ended (an owner writes a document's events in ``seq`` order), so the second
    read holds all of them, and a gap still there is on disk.
    """
    lines = list(log.iter_stored(run_id))
    heads = _index(lines, run_id)
    if _first_gap(lines) is None:
        return lines, heads
    bound = dict(heads.heads)
    again = list(log.iter_stored(run_id))
    _index(again, run_id)  # a malformed or forked line anywhere is refused, not skipped
    lines = [s for s in again if s.raw["doc_id"] in bound and s.raw["seq"] <= bound[s.raw["doc_id"]][0]]
    heads = _index(lines, run_id)
    gap = _first_gap(lines)
    if gap is not None:
        doc, seq = gap
        raise SealRefused(
            f"run {run_id!r}: document {doc!r} has no event with seq {seq} on disk although later "
            "events exist; run verify before sealing"
        )
    changed = sorted(d for d, h in bound.items() if heads.heads.get(d) != h)
    if changed:  # a line seen by the first read is gone from the second
        raise SealRefused(
            f"run {run_id!r}: the head of document {changed[0]!r} at seq {bound[changed[0]][0]} "
            "disappeared from the log while it was read; run verify before sealing"
        )
    return lines, heads


def _check_links(
    export: Sequence[StoredLine], by_key: dict[tuple[str, int], StoredLine], run_id: str
) -> None:
    """Every line about to be exported hashes to its stored hash and links to its predecessor.

    Each line is hashed once, on the stored dict as parsed (ADR-0005). The predecessor's
    stored hash is what a link must equal; the predecessor itself was checked when the
    seal that covers it was made (or is checked here, if it is exported too).
    """
    for s in export:
        doc, seq, raw = s.raw["doc_id"], s.raw["seq"], s.raw
        where = f"run {run_id!r}: document {doc!r} seq {seq} ({s.segment} at byte {s.byte_offset})"
        try:
            ok = event_hash(raw) == raw["hash"]
        except (TypeError, ValueError, RecursionError):  # NaN, a non-str key, nesting too deep
            ok = False
        if not ok:
            raise SealRefused(f"{where} does not hash to its stored hash; run verify before sealing")
        if seq == 0:
            if raw.get("prev_hash") != GENESIS:
                raise SealRefused(f"{where}: prev_hash is not GENESIS; run verify before sealing")
            continue
        pred = by_key.get((doc, seq - 1))
        if pred is None or raw.get("prev_hash") != pred.raw["hash"]:
            raise SealRefused(f"{where}: prev_hash is not the hash of seq {seq - 1}; run verify before sealing")


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
    elif type(exc).__name__ == "UnknownSchemaVersion":
        what = "schema_version: UnknownSchemaVersion"
    else:  # the type only: Arrow and upgrade messages can quote the stored value
        what = type(exc).__name__
    return CorruptLine(f"{s.segment} at byte {s.byte_offset}: not a valid event ({what})")


def _row(s: StoredLine) -> dict[str, Any]:
    try:
        ev = Event.from_stored(s.raw).model_dump(mode="json")
        decision = None if ev["decision"] is None else canonical_json(ev["decision"]).decode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:  # ValidationError, UnknownSchemaVersion, nesting
        raise _invalid(s, exc) from None
    row = {c: ev[c] for c in EVENT_COLUMNS}
    row["schema_version"] = s.raw["schema_version"]
    row["decision"] = decision
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


def _write_columns(path: Path, table: pa.Table, root: Path) -> None:
    directory = path.parent
    _makedirs_durable(directory, root)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with _open_fd_for_write(fd, tmp.name) as f:
            pq.write_table(table, f, compression="zstd")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    _fsync_dir(directory)


def _columns_look_whole(path: Path, n_rows: int) -> bool:
    """A cheap check of an older seal's column file: it opens, has the schema and row count."""
    try:
        f = pq.ParquetFile(path)
        return f.metadata.num_rows == n_rows and f.schema_arrow.equals(EVENT_SCHEMA)
    except (OSError, pa.ArrowException, ValueError):  # missing, truncated, not Parquet
        return False


def _columns_hold(path: Path, lines: Sequence[StoredLine]) -> bool:
    """Whether the latest seal's column file reads back as exactly the rows of ``lines``."""
    try:
        stored = pq.read_table(path)
    except (OSError, pa.ArrowException, ValueError):
        return False
    try:
        want = _table(lines)
    except CorruptLine:
        return False  # rebuilding raises it with its segment and offset
    return stored.schema.equals(EVENT_SCHEMA) and stored.equals(want)


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


def _orphan_columns(log: EventLog, run_id: str, latest: int) -> list[int]:
    """Numbers above ``latest`` that have a column file: their seal file was deleted."""
    d = log.run_dir(run_id) / "columns"
    if not d.is_dir():
        return []
    found = (_COLUMNS_NAME_RE.fullmatch(p.name) for p in d.iterdir())
    return sorted(n for n in (int(m[1]) for m in found if m) if n > latest)


def seal_run(log: EventLog, run_id: str, *, sealed_at: str | None = None) -> Seal:
    """Seal every flushed event of ``run_id`` not covered by an earlier seal (see module doc).

    Returns the new seal, or the previous seal unchanged when there is nothing new.
    Raises ``FileNotFoundError`` for an unknown run, ``ForkedChain`` for a forked document
    chain, ``SealRefused`` when the log or the seal chain disagrees with the stored seals,
    a document's chain has a gap on disk, a line to be sealed does not hash to its stored
    hash or does not link to its predecessor, or a column file outlives its seal file;
    ``CorruptLine`` for a stored line that is not a valid event, ``SealCorrupt`` for an
    unreadable seal file, and ``ValueError`` for a bad ``sealed_at``.
    """
    sealed_at = utc_now() if sealed_at is None else sealed_at
    if not (log.run_dir(run_id) / "run.json").is_file():
        raise FileNotFoundError(f"no run {run_id!r} under {log.root}")
    seals_dir = _seals_dir(log, run_id)
    _makedirs_durable(seals_dir, log.root)
    with _seal_lock(seals_dir):
        loaded = _load_seals(log, run_id)
        _check_seal_chain(loaded)
        seals = [seal for seal, _ in loaded]
        prev = seals[-1] if seals else None
        orphans = _orphan_columns(log, run_id, -1 if prev is None else prev.n)
        if orphans:
            raise SealRefused(
                f"run {run_id!r}: columns/events-{orphans[0]}.parquet exists but seals/{orphans[0]}.json "
                "does not; a seal file was deleted (the seal is always written first); run verify"
            )

        lines, heads = _snapshot(log, run_id)
        where = {(s.raw["doc_id"], s.raw["hash"]): s.raw["seq"] for s in lines}
        by_key = {(s.raw["doc_id"], s.raw["seq"]): s for s in lines}
        prev_seqs = _head_seqs(prev, where, run_id)

        # The seal is the commit point and its Parquet file is derived: rebuild any a crash
        # (the latest) or a deletion (any) lost, from the same log the seals committed.
        rebuilt: list[tuple[Seal, pa.Table]] = []
        for s in seals:
            path = _columns_path(log, run_id, s.n)
            added = s.event_count - (seals[s.n - 1].event_count if s.n > 0 else 0)
            if s is not prev and _columns_look_whole(path, added):
                continue
            upto = prev_seqs if s is prev else _head_seqs(s, where, run_id)
            before = _head_seqs(seals[s.n - 1], where, run_id) if s.n > 0 else {}
            part = _between(lines, before, upto)
            if s is prev and _columns_hold(path, part):
                continue
            _check_links(part, by_key, run_id)
            rebuilt.append((s, _table(part)))
        for s, table in rebuilt:
            _write_columns(_columns_path(log, run_id, s.n), table, log.root)

        new = lines if prev is None else _between(lines, prev_seqs, None)
        if prev is not None and not new:
            return prev
        _check_links(new, by_key, run_id)
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
        if not write_exclusive(seals_dir / f"{seal.n}.json", canonical_json(seal.model_dump(mode="json")), log.root):
            raise SealRefused(f"run {run_id!r}: seal {seal.n} already exists; it is never replaced")
        _write_columns(_columns_path(log, run_id, seal.n), table, log.root)
        return seal
