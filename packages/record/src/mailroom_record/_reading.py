"""Tolerant reads behind ``version_chain``, ``trace_run``, ``trace_doc`` and the CLI (Task 9).

Nothing here raises on damaged data. Every read is materialised per document before
anything is built from it (a strict reader may yield events before it raises on a later
line), and what could not be read becomes a ``Damage`` naming the kind, document, ``seq``,
segment and byte offset where known. Only a malformed run id (``BadRunId``) or a run with no
directory (``UnknownRun``) raises: those are the caller's mistakes, not the record's.

Reading one document
--------------------
``read_document`` asks the index first (``Index.iter_events``: the byte ranges the last
rebuild recorded plus every byte written since), so a per-document read touches only that
document's lines; the lines the last rebuild skipped as corrupt or invalid in that run are
reported with it, because a skipped line carries no usable ``doc_id`` and may be one of the
document's events. When there is no usable index, when the index read raises (a corrupt or
invalid line inside the document's ranges), or when the events it returns do not form one
clean chain, the document is read again from the log with a tolerant scan
(``EventLog.scan(strict=False)``), re-read while it shows a ``seq`` gap a second read does
not confirm (a hand-off between writers during the scan). That read records every line it
skipped, so damage is always reported from the log itself, never from a stale index.

What is reported (``Damage.kind``)
---------------------------------
- ``corrupt_line``: a whole line that is not a JSON object. It cannot be attributed to a
  document (``doc_id`` is ``None``); a document that lost an event to it shows a
  ``seq_gap`` too.
- ``invalid_event``: a whole line that is JSON but not a valid event (``doc_id`` ``None``).
- ``seq_gap``, ``forked_chain`` (two events with one ``seq``; both are kept and shown),
  ``broken_link`` (``prev_hash`` is not the stored hash of an event at ``seq - 1``).
  Hashes are not recomputed here: an edited line whose stored hash was left alone is
  found by ``verify_run`` (``bad_hash``), which is the integrity tool.
- ``torn_tail``: bytes after a segment's last newline (benign while a run is written).
- ``corrupt_run_header``: ``run.json`` missing or unreadable; the run is still read.
- ``unreadable``: a segment that could not be opened or read (``OSError``), or a stored line
  nested too deep to read (``RecursionError``), which stops the read there: what was read
  before it is kept and the damage says the rest is not shown.
- ``missing_from_log``: the index lists the document in a run, but the log holds no
  readable event of it.

Details name kinds, segments, offsets, ``seq`` numbers and error types, never stored
content (``log._safe_detail`` strips the input values from validation errors).
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from mailroom_record.index import Index, IndexUnavailable
from mailroom_record.log import BadLine, EventLog, RunScan, TornTail, _safe_detail
from mailroom_record.models import GENESIS, Event, Run

__all__ = [
    "BadRunId",
    "Damage",
    "DocRead",
    "UnknownRun",
    "check_run_id",
    "require_run",
]

# The Run model's run id rule (``models.RunId``): a run id names a directory.
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_SEAL_NAME_RE = re.compile(r"(0|[1-9][0-9]*)\.json")
_SCAN_TRIES = 3
# What a read of one document through the index can raise on damaged or changing data.
_INDEX_READ_ERRORS = (ValueError, TypeError, OSError, RecursionError, IndexUnavailable, sqlite3.Error)

DamageKind = Literal[
    "corrupt_line",
    "invalid_event",
    "seq_gap",
    "forked_chain",
    "broken_link",
    "torn_tail",
    "corrupt_run_header",
    "unreadable",
    "missing_from_log",
]


class BadRunId(ValueError):
    """A run id that cannot name a run directory."""


class UnknownRun(LookupError):
    """No run directory for this run id under the store root."""


@dataclass(frozen=True)
class Damage:
    """Something a trace could not read cleanly. ``detail`` never holds stored content."""

    kind: DamageKind
    doc_id: str | None
    seq: int | None
    detail: str
    segment: str | None = None
    byte_offset: int | None = None


@dataclass(frozen=True)
class DocRead:
    """One document's events in one run (``seq`` order, a fork's events side by side)."""

    events: tuple[Event, ...]
    damage: tuple[Damage, ...]
    source: Literal["index", "log"]


def check_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."}:
        raise BadRunId(f"bad run id {run_id!r}: letters, digits, '.', '_' and '-' only, starting with a letter or digit")
    return run_id


def require_run(log: EventLog, run_id: object) -> str:
    """``run_id`` if it is well formed and has a directory; raises ``BadRunId``/``UnknownRun``."""
    rid = check_run_id(run_id)
    if not log.run_dir(rid).is_dir():
        raise UnknownRun(f"no run {rid!r} in this store")
    return rid


def run_ids(log: EventLog) -> list[str]:
    """Every run directory under the store root with a well-formed name, sorted."""
    runs_dir = log.root / "runs"
    try:
        return sorted(p.name for p in runs_dir.iterdir() if p.is_dir() and _RUN_ID_RE.fullmatch(p.name))
    except OSError:
        return []


def seal_files(log: EventLog, run_id: str) -> int:
    """How many seal files a run holds (named only, not parsed: verify checks them)."""
    try:
        return sum(1 for p in (log.run_dir(run_id) / "seals").iterdir() if _SEAL_NAME_RE.fullmatch(p.name))
    except OSError:
        return 0


def open_index(log: EventLog, index: Index | None) -> tuple[Index | None, str]:
    """The index made ready with ``ensure(log)``, or ``None`` and why the log is read instead."""
    if index is None:
        return None, "not used: no index given, read from the log"
    try:
        stats = index.ensure(log)
    except (IndexUnavailable, OSError, sqlite3.Error, ValueError, RecursionError) as exc:
        # RecursionError: a rebuild that met a stored line nested too deep (log.py lets it
        # escape a tolerant scan); the trace reads the log itself and reports that line.
        return None, f"unavailable ({type(exc).__name__}), read from the log"
    return index, "ready" if stats is None else "rebuilt from the log"


def read_header(log: EventLog, run_id: str) -> tuple[Run | None, Damage | None]:
    try:
        return log.read_run(run_id), None
    except FileNotFoundError:
        return None, Damage("corrupt_run_header", None, None, "run.json is missing")
    except (ValueError, TypeError, OSError, RecursionError) as exc:
        return None, Damage("corrupt_run_header", None, None, f"run.json cannot be read: {_safe_detail(exc)}")


def _line_damage(bad: BadLine) -> Damage:
    what = "not a JSON object" if bad.kind == "corrupt_line" else "not a valid event"
    return Damage(
        bad.kind,
        None,
        None,
        f"{bad.segment} at byte {bad.byte_offset}: {what}, skipped ({bad.detail})",
        segment=bad.segment,
        byte_offset=bad.byte_offset,
    )


def _torn_damage(t: TornTail) -> Damage:
    return Damage(
        "torn_tail",
        None,
        None,
        f"{t.segment} at byte {t.byte_offset}: torn bytes after the last whole line (a crash or a write in progress)",
        segment=t.segment,
        byte_offset=t.byte_offset,
    )


def chain_damage(doc_id: str, events: tuple[Event, ...] | list[Event]) -> list[Damage]:
    """Gaps, forks and broken links in one document's events (stored hashes, not recomputed)."""
    by_seq: dict[int, list[Event]] = defaultdict(list)
    for e in events:
        by_seq[e.seq].append(e)
    out: list[Damage] = []
    expect = 0
    for seq in sorted(by_seq):
        if seq != expect:
            missing = f"seq {expect}" if seq == expect + 1 else f"seqs {expect} to {seq - 1}"
            out.append(Damage("seq_gap", doc_id, expect, f"{missing} missing before seq {seq}"))
        group = by_seq[seq]
        if len(group) > 1:
            hashes = ", ".join(e.hash[:12] for e in group)
            out.append(Damage("forked_chain", doc_id, seq, f"seq {seq} is stored {len(group)} times ({hashes})"))
        if seq == 0:
            prev_ok: set[str] | None = {GENESIS}
        else:
            prev_ok = {e.hash for e in by_seq[seq - 1]} if seq - 1 in by_seq else None
        if prev_ok is not None:
            for e in group:
                if e.prev_hash not in prev_ok:
                    what = "GENESIS" if seq == 0 else f"the hash of seq {seq - 1}"
                    out.append(Damage("broken_link", doc_id, seq, f"seq {seq} ({e.hash[:12]}) does not link to {what}"))
        expect = seq + 1
    return out


def stable_scan(log: EventLog, run_id: str, doc_id: str | None = None) -> RunScan:
    """A tolerant scan, re-read while it shows a gap that a second read does not confirm."""
    previous: dict[str, int] | None = None
    scan = log.scan(run_id, doc_id, strict=False)
    for _ in range(_SCAN_TRIES - 1):
        if not scan.gaps() or scan.ends == previous:
            break
        previous = dict(scan.ends)
        scan = log.scan(run_id, doc_id, strict=False)
    return scan


def iter_scan_documents(scan: RunScan, damage: list[Damage]) -> Iterator[tuple[str, tuple[Event, ...]]]:
    """``(doc_id, events)`` per document of a tolerant scan, each one fully read first.

    Lines the scan skipped and torn tails are appended to ``damage`` once the scan is done
    (or stopped by an ``OSError``, also recorded), so call it to exhaustion.
    """
    current: str | None = None
    buf: list[Event] = []
    try:
        for located in scan.events():
            e = located.event
            if e.doc_id != current:
                if current is not None:
                    yield current, tuple(buf)
                current, buf = e.doc_id, []
            buf.append(e)
    except OSError as exc:
        damage.append(Damage("unreadable", None, None, f"a segment could not be read: {type(exc).__name__}"))
    except RecursionError:
        # A stored line nested deeper than the reader can follow. The scan cannot resume
        # after it, so what was read is kept and the rest of the read is reported as lost.
        where = f" (document {current!r}, after seq {buf[-1].seq})" if buf else ""
        damage.append(
            Damage(
                "unreadable",
                current,
                None,
                f"reading stopped at a line nested too deep to read{where}; events after it are not shown",
            )
        )
    if current is not None:
        yield current, tuple(buf)
    damage.extend(_line_damage(b) for b in scan.bad)
    damage.extend(_torn_damage(t) for t in scan.torn)


def _read_from_log(log: EventLog, run_id: str, doc_id: str) -> DocRead:
    damage: list[Damage] = []
    try:
        scan = stable_scan(log, run_id, doc_id)
    except OSError as exc:
        return DocRead((), (Damage("unreadable", None, None, f"a segment could not be read: {type(exc).__name__}"),), "log")
    events: tuple[Event, ...] = ()
    for _, evs in iter_scan_documents(scan, damage):
        events = evs  # the scan holds this one document only
    return DocRead(events, tuple(chain_damage(doc_id, events)) + tuple(damage), "log")


def read_document(log: EventLog, index: Index | None, run_id: str, doc_id: str) -> DocRead:
    """One document of one run, through the index when it can, from the log otherwise.

    ``index`` must already be ready (``open_index``) or ``None``. See the module doc.
    """
    if index is not None:
        try:
            events = tuple(index.iter_events(log, run_id, doc_id))  # materialised: it may raise late
        except _INDEX_READ_ERRORS:
            events = None
        if events is not None and not chain_damage(doc_id, events):
            skipped = _skipped_by_index(index, run_id)
            if skipped is not None:
                torn = tuple(_torn_damage(t) for t in _torn_tails(log, run_id))
                return DocRead(events, skipped + torn, "index")
    return _read_from_log(log, run_id, doc_id)


def _skipped_by_index(index: Index, run_id: str) -> tuple[Damage, ...] | None:
    """The run's lines the last rebuild skipped (any may be one of this document's events)."""
    try:
        problems = index.problems()
    except _INDEX_READ_ERRORS:
        return None
    return tuple(
        _line_damage(BadLine(p.segment, p.byte_offset, p.kind, p.detail))  # type: ignore[arg-type]
        for p in problems
        if p.run_id == run_id and p.kind in ("corrupt_line", "invalid_event") and p.segment is not None and p.byte_offset is not None
    )


def _torn_tails(log: EventLog, run_id: str) -> list[TornTail]:
    try:
        return log.torn_tails(run_id)
    except OSError:
        return []
