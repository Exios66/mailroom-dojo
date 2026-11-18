"""Stage 1 gate: import every sandbox run folder, verify, reindex and write the gate report.

Usage::

    python packages/record/scripts/stage1_gate.py --runs <dir> --root <store>
        [--sandbox <sandbox checkout>] [--report <file>]

Every folder under ``--runs`` that holds a ``dataset.jsonl`` is a run folder. Each is imported
into the store at ``--root`` (created when missing; a second run of the script with the same
root finds every run ``already_imported``), audited for routing inputs, and verified; then
the index is rebuilt and the report written (default ``docs/stage1-gate-report.md``, at the
repository root). The report holds counts, hashes and run ids only: one row per run, then
these totals and checks:

- source size on disk (every file under each run folder) against store size on disk
  (every file under ``runs/`` and ``blobs/`` of the root; the index is listed apart);
  sizes are apparent file sizes (``st_size``);
- ``original`` blobs (distinct, named by the imported runs) against document-runs;
- the union of the runs' absent routing inputs (``audit_sandbox_run``);
- trace: the document drawn by the most runs, and whether ``trace_doc`` lists exactly
  those runs;
- leak scan: a 40-character string from the middle of the longest imported document text
  must be in none of the report's bytes and none of the decoded values of any Parquet file
  under the root (every cell, through ``pyarrow``; a byte grep misses compressed pages).
  The same scan is first run on a scratch Parquet file holding the string (positive
  control); if that scan does not find it, the check fails. The string itself is never
  printed, only its SHA-256;
- floor replay parity, when ``--sandbox`` names a sandbox checkout: for every run folder
  with ``nodes.jsonl`` or ``items.jsonl``, ``replay_timeline`` from the record and the
  sandbox's ``build_timeline`` on the folder give the same event sequence (node rows by
  doc, node and status; items by id; checkpoints) and the same times within 1 ms. The
  sandbox's ``tui/floor_replay.py`` is loaded by path (it imports only the standard
  library); without ``--sandbox`` the check is reported as skipped;
- import warnings: what each import skipped (a torn last line of a JSONL file), on the
  run's row, and the number of runs with any (reported, not a gate; the record keeps them
  as the run's ``import`` event);
- append throughput with ``flush_every=16`` in a scratch store (reported, not a gate).

Exit status: 0 when every import succeeded, every run verifies, the store is smaller than
the source, the trace check holds, the leak scan is clean and parity did not fail; 1
otherwise (each failed condition is listed in the report and on stderr); 2 for bad
arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from mailroom_record import BlobStore, EventLog, Index, Run, audit_sandbox_run, import_sandbox_run, replay_timeline, trace_doc, verify_run

__all__ = ["GateResult", "load_sandbox_floor_replay", "main", "parquet_holds", "run_gate"]

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REPORT = REPO_ROOT / "docs" / "stage1-gate-report.md"
LEAK_LEN = 40
TOLERANCE_S = 0.001
THROUGHPUT_EVENTS = 4000
FLOOR_DOCS_PER_MINUTE = 249


@dataclass
class RunRow:
    folder: str
    run_id: str = "-"
    status: str = "-"
    documents: int = 0
    events: int = 0
    verify_ok: bool = False
    failing: list[str] = field(default_factory=list)
    absent_fields: list[str] = field(default_factory=list)
    routing_absent: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parity: str = "skipped"
    error: str = ""


@dataclass
class GateResult:
    rows: list[RunRow]
    totals: dict[str, Any]
    failures: list[str]
    report: str

    @property
    def ok(self) -> bool:
        return not self.failures


# --- helpers ------------------------------------------------------------------------


def _files_size(*dirs: Path) -> int:
    total = 0
    for d in dirs:
        if d.is_dir():
            total += sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
    return total


def _run_folders(runs: Path) -> list[Path]:
    return sorted(p for p in runs.iterdir() if p.is_dir() and (p / "dataset.jsonl").is_file())


def _iter_strings(value: Any) -> Any:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _iter_strings(k)
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v)


def parquet_holds(path: Path, needle: str) -> bool:
    """Whether any decoded value of any cell of the Parquet file contains ``needle``."""
    table = pq.read_table(path)
    for column in table.columns:
        for value in column.to_pylist():
            if any(needle in s for s in _iter_strings(value)):
                return True
    return False


def load_sandbox_floor_replay(checkout: Path) -> ModuleType:
    """The sandbox's ``tui/floor_replay.py``, loaded by path from a checkout."""
    for rel in ("src/mailroom_sandbox/tui/floor_replay.py", "tui/floor_replay.py"):
        path = checkout / rel
        if path.is_file():
            break
    else:
        raise FileNotFoundError("no tui/floor_replay.py in the sandbox checkout")
    name = "stage1_gate_sandbox_floor_replay"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load the sandbox's floor_replay.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while the class is built
    spec.loader.exec_module(module)
    return module


def _shape(timeline: list[tuple[float, dict[str, Any]]]) -> list[tuple[float, tuple[Any, ...]]]:
    out = []
    for at, event in timeline:
        ((kind, body),) = event.items()
        if kind == "node":
            what: tuple[Any, ...] = ("node", body.get("doc"), body.get("node"), body.get("status"))
        elif kind == "item":
            what = ("item", body.get("item_id"))
        else:
            what = ("checkpoint", body.get("state"), body.get("cursor"), body.get("total"))
        out.append((float(at), what))
    return out


def _parity(log: EventLog, run_id: str, folder: Path, sandbox: ModuleType) -> str:
    """Compare the two timelines. A row without a field (the sandbox's rows are the source's
    dicts) compares as ``None``; any error other than a refusal (``ValueError``) on either
    side is a FAILED row, so one odd run never stops the gate."""
    try:
        want = sandbox.build_timeline(folder)[0]
    except ValueError:
        want = None
    except Exception as exc:  # noqa: BLE001 - reported as the run's parity, the gate goes on
        return f"FAILED (sandbox error: {type(exc).__name__})"
    try:
        got = replay_timeline(log, run_id)[0]
    except ValueError:
        got = None
    except Exception as exc:  # noqa: BLE001 - reported as the run's parity, the gate goes on
        return f"FAILED (record error: {type(exc).__name__})"
    if want is None or got is None:
        return "ok (both refuse)" if want is None and got is None else "FAILED (one side refuses)"
    try:
        a, b = _shape(got), _shape(want)
    except Exception as exc:  # noqa: BLE001 - a timeline of an unexpected shape
        return f"FAILED (timeline shape: {type(exc).__name__})"
    if [w for _, w in a] != [w for _, w in b]:
        return "FAILED (sequence differs)"
    worst = max((abs(x - y) for (x, _), (y, _) in zip(a, b, strict=True)), default=0.0)
    if worst > TOLERANCE_S:
        return f"FAILED (times differ by {worst * 1000:.1f} ms)"
    return f"ok ({len(a)} events)"


def _throughput(scratch: Path) -> dict[str, float]:
    blobs = BlobStore(scratch)
    log = EventLog(scratch, blobs)
    fields = {
        "run_id": "throughput",
        "kind": "specialist_only",
        "backend": "mock",
        "origin": "native",
        "policy_name": "throughput",
        "policy_hash": None,
        "prompt_set": "none",
        "prompt_hashes": {},
        "agent_models": {},
        "code_version": "gate",
        "label_snapshot_ids": [],
        "document_ids": [],
        "caps": {},
        "signed_by": None,
        "created_at": "2026-01-01T00:00:00Z",
        "source": {},
    }
    log.create_run(Run(**fields))
    start = time.perf_counter()
    with log.open_writer("throughput", "w0", flush_every=16) as w:
        for i in range(THROUGHPUT_EVENTS):
            w.append(doc_id=f"d{i // 20}", node=f"node{i % 20}", kind="handoff")
    seconds = time.perf_counter() - start
    per_s = THROUGHPUT_EVENTS / seconds
    return {"events_per_s": per_s, "docs_per_minute": per_s * 60 / 20}


def _commit() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _longest_text(folders: list[Path]) -> str | None:
    import json

    best: str | None = None
    for folder in folders:
        with (folder / "dataset.jsonl").open("rb") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                text = row.get("doc_text") if isinstance(row, dict) else None
                if isinstance(text, str) and len(text) >= LEAK_LEN and (best is None or len(text) > len(best)):
                    best = text
    return best


def _cell(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


# --- the gate -----------------------------------------------------------------------


def run_gate(runs: Path, root: Path, sandbox: Path | None = None, report: Path = DEFAULT_REPORT) -> GateResult:
    """Run the gate and write the report (see the module doc)."""
    root.mkdir(parents=True, exist_ok=True)
    blobs = BlobStore(root)
    log = EventLog(root, blobs)
    folders = _run_folders(runs)
    floor = load_sandbox_floor_replay(sandbox) if sandbox is not None else None
    failures: list[str] = []
    rows: list[RunRow] = []
    for folder in folders:
        row = RunRow(folder=folder.name)
        rows.append(row)
        try:
            imported = import_sandbox_run(folder, log, blobs)
        except Exception as exc:  # noqa: BLE001 - reported as a row, the gate goes on
            row.error = type(exc).__name__
            failures.append(f"{folder.name}: import failed ({row.error})")
            continue
        row.run_id, row.status = imported.run_id, imported.status
        row.documents, row.events, row.absent_fields = imported.documents, imported.events, imported.absent_fields
        row.warnings = imported.warnings
        row.routing_absent = audit_sandbox_run(folder).absent
        v = verify_run(log, blobs, imported.run_id)
        row.verify_ok = v.ok
        row.failing = sorted({p.kind for p in v.problems if p.fails})
        if not v.ok:
            failures.append(f"{row.run_id}: verify not ok ({_cell(row.failing)})")
        if floor is not None and ((folder / "nodes.jsonl").is_file() or (folder / "items.jsonl").is_file()):
            row.parity = _parity(log, imported.run_id, folder, floor)
            if row.parity.startswith("FAILED"):
                failures.append(f"{row.run_id}: floor replay parity {row.parity}")

    index = Index(root / "index.sqlite")
    stats = index.reindex(log)
    imported_rows = [r for r in rows if not r.error]

    originals: set[str] = set()
    drawn: Counter[str] = Counter()
    for r in imported_rows:
        for doc in log.read_run(r.run_id).document_ids:
            drawn[doc] += 1
        for e in log.iter_events(r.run_id):
            originals.update(ref.sha256 for ref in (*e.inputs, *e.outputs) if ref.role == "original")
    trace_line = "no document"
    if drawn:
        doc, n = max(sorted(drawn.items()), key=lambda kv: kv[1])
        want = {r.run_id for r in imported_rows if doc in log.read_run(r.run_id).document_ids}
        got = {dr.run_id for dr in trace_doc(log, index, doc).runs}
        trace_line = f"document {doc}: drawn by {n} runs, trace_doc lists {len(got)}" + (
            "" if got == want else " (MISMATCH)"
        )
        if got != want:
            failures.append(f"trace_doc of the most drawn document lists {len(got)} runs, the draws {len(want)}")

    source_size = _files_size(*folders)
    store_size = _files_size(root / "runs", root / "blobs")
    index_size = sum(p.stat().st_size for p in root.glob("index.sqlite*") if p.is_file())
    if imported_rows and store_size >= source_size:
        failures.append(f"store size {store_size} B is not below the source size {source_size} B")
    document_runs = sum(r.documents for r in imported_rows)
    routing_absent = sorted({a for r in imported_rows for a in r.routing_absent})

    with tempfile.TemporaryDirectory(prefix="stage1-gate-") as scratch:
        throughput = _throughput(Path(scratch) / "store")
        text = _longest_text(folders)
        needle = None
        control = "skipped (no document text of 40 characters)"
        parquet_hits: list[str] = []
        if text is not None:
            middle = (len(text) - LEAK_LEN) // 2
            needle = text[middle : middle + LEAK_LEN]
            probe = Path(scratch) / "control.parquet"
            pq.write_table(pa.table({"cell": [f"x{needle}x"], "list": [["a", needle]]}), probe)
            control = "found (the scan works)" if parquet_holds(probe, needle) else "NOT FOUND"
            if control != "found (the scan works)":
                failures.append("leak scan positive control did not find the string")
            parquet_files = sorted((root / "runs").glob("*/columns/*.parquet"))
            parquet_hits = [str(p.relative_to(root)) for p in parquet_files if parquet_holds(p, needle)]
            if parquet_hits:
                failures.append(f"document text found in {len(parquet_hits)} Parquet file(s)")

    lines = [
        "# Stage 1 gate report",
        "",
        f"Written by `packages/record/scripts/stage1_gate.py` at commit `{_commit()}` on "
        f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}. Counts, hashes and run ids only.",
        "",
        "| run | status | documents | events | verify | absent fields | routing inputs absent | floor replay parity "
        "| import warnings |",
        "|:---|:---|---:|---:|:---|:---|:---|:---|:---|",
    ]
    for r in rows:
        if r.error:
            lines.append(f"| {r.folder} | import failed: {r.error} | - | - | - | - | - | - | - |")
            continue
        verify = "ok" if r.verify_ok else f"NOT OK ({_cell(r.failing)})"
        lines.append(
            f"| {r.run_id} | {r.status} | {r.documents} | {r.events} | {verify} | {_cell(r.absent_fields)} "
            f"| {_cell(r.routing_absent)} | {r.parity} | {_cell(r.warnings)} |"
        )
    status_counts = Counter(r.status for r in imported_rows)
    lines += [
        "",
        "## Totals",
        "",
        f"- Run folders: {len(folders)}; imported now: {status_counts['imported']}; already imported: "
        f"{status_counts['already_imported']}; failed: {len(rows) - len(imported_rows)}.",
        f"- Verify ok: {sum(r.verify_ok for r in imported_rows)} of {len(imported_rows)}.",
        f"- Runs with import warnings: {sum(bool(r.warnings) for r in imported_rows)}.",
        f"- Source size on disk: {source_size} B; store size on disk (runs and blobs): {store_size} B "
        f"({(store_size / source_size * 100) if source_size else 0:.1f}% of the source); index: {index_size} B.",
        f"- `original` blobs: {len(originals)} distinct; document-runs: {document_runs}.",
        f"- Index rebuilt: {stats.runs} runs, {stats.events} events.",
        f"- Routing inputs absent in at least one run: {_cell(routing_absent)}.",
        f"- Trace: {trace_line}.",
        f"- Append throughput (flush_every=16, {THROUGHPUT_EVENTS} events, scratch store): "
        f"{throughput['events_per_s']:.0f} events/s, {throughput['docs_per_minute']:.0f} documents/minute at 20 "
        f"events per document (design floor {FLOOR_DOCS_PER_MINUTE}; reported, not gated).",
        "- Floor replay parity: "
        + ("checked against the sandbox's build_timeline." if floor is not None else "skipped (no --sandbox)."),
    ]
    if needle is None:
        leak_line = f"- Leak scan: {control}."
    else:
        digest = hashlib.sha256(needle.encode()).hexdigest()
        leak_line = (
            f"- Leak scan (string sha256 {digest}): positive control {control}; Parquet files with it: "
            f"{len(parquet_hits)}; report bytes: "
        )
    body = "\n".join(lines) + "\n"
    if needle is not None:
        in_report = needle.encode() in (body + leak_line).encode()
        if in_report:
            failures.append("document text found in the report")
        leak_line += "found." if in_report else "not found."
    body += leak_line + "\n"
    body += "\n## Gate\n\n" + ("All conditions hold.\n" if not failures else "".join(f"- FAILED: {f}\n" for f in failures))
    if needle is not None and needle.encode() in body.encode():  # never written, whatever happened above
        raise RuntimeError("the report would hold document text; not written")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(body, encoding="utf-8")
    totals = {
        "folders": len(folders),
        "source_size": source_size,
        "store_size": store_size,
        "index_size": index_size,
        "originals": len(originals),
        "document_runs": document_runs,
        "routing_absent": routing_absent,
        "statuses": dict(status_counts),
        "parquet_hits": parquet_hits,
        "leak_control": control,
        "throughput": throughput,
    }
    return GateResult(rows=rows, totals=totals, failures=failures, report=body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 1 gate: import, verify, reindex, report.")
    parser.add_argument("--runs", type=Path, required=True, help="folder holding sandbox run folders")
    parser.add_argument("--root", type=Path, required=True, help="the store root (created when missing)")
    parser.add_argument("--sandbox", type=Path, help="a sandbox checkout, for the floor replay parity check")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="where to write the report")
    args = parser.parse_args(argv)
    if not args.runs.is_dir():
        parser.error("--runs is not a directory")
    if args.sandbox is not None and not args.sandbox.is_dir():
        parser.error("--sandbox is not a directory")
    result = run_gate(args.runs, args.root, args.sandbox, args.report)
    statuses = ", ".join(f"{k} {v}" for k, v in sorted(result.totals["statuses"].items())) or "none"
    print(f"stage 1 gate: {result.totals['folders']} run folders ({statuses}); report written")
    for f in result.failures:
        print(f"FAILED: {f}", file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.exit(main())
