"""The ``mailroom-record`` command line (Task 9).

Commands (each takes ``--root``, the store root, and ``--json`` for machine-readable output)::

    mailroom-record verify <run>          check chains, seals, column files and blobs
    mailroom-record seal <run>            seal every flushed event not yet sealed
    mailroom-record reindex               rebuild <root>/index.sqlite from the log
    mailroom-record trace run <run>       every document of a run and its path
    mailroom-record trace doc <doc>       every run that touched a document, with versions
    mailroom-record versions <run> <doc>  a document's version chain in one run

Exit codes: 0 done (for ``verify``: the run is ok); 1 the record is not ok or an operation
was refused or failed (``verify`` found a failing problem, ``seal`` refused a damaged log,
an unexpected error); 2 a usage error (a malformed run id, an unknown run or document, a
root that is not a directory, a missing or unknown option). Errors are one line on stderr,
never a traceback.

Text output escapes every control, format, separator and surrogate character of a stored id
(``_printable``), so an id cannot start a line, recolour or reorder text; ``--json`` output
is ASCII-escaped. Output prints hashes, roles, node names, kinds, counts and the details ``verify`` and
``reindex`` report (which name segments, offsets and error types). It never prints blob
content, payloads, or the values inside a decision or cause (a trace shows their digest:
shape, field names, size, SHA-256). ``trace`` and ``versions`` make the index ready first
(``Index.ensure``: built when missing, rebuilt when damaged or old) and read from the log
when it cannot be (for example a read-only store); damaged data is shown, never raised.
``seal`` does not update the index; the next ``reindex`` (or ``ensure``) does.
"""

from __future__ import annotations

import json
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer

from mailroom_record._reading import BadRunId, Damage, UnknownRun, read_document, require_run, seal_files, open_index
from mailroom_record.blobs import BlobStore
from mailroom_record.index import ApplyRefused, Index, IndexUnavailable
from mailroom_record.log import CorruptLine, EventLog, ForkedChain
from mailroom_record.seal import SealCorrupt, SealRefused, seal_run
from mailroom_record.trace import Digest, DocSummary, Noted, RunHeader, Step, trace_doc, trace_run
from mailroom_record.verify import verify_run
from mailroom_record.versions import Version, versions_from_events

__all__ = ["INDEX_FILE", "app", "main"]

INDEX_FILE = "index.sqlite"

app = typer.Typer(
    name="mailroom-record",
    help="Verify, seal, index and trace a Mailroom run record.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
trace_app = typer.Typer(help="Trace a run or a document.", no_args_is_help=True)
app.add_typer(trace_app, name="trace")

Root = Annotated[Path, typer.Option("--root", help="The store root (holds runs/ and blobs/).")]
AsJson = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]
RunArg = Annotated[str, typer.Argument(help="The run id.")]
DocArg = Annotated[str, typer.Argument(help="The document id.")]


class _Usage(Exception):
    """A usage error: exit 2 with a one-line message."""


class _Store:
    def __init__(self, root: Path) -> None:
        if not root.is_dir():
            raise _Usage(f"no store at {root}: not a directory")
        self.root = root
        self.blobs = BlobStore(root)
        self.log = EventLog(root, self.blobs)
        self.index = Index(root / INDEX_FILE)


# Unicode categories a terminal may act on rather than show: controls (C0, C1, ESC and so
# ANSI CSI/OSC sequences, newlines), format characters (bidi overrides such as U+202E,
# zero-width characters, BOM), line and paragraph separators, and lone surrogates.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})


def _printable(text: str) -> str:
    """``text`` with every character of an unsafe category escaped as ``\\xNN``/``\\uNNNN``.

    Stored ids (document ids, node and agent names, decision field names) reach text output,
    and a hostile one could otherwise start a new line, recolour or hide text, or reorder it
    (a forged "run r1: ok" inside a failing report). Every text line goes through here;
    ``--json`` output is already ASCII-escaped by ``json.dumps``.
    """
    if all(unicodedata.category(c) not in _UNSAFE_CATEGORIES for c in text):
        return text
    out = []
    for c in text:
        if unicodedata.category(c) not in _UNSAFE_CATEGORIES:
            out.append(c)
        elif ord(c) < 0x100:
            out.append(f"\\x{ord(c):02x}")
        elif ord(c) < 0x10000:
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(f"\\U{ord(c):08x}")
    return "".join(out)


def _echo(line: str) -> None:
    """Print one line of text output, escaped (``_printable``)."""
    typer.echo(_printable(line))


def _err(message: str) -> None:
    typer.echo(_printable(message), err=True)  # one line: newlines are escaped


def _describe(exc: BaseException) -> str:
    """A one-line description of an error without stored content.

    Messages are printed only for errors this package words from hashes, ids, ``seq``
    numbers, segments and offsets. ``SealCorrupt`` and ``CorruptLine`` messages can embed a
    validation error, which names stored field names, so only where they point is printed;
    anything else is named by its type.
    """
    if isinstance(exc, SealCorrupt):
        return f"a seal file is corrupt ({str(exc).split(':', 1)[0]}); run verify"
    if isinstance(exc, CorruptLine):
        return f"CorruptLine: {str(exc).split(':', 1)[0]} is not a valid event or not JSON; run verify"
    if isinstance(exc, (SealRefused, ForkedChain, IndexUnavailable, ApplyRefused, FileNotFoundError)):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _guard(body: Callable[[], int]) -> None:
    try:
        code = body()
    except (BadRunId, UnknownRun, _Usage) as exc:
        _err(f"usage error: {exc}")
        raise typer.Exit(2) from None
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - one line, never a traceback (it may hold content)
        _err(f"error: {_describe(exc)}")
        raise typer.Exit(1) from None
    raise typer.Exit(code)


def _print_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, sort_keys=True, indent=2))


# --- text rendering ---------------------------------------------------------------


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _short(h: str | None) -> str:
    return "-" if h is None else h[:12]


def _digest_text(d: Digest) -> str:
    if d.shape == "object":
        what = f"object {{{', '.join(d.keys)}}}"
    elif d.shape == "list":
        what = f"list of {d.size}" + (f" {{{', '.join(d.keys)}}}" if d.keys else "")
    elif d.shape == "string":
        what = f"text of {d.size} bytes"
    else:
        what = d.shape
    return f"{what}, sha256 {_short(d.sha256)}"


def _noted_text(n: Noted | None) -> str:
    return "-" if n is None else f"seq {n.seq} at {n.node}: {_digest_text(n.digest)}"


def _damage_lines(damage: tuple[Damage, ...] | list[Damage], indent: str) -> list[str]:
    out = []
    for d in damage:
        where = "".join(
            [
                f" doc {d.doc_id}" if d.doc_id is not None else "",
                f" seq {d.seq}" if d.seq is not None else "",
            ]
        )
        out.append(f"{indent}{d.kind}{where}: {d.detail}")
    return out


def _header_text(h: RunHeader | None) -> str:
    if h is None:
        return "header unreadable"
    return f"{h.kind}, {h.origin}, backend {h.backend}, created {h.created_at}, {_n(h.declared_documents, 'declared document')}"


def _summary_lines(s: DocSummary, indent: str) -> list[str]:
    if s.events == 0:
        lines = [f"{indent}no readable events"]
    else:
        review = ", IN REVIEW" if s.in_review else ""
        lines = [
            f"{indent}{_n(s.events, 'event')}, head seq {s.head_seq} ({_short(s.head_hash)}), last {s.last_kind} at {s.last_node}{review}",
            f"{indent}nodes: {' > '.join(s.nodes)}",
        ]
        retried = {n: a for n, a in s.attempts.items() if a > 1}
        if retried:
            lines.append(f"{indent}attempts: " + ", ".join(f"{n} {a}" for n, a in retried.items()))
        lines.append(f"{indent}last decision: {_noted_text(s.last_decision)}")
        lines.append(f"{indent}last cause: {_noted_text(s.last_cause)}")
    if s.damage:
        lines.append(f"{indent}damage:")
        lines += _damage_lines(s.damage, indent + "  ")
    return lines


def _uses_text(uses: tuple[Any, ...]) -> str:
    return ", ".join(f"{u.role} {_short(u.sha256)}" for u in uses) or "-"


def _step_line(s: Step, indent: str) -> str:
    agent = f" ({s.agent})" if s.agent else ""
    extra = "".join(
        [
            f" decision {_digest_text(s.decision)};" if s.decision is not None else "",
            f" cause {_digest_text(s.cause)};" if s.cause is not None else "",
        ]
    )
    return (
        f"{indent}{s.seq} {s.ts} {s.node}{agent} {s.kind} attempt {s.attempt};{extra} "
        f"in: {_uses_text(s.inputs)}; out: {_uses_text(s.outputs)}"
    )


def _version_line(v: Version, indent: str) -> str:
    return (
        f"{indent}{v.order} {v.role} {v.sha256} {v.size} bytes, first {v.used_as} of {v.node} "
        f"attempt {v.attempt} (seq {v.seq}, {v.ts}): {v.state}"
    )


# --- commands ---------------------------------------------------------------------


@app.command()
def verify(run_id: RunArg, root: Root, as_json: AsJson = False) -> None:
    """Verify a run's chains, seals, column files and blobs. Exit 1 when it is not ok."""

    def body() -> int:
        s = _Store(root)
        rid = require_run(s.log, run_id)
        report = verify_run(s.log, s.blobs, rid)
        if as_json:
            _print_json({"run_id": rid, **asdict(report)})
        else:
            failing = sum(p.fails for p in report.problems)
            state = "ok" if report.ok else f"NOT OK ({failing} failing problem{'s' if failing != 1 else ''})"
            _echo(f"run {rid}: {state}")
            _echo(
                f"checked {report.events_checked} events, {report.documents_checked} documents, "
                f"{report.seals_checked} seals; {report.tombstoned_blobs} tombstoned blobs; "
                f"{'closed' if report.closed else 'open'} run"
            )
            for p in report.problems:
                where = "".join(
                    [
                        f" doc {p.doc_id}" if p.doc_id is not None else "",
                        f" seq {p.seq}" if p.seq is not None else "",
                        f" seal {p.seal}" if p.seal is not None else "",
                    ]
                )
                note = "" if p.fails else " (does not fail)"
                _echo(f"  {p.kind}{note}{where}: {p.detail}")
        return 0 if report.ok else 1

    _guard(body)


@app.command()
def seal(run_id: RunArg, root: Root, as_json: AsJson = False) -> None:
    """Seal every flushed event of a run not yet sealed (refused on a damaged log: exit 1)."""

    def body() -> int:
        s = _Store(root)
        rid = require_run(s.log, run_id)
        before = seal_files(s.log, rid)
        sealed = seal_run(s.log, rid)
        new = sealed.n >= before
        if as_json:
            _print_json({"run_id": rid, "new": new, "seal": sealed.model_dump(mode="json")})
        elif new:
            _echo(
                f"sealed run {rid}: seal {sealed.n}, {sealed.event_count} events in total, "
                f"{len(sealed.document_ids)} documents, hash {sealed.hash}"
            )
        else:
            _echo(f"run {rid}: no new events; the latest seal is {sealed.n} (hash {sealed.hash})")
        return 0

    _guard(body)


@app.command()
def reindex(root: Root, as_json: AsJson = False) -> None:
    """Rebuild <root>/index.sqlite from the log (problems in the log are listed, not fatal)."""

    def body() -> int:
        s = _Store(root)
        stats = s.index.reindex(s.log)
        if as_json:
            _print_json(asdict(stats))
            return 0
        _echo(
            f"reindexed {s.index.path}: {stats.runs} runs, {stats.documents} documents, {stats.events} events, "
            f"{stats.seals} seals, {stats.torn_tails} torn tails, {len(stats.problems)} problems"
        )
        for p in stats.problems:
            where = "".join(
                [
                    f" doc {p.doc_id}" if p.doc_id is not None else "",
                    f" segment {p.segment}" if p.segment is not None else "",
                    f" byte {p.byte_offset}" if p.byte_offset is not None else "",
                ]
            )
            _echo(f"  {p.kind} run {p.run_id}{where}: {p.detail}")
        return 0

    _guard(body)


@trace_app.command("run")
def trace_run_cmd(run_id: RunArg, root: Root, as_json: AsJson = False) -> None:
    """Every document of a run: nodes visited, attempts, last decision and cause, review."""

    def body() -> int:
        s = _Store(root)
        t = trace_run(s.log, s.index, run_id)
        if as_json:
            _print_json({**asdict(t), "damaged": t.damaged})
            return 0
        _echo(f"run {t.run_id}: {_header_text(t.header)}")
        _echo(
            f"{_n(len(t.documents), 'document')}, {_n(t.events, 'event')}, {t.in_review} in review, "
            f"{_n(t.seal_files, 'seal file')}; index {t.index}"
        )
        for d in t.documents:
            _echo(f"document {d.doc_id}")
            for line in _summary_lines(d, "  "):
                _echo(line)
        if t.damage:
            _echo("run damage:")
            for line in _damage_lines(t.damage, "  "):
                _echo(line)
        return 0

    _guard(body)


@trace_app.command("doc")
def trace_doc_cmd(doc_id: DocArg, root: Root, as_json: AsJson = False) -> None:
    """Every run that touched a document, with its steps and version chain in each."""

    def body() -> int:
        s = _Store(root)
        t = trace_doc(s.log, s.index, doc_id)
        if not t.runs:
            raise _Usage(f"no run in this store holds document {doc_id!r}")
        if as_json:
            _print_json({**asdict(t), "damaged": t.damaged})
            return 0
        _echo(f"document {t.doc_id}: {_n(len(t.runs), 'run')}; index {t.index}")
        for r in t.runs:
            _echo(f"run {r.run_id}: {_header_text(r.header)} (read from the {r.source})")
            for line in _summary_lines(r.summary, "  "):
                _echo(line)
            _echo("  steps:")
            for st in r.steps:
                _echo(_step_line(st, "    "))
            _echo(f"  versions ({len(r.versions)}):")
            for v in r.versions:
                _echo(_version_line(v, "    "))
            if r.damage:
                _echo("  run damage:")
                for line in _damage_lines(r.damage, "    "):
                    _echo(line)
        return 0

    _guard(body)


@app.command()
def versions(run_id: RunArg, doc_id: DocArg, root: Root, as_json: AsJson = False) -> None:
    """A document's version chain in one run: role, hash, size, node, attempt, state."""

    def body() -> int:
        s = _Store(root)
        rid = require_run(s.log, run_id)
        usable, note = open_index(s.log, s.index)
        read = read_document(s.log, usable, rid, doc_id)
        if not read.events and not any(d.doc_id == doc_id for d in read.damage):
            raise _Usage(f"run {rid!r} holds no readable event of document {doc_id!r}")
        chain = versions_from_events(read.events, s.blobs)
        if as_json:
            _print_json(
                {
                    "run_id": rid,
                    "doc_id": doc_id,
                    "events": len(read.events),
                    "versions": [asdict(v) for v in chain],
                    "damage": [asdict(d) for d in read.damage],
                    "source": read.source,
                    "index": note,
                }
            )
            return 0
        _echo(
            f"run {rid}, document {doc_id}: {_n(len(chain), 'version')} over {_n(len(read.events), 'event')} "
            f"(read from the {read.source})"
        )
        for v in chain:
            _echo(_version_line(v, "  "))
        if read.damage:
            _echo("damage:")
            for line in _damage_lines(read.damage, "  "):
                _echo(line)
        return 0

    _guard(body)


def main(argv: list[str] | None = None) -> None:
    """Run the CLI (``python -m mailroom_record``)."""
    app(args=argv if argv is not None else sys.argv[1:], prog_name="mailroom-record")
