"""The derived SQLite index: runs, documents, chain heads and the review queue (ADR-0001, ADR-0004).

The log is the only truth; this index is derived from it and disposable. ``reindex`` is the
authority: it drops everything and rebuilds from the log. ``apply`` is the incremental path
the engine uses between rebuilds; ``apply`` followed by ``reindex`` changes nothing, and an
index built by ``apply`` alone equals one built by ``reindex`` (tested as a property).

Files
-----
One SQLite file (``<root>/index.sqlite`` by convention) in WAL mode with
``synchronous=NORMAL``, plus ``<path>.lock``, which serializes rebuilds (readers never take
it). A ``meta`` table holds ``schema_version`` and ``state``. A file that is missing, empty,
not SQLite, damaged (``PRAGMA quick_check`` fails or SQLite reports corruption), of another
``schema_version`` or without ``state = complete`` is never trusted: every query and
``apply`` raises ``IndexUnavailable`` naming the status, and ``reindex`` (or ``ensure``)
rebuilds it from the log. Each ``Index`` object runs ``quick_check`` before a query or
``apply`` whenever the file (or its ``-wal``) changed since that object last checked or
wrote it, so a page damaged by anything but SQLite is caught even when the query would
not touch it; damage SQLite itself trips over is reported the same way. ``quick_check``
checks SQLite's own structure only: an index that is well-formed SQLite but holds wrong
rows (edited by hand, or left behind by a bug) passes it; ``drift(log)`` finds that, by
comparing every table with a fresh rebuild. A file SQLite cannot read is deleted (with its
``-wal`` and ``-shm``) and recreated; a readable one is rebuilt in place.

No half-built index is ever visible. A rebuild is one write transaction: drop every table,
create the current schema, fill it, write ``meta``, commit. WAL readers keep seeing the
previous committed index until the commit; a crash or kill at any point before it (an
exception, a full disk, ``SIGKILL``) rolls back to the previous index, or, if there was
none, to an empty file whose status is ``missing``. ``apply`` calls wait for a running
rebuild (SQLite's busy timeout, ``busy_timeout`` seconds) and then apply on the new index;
one that is still locked out after ``busy_timeout`` raises ``IndexUnavailable`` with status
``busy`` (nothing applied; retry), never a raw ``sqlite3.OperationalError``. ``reindex``
and ``ensure`` take the same write lock and fail the same way (nothing rebuilt). Queries do not
wait: WAL readers keep reading the last committed index during a rebuild.
The file is never renamed into place: renaming a WAL database under live connections can
pair it with another file's ``-wal``.

Tables (no table holds document text, payloads, decisions or causes)
------------------------------------------------------------------
- ``runs``: one row per run header (kind, origin, backend, created_at) and the number of
  seals the index has taken in order (``seals``, ``last_seal_hash``; ``seals`` is NULL
  when a seal file could not be read).
- ``documents``: per ``(run_id, doc_id)``, the node, kind and ``ts`` of its head event.
- ``chain_heads``: per ``(run_id, doc_id)``, the head's ``seq`` and ``hash``.
- ``doc_runs``: per ``(doc_id, run_id)``, for ``runs_for_document``.
- ``review_queue``: the documents in review (rule below), with the review event's ``seq``
  and ``ts``.
- ``problems``: what the last rebuild skipped or stopped at (kinds below).
- ``segments`` and ``doc_spans``: where the last rebuild found each document's lines
  (byte ranges per segment), how far it read each segment and a fingerprint of the bytes
  it read. Only ``reindex`` writes them; they let ``iter_events`` read one document without
  scanning the run, and let it notice when they no longer describe the log (see
  ``Index.iter_events`` for when it reads the whole run instead).

The review rule (domain-free, R4)
---------------------------------
A document is in review exactly when its head event (highest ``seq``) has ``kind ==
"review"``. A review event opens the review; the review's outcome is recorded as the
document's next event (an ``output`` naming a ``human_correction`` blob, a ``decision``,
a ``handoff``), which takes it out of the queue; a second ``review`` event keeps it in.
The index never reads node names, decision content or payloads to decide this.

Incremental rules (``apply``)
-----------------------------
The caller applies an event only after the ``LogWriter.flush()`` that made it durable
(write order: blob, event, index); ``append`` returns before that. ``apply`` keeps each
document's chain head and accepts exactly the next link:

- ``seq == head + 1`` and ``prev_hash == head hash`` (``GENESIS`` and ``seq 0`` for a new
  document): applied.
- ``seq == head`` with the head's hash, or ``seq < head``: already covered, nothing
  changes (an event applied twice, or covered by a rebuild). An older event's hash is not
  kept, so a fork below the head is not detected here; ``verify_run`` reports it.
- ``seq == head`` with another hash: ``ApplyRefused`` (``forked_chain``).
- ``seq > head + 1``: ``ApplyRefused`` (``seq_gap``); ``prev_hash`` not the head's:
  ``ApplyRefused`` (``broken_link``); a run the index has no header for:
  ``ApplyRefused`` (``unknown_run``; call ``add_run`` after ``create_run``).

A refused apply changes nothing. It means the index and the log disagree (events applied
out of order, an index that lost its last commits in a power cut, which ``synchronous =
NORMAL`` allows, or a damaged log): rebuild with ``reindex``. ``apply_seal`` follows the
same rules for the seal chain.

Rebuild rules (``reindex``)
---------------------------
Every run directory is read; an entry of ``runs/`` whose name is not a run id (``.staging``)
is not a run and is skipped. A run whose ``run.json`` cannot be read is recorded as a
``corrupt_run_header`` problem and skipped; a directory with neither ``run.json`` nor a
segment is a run being created and is skipped silently (``add_run`` registers it). Each run is scanned tolerantly
(``EventLog.scan(strict=False)``): a whole line that is not JSON (``corrupt_line``) or not a
valid event (``invalid_event``) is recorded and skipped, never fatal, and a torn tail is
counted (it is benign while a run is live; ``verify_run`` judges it). Each document's
events are then taken in ``seq`` order through the same rule as ``apply``; at the first
event it refuses (``seq_gap``, ``forked_chain``, ``broken_link``) the document's head stays
at the last good event and the problem is recorded. The same event stored twice (one ``seq``
and hash in two places) is a ``forked_chain`` too, as ``verify_run`` reports it, not a
replay. A scan that finds a gap is repeated (up to three reads), each read bounded by the
one before: a gap at a ``seq`` at or below the highest ``seq`` the previous read saw of that
document is real (the missing event was written before that read ended, so this one would
have seen it) and is recorded; a gap above it may be an event written during this read by a
writer that handed the document over, so the read is repeated, and if the last read still
shows one, the document's head stops before it with no problem recorded (the events after it
were in flight; the next rebuild or ``verify_run`` sees them). Seals are read with ``list_seals``; the index counts the
seals that are numbered from 0 and chained, and records ``seal_corrupt`` or
``seal_chain`` otherwise. Problem details name error types and field locations only,
never stored content.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import sqlite3
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mailroom_record.log import CorruptLine, EventLog, RunScan, _safe_detail
from mailroom_record.models import _RUN_ID_RE, GENESIS, Event, Run, Seal
from mailroom_record.seal import SealCorrupt, list_seals

__all__ = [
    "SCHEMA_VERSION",
    "ApplyRefused",
    "DocRow",
    "Index",
    "IndexProblem",
    "IndexStats",
    "IndexUnavailable",
    "RunRow",
]

SCHEMA_VERSION = 2  # 2: segments.fingerprint (the read path checks a segment is the one indexed)

_DDL = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, origin TEXT NOT NULL, "
    "backend TEXT NOT NULL, created_at TEXT NOT NULL, seals INTEGER, last_seal_hash TEXT)",
    "CREATE TABLE documents (run_id TEXT NOT NULL, doc_id TEXT NOT NULL, last_node TEXT NOT NULL, "
    "last_kind TEXT NOT NULL, last_ts TEXT NOT NULL, PRIMARY KEY (run_id, doc_id))",
    "CREATE TABLE chain_heads (run_id TEXT NOT NULL, doc_id TEXT NOT NULL, seq INTEGER NOT NULL, "
    "hash TEXT NOT NULL, PRIMARY KEY (run_id, doc_id))",
    "CREATE TABLE doc_runs (doc_id TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY (doc_id, run_id))",
    "CREATE TABLE review_queue (run_id TEXT NOT NULL, doc_id TEXT NOT NULL, seq INTEGER NOT NULL, "
    "since TEXT NOT NULL, PRIMARY KEY (run_id, doc_id))",
    "CREATE TABLE problems (run_id TEXT NOT NULL, doc_id TEXT, segment TEXT, byte_offset INTEGER, "
    "kind TEXT NOT NULL, detail TEXT NOT NULL)",
    "CREATE TABLE segments (run_id TEXT NOT NULL, segment TEXT NOT NULL, indexed_bytes INTEGER NOT NULL, "
    "fingerprint TEXT NOT NULL, PRIMARY KEY (run_id, segment))",
    "CREATE TABLE doc_spans (run_id TEXT NOT NULL, doc_id TEXT NOT NULL, segment TEXT NOT NULL, "
    "first_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, PRIMARY KEY (run_id, doc_id, segment))",
)

# The tables whose content is the index's meaning (``dump``); segments and doc_spans are an
# access path that only a rebuild fills, so they are left out of equality checks.
_DUMP_TABLES = ("runs", "documents", "chain_heads", "doc_runs", "review_queue", "problems")
_CORRUPT_ERRORS = {"SQLITE_CORRUPT", "SQLITE_NOTADB"}
_BUSY_ERRORS = {"SQLITE_BUSY", "SQLITE_LOCKED"}
_SCAN_TRIES = 3
_FINGERPRINT_BYTES = 4096
# Problems that mean a line the rebuild could not attribute to a document: the read path
# then reads the whole run, so the strict reader meets the line and raises.
_UNATTRIBUTED = ("corrupt_line", "invalid_event")


class IndexUnavailable(RuntimeError):
    """The index cannot answer now.

    ``status`` is ``missing``, ``corrupt``, ``old_version`` or ``incomplete`` (not a
    complete, current index: rebuild it with ``reindex``), or ``busy`` (another connection,
    usually a rebuild, held the write lock longer than ``busy_timeout``: retry; the index
    itself is fine).
    """

    def __init__(self, path: Path, status: str, detail: str = "") -> None:
        advice = "retry" if status == "busy" else "run reindex"
        super().__init__(f"index {path} is {status}{': ' + detail if detail else ''}; {advice}")
        self.path = path
        self.status = status


class ApplyRefused(ValueError):
    """``apply``/``apply_seal``/``add_run`` was refused; the index is unchanged (module doc)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind


@dataclass(frozen=True)
class RunRow:
    run_id: str
    kind: str
    origin: str
    backend: str
    created_at: str
    documents: int
    events: int
    in_review: int
    seals: int | None


@dataclass(frozen=True)
class DocRow:
    run_id: str
    doc_id: str
    events: int
    last_node: str
    last_kind: str
    head_hash: str
    in_review: bool


@dataclass(frozen=True)
class IndexProblem:
    run_id: str
    doc_id: str | None
    segment: str | None
    byte_offset: int | None
    kind: str
    detail: str


@dataclass(frozen=True)
class IndexStats:
    runs: int
    documents: int
    events: int
    seals: int
    torn_tails: int
    problems: tuple[IndexProblem, ...]


@dataclass(frozen=True)
class _Head:
    seq: int
    hash: str
    node: str
    kind: str
    ts: str


def _next_head(run_id: str, head: _Head | None, e: Event) -> _Head | None:
    """The document's head after ``e``, or ``None`` if ``e`` is already covered.

    The one transition rule ``apply`` and ``reindex`` share. Raises ``ApplyRefused``.
    """
    seq, h = (-1, GENESIS) if head is None else (head.seq, head.hash)
    where = f"document {e.doc_id!r} of run {run_id!r}"
    if e.seq <= seq:
        if e.seq == seq and e.hash != h:
            raise ApplyRefused("forked_chain", f"{where}: seq {e.seq} is already indexed with another hash")
        return None
    if e.seq != seq + 1:
        raise ApplyRefused("seq_gap", f"{where}: expected seq {seq + 1}, got {e.seq}")
    if e.prev_hash != h:
        raise ApplyRefused("broken_link", f"{where}: seq {e.seq} does not link to the indexed head")
    return _Head(e.seq, e.hash, e.node, e.kind, e.ts)


def _write_head(con: sqlite3.Connection, run_id: str, doc_id: str, head: _Head) -> None:
    con.execute(
        "INSERT INTO documents VALUES (?, ?, ?, ?, ?) ON CONFLICT (run_id, doc_id) DO UPDATE SET "
        "last_node = excluded.last_node, last_kind = excluded.last_kind, last_ts = excluded.last_ts",
        (run_id, doc_id, head.node, head.kind, head.ts),
    )
    con.execute(
        "INSERT INTO chain_heads VALUES (?, ?, ?, ?) ON CONFLICT (run_id, doc_id) DO UPDATE SET "
        "seq = excluded.seq, hash = excluded.hash",
        (run_id, doc_id, head.seq, head.hash),
    )
    con.execute("INSERT OR IGNORE INTO doc_runs VALUES (?, ?)", (doc_id, run_id))
    con.execute("DELETE FROM review_queue WHERE run_id = ? AND doc_id = ?", (run_id, doc_id))
    if head.kind == "review":
        con.execute("INSERT INTO review_queue VALUES (?, ?, ?, ?)", (run_id, doc_id, head.seq, head.ts))


def _run_header(run: Run) -> tuple[str, str, str, str]:
    return (run.kind, run.origin, run.backend, run.created_at)


class Index:
    """The rebuildable SQLite index at ``path`` (see the module doc)."""

    def __init__(self, path: Path, *, busy_timeout: float = 60.0) -> None:
        self.path = Path(path)
        self.busy_timeout = busy_timeout
        self._verified: tuple[Any, ...] | None = None  # file identity last checked or written here

    def _identity(self) -> tuple[Any, ...]:
        """Inode, size and mtime of the file and its ``-wal``: changes whenever anyone writes."""
        out: list[Any] = []
        for suffix in ("", "-wal"):
            try:
                st = os.stat(str(self.path) + suffix)
                out.append((st.st_ino, st.st_size, st.st_mtime_ns))
            except FileNotFoundError:
                out.append(None)
        return tuple(out)

    # --- connections ----------------------------------------------------------------

    def _connect(self, create: bool) -> sqlite3.Connection:
        mode = "rwc" if create else "rw"
        uri = f"file:{urllib.parse.quote(str(self.path.resolve()))}?mode={mode}"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout, isolation_level=None, check_same_thread=False)
        except sqlite3.OperationalError as exc:
            if not create and not self.path.exists():
                raise IndexUnavailable(self.path, "missing") from None
            raise self._translate(exc) from None
        try:
            con.execute("PRAGMA synchronous = NORMAL")
            con.execute("PRAGMA cell_size_check = ON")
        except sqlite3.Error as exc:
            con.close()
            raise self._translate(exc) from None
        return con

    def _translate(self, exc: sqlite3.Error) -> Exception:
        name = getattr(exc, "sqlite_errorname", None)
        if name in _CORRUPT_ERRORS:
            return IndexUnavailable(self.path, "corrupt", name or "")
        if name in _BUSY_ERRORS:
            return IndexUnavailable(self.path, "busy", f"{name} after waiting {self.busy_timeout:g}s")
        return exc

    def _check_ready(self, con: sqlite3.Connection) -> None:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "meta" not in tables:
            raise IndexUnavailable(self.path, "missing" if not tables else "old_version")
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            raise IndexUnavailable(self.path, "old_version", f"schema_version {meta.get('schema_version')!r}")
        if meta.get("state") != "complete":
            raise IndexUnavailable(self.path, "incomplete")

    @contextlib.contextmanager
    def _txn(self, write: bool) -> Iterator[sqlite3.Connection]:
        """A transaction on a ready index: ``BEGIN IMMEDIATE`` to write, a snapshot to read."""
        check = self._identity() != self._verified
        con = self._connect(create=False)
        try:
            try:
                con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                if check and con.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise IndexUnavailable(self.path, "corrupt", "quick_check failed")
                self._check_ready(con)
                yield con
                con.execute("COMMIT")
            except BaseException as exc:
                if con.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        con.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise self._translate(exc) from exc
                raise
        finally:
            con.close()
        self._verified = self._identity()

    # --- status and rebuild ---------------------------------------------------------

    def status(self) -> str:
        """``ready``, ``missing``, ``corrupt``, ``old_version``, ``incomplete`` or ``busy``. Never raises."""
        if not self.path.exists():
            return "missing"
        try:
            con = self._connect(create=False)
        except IndexUnavailable as exc:
            return exc.status
        except sqlite3.Error:
            return "corrupt"
        try:
            rows = con.execute("PRAGMA quick_check").fetchall()
            if rows != [("ok",)]:
                return "corrupt"
            self._check_ready(con)
            return "ready"
        except IndexUnavailable as exc:
            return exc.status
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorname", None) in _BUSY_ERRORS:
                return "busy"
            return "corrupt"
        finally:
            con.close()

    @contextlib.contextmanager
    def _rebuild_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_name(self.path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _remove_files(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(str(self.path) + suffix)

    def reindex(self, log: EventLog) -> IndexStats:
        """Drop everything and rebuild from the log, in one transaction (see the module doc)."""
        with self._rebuild_lock():
            if self.status() == "corrupt":
                self._remove_files()
            con = self._connect(create=True)
            try:
                con.execute("PRAGMA journal_mode = WAL")
                con.execute("BEGIN IMMEDIATE")
                try:
                    old = con.execute(
                        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view') "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC"
                    ).fetchall()
                    for kind, name in old:
                        con.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
                    stats = _build(con, log)
                    con.execute(
                        "INSERT INTO meta VALUES ('schema_version', ?), ('state', 'complete')", (str(SCHEMA_VERSION),)
                    )
                    con.execute("COMMIT")
                except BaseException:
                    if con.in_transaction:
                        with contextlib.suppress(sqlite3.Error):
                            con.execute("ROLLBACK")
                    raise
            except sqlite3.Error as exc:
                # Locked out past busy_timeout (another rebuild or an apply holds the write
                # lock): the same typed, retryable error as apply, never "database is locked".
                raise self._translate(exc) from None
            finally:
                con.close()
            return stats

    def ensure(self, log: EventLog) -> IndexStats | None:
        """Rebuild unless the index is ``ready``; returns the rebuild's stats, or ``None``."""
        if self.status() == "ready":
            return None
        return self.reindex(log)

    def drift(self, log: EventLog) -> list[str]:
        """Tables where this index differs from a fresh rebuild of ``log`` (empty: none).

        The rebuild is made in memory; nothing on disk changes.
        """
        mem = sqlite3.connect(":memory:", isolation_level=None)
        try:
            mem.execute("BEGIN")
            _build(mem, log)
            fresh = _dump(mem)
            mem.execute("COMMIT")
        finally:
            mem.close()
        mine = self.dump()
        return [t for t in _DUMP_TABLES if mine[t] != fresh[t]]

    # --- incremental ----------------------------------------------------------------

    def add_run(self, run: Run) -> None:
        """Register a run header (after ``EventLog.create_run``). Idempotent."""
        with self._txn(write=True) as con:
            row = con.execute(
                "SELECT kind, origin, backend, created_at FROM runs WHERE run_id = ?", (run.run_id,)
            ).fetchone()
            if row is None:
                con.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, 0, NULL)", (run.run_id, *_run_header(run)))
            elif tuple(row) != _run_header(run):
                raise ApplyRefused("run_mismatch", f"run {run.run_id!r} is indexed with another header")

    def apply(self, event: Event) -> None:
        """Apply one durable event (call only after the flush that made it durable)."""
        with self._txn(write=True) as con:
            if con.execute("SELECT 1 FROM runs WHERE run_id = ?", (event.run_id,)).fetchone() is None:
                raise ApplyRefused("unknown_run", f"run {event.run_id!r} is not indexed; call add_run")
            row = con.execute(
                "SELECT h.seq, h.hash, d.last_node, d.last_kind, d.last_ts FROM chain_heads h "
                "JOIN documents d USING (run_id, doc_id) WHERE h.run_id = ? AND h.doc_id = ?",
                (event.run_id, event.doc_id),
            ).fetchone()
            new = _next_head(event.run_id, None if row is None else _Head(*row), event)
            if new is not None:
                _write_head(con, event.run_id, event.doc_id, new)

    def apply_seal(self, seal: Seal) -> None:
        """Apply one stored seal: the next in the chain, or one already taken (no change)."""
        with self._txn(write=True) as con:
            row = con.execute("SELECT seals, last_seal_hash FROM runs WHERE run_id = ?", (seal.run_id,)).fetchone()
            if row is None:
                raise ApplyRefused("unknown_run", f"run {seal.run_id!r} is not indexed; call add_run")
            n, last = row
            if n is None:
                raise ApplyRefused("seal_chain", f"run {seal.run_id!r}: seals could not be read at rebuild")
            if seal.n < n:
                if seal.n == n - 1 and seal.hash != last:
                    raise ApplyRefused("seal_chain", f"seal {seal.n} of run {seal.run_id!r} differs from the indexed one")
                return
            if seal.n > n:
                raise ApplyRefused("seal_chain", f"run {seal.run_id!r}: expected seal {n}, got {seal.n}")
            if seal.prev_seal_hash != (GENESIS if last is None else last):
                raise ApplyRefused("seal_chain", f"seal {seal.n} of run {seal.run_id!r} does not chain")
            con.execute("UPDATE runs SET seals = ?, last_seal_hash = ? WHERE run_id = ?", (n + 1, seal.hash, seal.run_id))

    # --- queries --------------------------------------------------------------------

    _RUN_SELECT = (
        "SELECT r.run_id, r.kind, r.origin, r.backend, r.created_at, "
        "(SELECT COUNT(*) FROM chain_heads h WHERE h.run_id = r.run_id), "
        "(SELECT COALESCE(SUM(h.seq + 1), 0) FROM chain_heads h WHERE h.run_id = r.run_id), "
        "(SELECT COUNT(*) FROM review_queue q WHERE q.run_id = r.run_id), r.seals FROM runs r"
    )
    _DOC_SELECT = (
        "SELECT d.run_id, d.doc_id, h.seq + 1, d.last_node, d.last_kind, h.hash, q.doc_id IS NOT NULL "
        "FROM documents d JOIN chain_heads h USING (run_id, doc_id) "
        "LEFT JOIN review_queue q USING (run_id, doc_id)"
    )

    def runs(self) -> list[RunRow]:
        with self._txn(write=False) as con:
            return [RunRow(*r) for r in con.execute(self._RUN_SELECT + " ORDER BY r.run_id")]

    def documents(self, run_id: str) -> list[DocRow]:
        with self._txn(write=False) as con:
            rows = con.execute(self._DOC_SELECT + " WHERE d.run_id = ? ORDER BY d.doc_id", (run_id,))
            return [DocRow(*r[:6], bool(r[6])) for r in rows]

    def review_queue(self, run_id: str | None = None) -> list[DocRow]:
        """Documents in review (head event of kind ``review``), by run then doc id."""
        where = " WHERE q.doc_id IS NOT NULL" + ("" if run_id is None else " AND d.run_id = ?")
        with self._txn(write=False) as con:
            rows = con.execute(self._DOC_SELECT + where + " ORDER BY d.run_id, d.doc_id", () if run_id is None else (run_id,))
            return [DocRow(*r[:6], bool(r[6])) for r in rows]

    def runs_for_document(self, doc_id: str) -> list[RunRow]:
        """Every run holding ``doc_id``, ordered by ``created_at`` then ``run_id``."""
        with self._txn(write=False) as con:
            rows = con.execute(
                self._RUN_SELECT + " JOIN doc_runs x ON x.run_id = r.run_id WHERE x.doc_id = ? "
                "ORDER BY r.created_at, r.run_id",
                (doc_id,),
            )
            return [RunRow(*r) for r in rows]

    def problems(self) -> list[IndexProblem]:
        with self._txn(write=False) as con:
            rows = con.execute("SELECT * FROM problems ORDER BY run_id, segment, byte_offset, doc_id, kind")
            return [IndexProblem(*r) for r in rows]

    def dump(self) -> dict[str, list[tuple[Any, ...]]]:
        """Every row of the index's meaning tables, sorted (for equality checks)."""
        with self._txn(write=False) as con:
            return _dump(con)

    # --- reading one document through the index -------------------------------------

    def iter_events(self, log: EventLog, run_id: str, doc_id: str) -> Iterator[Event]:
        """``log.iter_events(run_id, doc_id)``, reading only where the index says to look.

        Reads the byte ranges the last rebuild found for the document, plus every byte the
        rebuild did not see (each segment past ``indexed_bytes``, and new segments), so
        events appended since the rebuild are read however far the index lags. Every event
        returned is parsed and validated from the log's bytes now, never taken from the
        index. It falls back to the full read (``log.iter_events``) whenever the index
        cannot vouch for those ranges:

        - the index is unavailable, or does not know the run;
        - the last rebuild recorded a ``corrupt_line`` or ``invalid_event`` in the run (a bad
          line names no document the index can trust, so it may be this one's: the full
          read meets it and raises, as the log's own reader does, never skipping it);
        - a segment the index read is gone, shorter, or not the file it read (its
          fingerprint, a hash of its first and last ``4096`` indexed bytes, changed);
        - the ranges read do not hold the document's indexed chain: every ``seq`` from 0 to
          the indexed head, with the head's hash at the head (a segment rewritten in place
          with its edges kept, lines moved out of the recorded ranges);
        - the first pass of a strict read of the ranges raises (a line damaged since the
          rebuild). An invalid event met in the second pass raises ``InvalidEvent``, as
          the full read does on the same line.

        What is guaranteed: on a log changed only by appends (the store's own writes), the
        result equals ``log.iter_events(run_id, doc_id)``. This is not a tamper check. A
        rewrite in place that keeps a segment's length, its fingerprinted edges and the
        document's head line can change what lies inside the recorded ranges in ways the
        checks above do not see, so the result can then differ from a full read (an event
        of the document can be missing from it). Corruption that appears after the rebuild
        in another document's lines outside those ranges is not seen here either (the full
        read would raise on it). ``verify_run`` reads every line and is the tamper check:
        it reports such damage (a ``seq_gap``, a ``broken_link``, a ``corrupt_line``).
        """
        found = self._spans_for(log, run_id, doc_id)
        scan: RunScan | None = None
        if found is not None:
            spans, head = found
            try:
                scan = log.scan(run_id, doc_id, spans=spans)
                if head is not None and not _holds_head(scan, doc_id, head):
                    scan = None
            except (CorruptLine, ValueError, OSError):
                scan = None
        if scan is None:
            yield from log.iter_events(run_id, doc_id)
            return
        for located in scan.events():
            yield located.event

    def _spans_for(
        self, log: EventLog, run_id: str, doc_id: str
    ) -> tuple[dict[str, list[tuple[int, int | None]]], tuple[int, str] | None] | None:
        """The ranges to read for ``doc_id`` and its indexed head, or ``None`` to read the whole run."""
        try:
            with self._txn(write=False) as con:
                if con.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                    return None
                bad = con.execute(
                    f"SELECT 1 FROM problems WHERE run_id = ? AND kind IN ({', '.join('?' * len(_UNATTRIBUTED))}) LIMIT 1",
                    (run_id, *_UNATTRIBUTED),
                ).fetchone()
                if bad is not None:
                    return None
                known = {
                    seg: (size, fp)
                    for seg, size, fp in con.execute(
                        "SELECT segment, indexed_bytes, fingerprint FROM segments WHERE run_id = ?", (run_id,)
                    )
                }
                ranges = con.execute(
                    "SELECT segment, first_offset, end_offset FROM doc_spans WHERE run_id = ? AND doc_id = ?",
                    (run_id, doc_id),
                ).fetchall()
                row = con.execute(
                    "SELECT seq, hash FROM chain_heads WHERE run_id = ? AND doc_id = ?", (run_id, doc_id)
                ).fetchone()
        except IndexUnavailable:
            return None
        on_disk = {p.name: p for p in log.segment_paths(run_id)}
        if set(known) - set(on_disk):
            return None
        spans: dict[str, list[tuple[int, int | None]]] = {}
        try:
            for name, path in on_disk.items():
                if name not in known:
                    spans[name] = [(0, None)]
                    continue
                indexed, fingerprint = known[name]
                size = path.stat().st_size
                if size < indexed or _fingerprint(path, indexed) != fingerprint:
                    return None
                mine: list[tuple[int, int | None]] = [(a, b) for seg, a, b in ranges if seg == name]
                if size > indexed:
                    mine.append((indexed, None))
                if mine:
                    spans[name] = mine
        except OSError:  # a segment vanished or cannot be read: let the full read report it
            return None
        return spans, (None if row is None else (row[0], row[1]))


# --- building ---------------------------------------------------------------------


def _fingerprint(path: Path, upto: int) -> str:
    """A hash of the first and last ``_FINGERPRINT_BYTES`` of ``path``'s first ``upto`` bytes.

    Segments are append-only, so these bytes never change while the file is the one the
    index read; a segment rewritten, replaced or restored from elsewhere almost always
    changes them. Reads at most ``2 * _FINGERPRINT_BYTES``.
    """
    digest = hashlib.sha256(str(upto).encode())
    with path.open("rb") as f:
        digest.update(f.read(min(upto, _FINGERPRINT_BYTES)))
        tail = max(_FINGERPRINT_BYTES, upto - _FINGERPRINT_BYTES)
        if tail < upto:
            f.seek(tail)
            digest.update(f.read(upto - tail))
    return digest.hexdigest()


def _holds_head(scan: RunScan, doc_id: str, head: tuple[int, str]) -> bool:
    """Whether ``scan`` holds every ``seq`` 0..head of ``doc_id`` and the head's hash at the head."""
    seq, h = head
    expect = 0
    for s, *_ in scan._ordered(doc_id):
        if s > expect or expect > seq:
            break
        expect = max(expect, s + 1)
    if expect <= seq:
        return False
    return h in scan.stored_hashes(doc_id, seq)


def _dump(con: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {t: sorted(tuple(r) for r in con.execute(f"SELECT * FROM {t}")) for t in _DUMP_TABLES}


def _scan_heads(scan: RunScan) -> dict[str, int]:
    """The highest ``seq`` of each document in ``scan``."""
    out: dict[str, int] = {}
    for doc in scan.doc_ids():
        for seq, *_ in scan._ordered(doc):  # ascending: the last row is the highest
            out[doc] = seq
    return out


def _stable_scan(log: EventLog, run_id: str) -> tuple[RunScan, dict[str, int] | None]:
    """A tolerant scan, re-read while it shows a gap no earlier read confirms, and its bound.

    The bound is the highest ``seq`` per document of the read before the returned one
    (``None`` when the first read showed no gap). A gap at or below it is real: the read
    before saw a later ``seq``, so the missing one was written before that read ended, and
    every later read lists and reads past it. A gap above it may be a hand-off between
    writers during the read. ``_reading.stable_scan`` keeps the same rule (a deliberate
    copy); change both together.
    """
    scan = log.scan(run_id, strict=False)
    bound: dict[str, int] | None = None
    for _ in range(_SCAN_TRIES - 1):
        gaps = scan.gaps()
        if not gaps or (bound is not None and all(g <= bound.get(d, -1) for d, g in gaps.items())):
            break
        bound = _scan_heads(scan)
        scan = log.scan(run_id, strict=False)
    return scan, bound


def _run_ids(log: EventLog) -> list[str]:
    """Every directory under ``runs/`` whose name is a run id (``.staging`` and the like are not)."""
    runs_dir = log.root / "runs"
    if not runs_dir.is_dir():
        return []
    return sorted(p.name for p in runs_dir.iterdir() if _RUN_ID_RE.fullmatch(p.name) and p.is_dir())


def _build(con: sqlite3.Connection, log: EventLog) -> IndexStats:
    """Create the schema and fill it from the log, inside the caller's transaction."""
    for ddl in _DDL:
        con.execute(ddl)
    problems: list[IndexProblem] = []
    n_runs = n_docs = n_events = n_seals = n_torn = 0
    for run_id in _run_ids(log):
        if not (log.run_dir(run_id) / "run.json").exists() and not log.segment_paths(run_id):
            continue  # being created (create_run writes run.json before any segment can exist)
        try:
            run = log.read_run(run_id)
            if run.run_id != run_id:
                raise ValueError(f"run.json names run {run.run_id!r}")
        except (OSError, ValueError, TypeError) as exc:
            problems.append(IndexProblem(run_id, None, None, None, "corrupt_run_header", _safe_detail(exc)))
            continue
        n_runs += 1

        seals: int | None = 0
        last: str | None = None
        try:
            prev = GENESIS
            for s in list_seals(log, run_id):
                if s.n != seals or s.prev_seal_hash != prev:
                    problems.append(IndexProblem(run_id, None, None, None, "seal_chain", f"seal {s.n} is not seal {seals} chained to the one before"))
                    break
                prev, last, seals = s.hash, s.hash, seals + 1
        except SealCorrupt as exc:
            problems.append(IndexProblem(run_id, None, None, None, "seal_corrupt", type(exc).__name__))
            seals, last = None, None
        n_seals += seals or 0
        con.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)", (run_id, *_run_header(run), seals, last))

        scan, bound = _stable_scan(log, run_id)
        n_torn += len(scan.torn)
        spans: dict[tuple[str, str], list[int]] = {}
        heads: dict[str, _Head] = {}
        stopped: set[str] = set()
        last_seq: dict[str, int] = {}  # the seq of each document's previous line in this scan
        for located in scan.events():
            e = located.event
            span = spans.get((e.doc_id, located.segment))
            if span is None:
                spans[(e.doc_id, located.segment)] = [located.byte_offset, located.byte_offset + located.length]
            else:
                span[0] = min(span[0], located.byte_offset)
                span[1] = max(span[1], located.byte_offset + located.length)
            if e.doc_id in stopped:
                continue
            repeated = last_seq.get(e.doc_id) == e.seq  # lines come in seq order: a repeat is a second copy
            last_seq[e.doc_id] = e.seq
            try:
                if repeated:
                    raise ApplyRefused("forked_chain", f"document {e.doc_id!r} of run {run_id!r}: seq {e.seq} is stored twice")
                new = _next_head(run_id, heads.get(e.doc_id), e)
            except ApplyRefused as exc:
                stopped.add(e.doc_id)
                head = heads.get(e.doc_id)
                missing = 0 if head is None else head.seq + 1
                if exc.kind == "seq_gap" and bound is not None and missing > bound.get(e.doc_id, -1):
                    continue  # in flight during the read, not damage (module doc)
                problems.append(IndexProblem(run_id, e.doc_id, located.segment, located.byte_offset, exc.kind, str(exc)))
                continue
            if new is not None:
                heads[e.doc_id] = new
        for bad in scan.bad:
            problems.append(IndexProblem(run_id, None, bad.segment, bad.byte_offset, bad.kind, bad.detail))
        for doc_id, head in heads.items():
            _write_head(con, run_id, doc_id, head)
        n_docs += len(heads)
        n_events += sum(h.seq + 1 for h in heads.values())
        con.executemany(
            "INSERT INTO segments VALUES (?, ?, ?, ?)",
            [
                (run_id, p.name, scan.ends.get(p.name, 0), _fingerprint(p, scan.ends.get(p.name, 0)))
                for p in scan.segments
            ],
        )
        con.executemany(
            "INSERT INTO doc_spans VALUES (?, ?, ?, ?, ?)",
            [(run_id, doc, seg, a, b) for (doc, seg), (a, b) in spans.items()],
        )
    con.executemany(
        "INSERT INTO problems VALUES (?, ?, ?, ?, ?, ?)",
        [(p.run_id, p.doc_id, p.segment, p.byte_offset, p.kind, p.detail) for p in problems],
    )
    return IndexStats(n_runs, n_docs, n_events, n_seals, n_torn, tuple(problems))

