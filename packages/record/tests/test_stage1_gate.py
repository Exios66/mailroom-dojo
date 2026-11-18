"""Task 12: the stage 1 gate script, run on synthetic run folders (the real runs are not here)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from record_testkit import SENTINEL, build_runs_dir, flip_byte, load_repo_script


@pytest.fixture
def gate() -> ModuleType:
    return load_repo_script("scripts/stage1_gate.py", "stage1_gate_under_test")


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    return build_runs_dir(tmp_path / "runs")


def _args(runs: Path, root: Path, report: Path, *extra: str) -> list[str]:
    return ["--runs", str(runs), "--root", str(root), "--report", str(report), *extra]


def test_gate_on_synthetic_runs_then_again(gate: ModuleType, runs_dir: Path, tmp_path: Path) -> None:
    report = tmp_path / "out" / "report.md"
    root = tmp_path / "store"
    assert gate.main(_args(runs_dir, root, report)) == 0
    text = report.read_text()
    assert text.count("| ar-gate-") == 3 and "imported now: 3" in text and "Verify ok: 3 of 3" in text
    assert "`original` blobs: 3 distinct; document-runs: 9" in text  # three runs, one draw
    assert "drawn by 3 runs, trace_doc lists 3." in text
    assert "positive control found (the scan works); Parquet files with it: 0; report bytes: not found." in text
    assert "parity" in text and "skipped (no --sandbox)" in text
    assert SENTINEL not in text and str(tmp_path) not in text
    assert "## Gate\n\nAll conditions hold." in text

    assert gate.main(_args(runs_dir, root, report)) == 0
    again = report.read_text()
    assert "imported now: 0; already imported: 3" in again and again.count("| already_imported |") == 3


def test_a_damaged_store_fails_the_gate(gate: ModuleType, runs_dir: Path, tmp_path: Path) -> None:
    root = tmp_path / "store"
    report = tmp_path / "report.md"
    assert gate.main(_args(runs_dir, root, report)) == 0
    blob = next((root / "blobs").rglob("*.zst"))
    flip_byte(blob, blob.stat().st_size // 2)
    assert gate.main(_args(runs_dir, root, report)) == 1
    text = report.read_text()
    assert "NOT OK" in text and "- FAILED:" in text and "corrupt_blob" in text


def test_a_failed_import_is_a_row_and_fails_the_gate(gate: ModuleType, runs_dir: Path, tmp_path: Path) -> None:
    bad = runs_dir / "ar-gate-bad"
    bad.mkdir()
    (bad / "dataset.jsonl").write_text('{"id": "x"}\n{"no": "id"}\n')
    report = tmp_path / "report.md"
    assert gate.main(_args(runs_dir, tmp_path / "store", report)) == 1
    text = report.read_text()
    assert "| ar-gate-bad | import failed: SandboxFolderInvalid |" in text and "failed: 1." in text


def test_the_leak_scan_reads_decoded_cells(gate: ModuleType, tmp_path: Path) -> None:
    needle = "N" * 40
    for i, table in enumerate([
        pa.table({"s": ["a", f"x{needle}y"]}),
        pa.table({"l": [["a"], ["b", needle]]}),
        pa.table({"st": [{"k": needle}]}),
    ]):
        p = tmp_path / f"t{i}.parquet"
        pq.write_table(table, p, compression="zstd")
        assert gate.parquet_holds(p, needle)
    clean = tmp_path / "clean.parquet"
    pq.write_table(pa.table({"s": ["a", "b"]}), clean)
    assert not gate.parquet_holds(clean, needle)


def test_bad_arguments_exit_2(gate: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        gate.main(["--runs", str(tmp_path / "nope"), "--root", str(tmp_path / "s")])
    assert exc.value.code == 2


def test_floor_replay_parity_against_the_sandbox(gate: ModuleType, runs_dir: Path, tmp_path: Path) -> None:
    checkout = os.environ.get("MAILROOM_SANDBOX_CHECKOUT")
    if not checkout or not Path(checkout, "src", "mailroom_sandbox", "tui", "floor_replay.py").is_file():
        pytest.skip("no sandbox checkout (set MAILROOM_SANDBOX_CHECKOUT to run the parity check)")
    report = tmp_path / "report.md"
    assert gate.main(_args(runs_dir, tmp_path / "store", report, "--sandbox", checkout)) == 0
    text = report.read_text()
    assert text.count("| ok (") == 3 and "checked against the sandbox's build_timeline" in text
    assert not [m for m in sys.modules if m.startswith("mailroom_sandbox")]


def test_import_warnings_reach_the_report(gate: ModuleType, runs_dir: Path, tmp_path: Path) -> None:
    """The crashed run's nodes.jsonl ends in a torn line: the import skips it and says so,
    and the report carries that warning on the run's row (counts and file names only)."""
    raw = (runs_dir / "ar-gate-crashed" / "nodes.jsonl").read_bytes()
    torn = len(raw) - raw.rfind(b"\n") - 1
    report = tmp_path / "report.md"
    assert gate.main(_args(runs_dir, tmp_path / "store", report)) == 0
    rows = {line.split(" | ")[0][2:]: line for line in report.read_text().splitlines() if line.startswith("| ar-gate-")}
    assert f"nodes.jsonl: skipped a torn last line ({torn} bytes, no newline)" in rows["ar-gate-crashed"]
    assert rows["ar-gate-r00"].endswith("| none |")  # the warnings column: nothing skipped
    assert "Runs with import warnings: 1." in report.read_text()


def test_parity_survives_rows_without_status_and_a_failing_side(
    gate: ModuleType, runs_dir: Path, tmp_path: Path
) -> None:
    """A sandbox node row without ``status`` (no such key in its dict) compares as ``None``;
    an error on either side is a FAILED parity row, never an exception that kills the gate."""
    import copy
    from types import SimpleNamespace

    from mailroom_record import BlobStore, EventLog, import_sandbox_run, replay_timeline

    folder = runs_dir / "ar-gate-r00"
    rows = [json.loads(line) for line in (folder / "nodes.jsonl").read_text().splitlines()]
    del rows[1]["status"]
    (folder / "nodes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    blobs = BlobStore(tmp_path / "store")
    log = EventLog(tmp_path / "store", blobs)
    run_id = import_sandbox_run(folder, log, blobs).run_id

    def like_the_sandbox(_folder: Path) -> tuple:  # the record's timeline, rows shaped as the sandbox's
        timeline, meta = replay_timeline(log, run_id)
        timeline = copy.deepcopy(timeline)
        for _, event in timeline:
            if "node" in event and event["node"]["status"] is None:
                del event["node"]["status"]
        return timeline, meta

    assert gate._parity(log, run_id, folder, SimpleNamespace(build_timeline=like_the_sandbox)).startswith("ok (")

    def broken(_folder: Path) -> tuple:
        raise KeyError("status")

    assert gate._parity(log, run_id, folder, SimpleNamespace(build_timeline=broken)) == "FAILED (sandbox error: KeyError)"


def test_a_stored_run_that_imports_again_fails_the_gate(gate: ModuleType, runs_dir: Path, tmp_path: Path) -> None:
    """A second run must find every run it already stored ``already_imported``. Here the
    stored run lacks the folder's last item row (the folder grew after the first run): the
    import appends it and says ``imported``, which fails the gate (finding 10)."""
    items = runs_dir / "ar-gate-r00" / "items.jsonl"
    whole = items.read_bytes()
    items.write_bytes(whole[: whole.rstrip(b"\n").rfind(b"\n") + 1])
    root, report = tmp_path / "store", tmp_path / "report.md"
    assert gate.main(_args(runs_dir, root, report)) == 0
    items.write_bytes(whole)
    assert gate.main(_args(runs_dir, root, report)) == 1
    text = report.read_text()
    assert "| ar-gate-r00 | imported |" in text
    assert "- FAILED: ar-gate-r00: already stored before this gate run, but imported again" in text


def test_expect_runs_checks_the_folder_count(gate: ModuleType, runs_dir: Path, tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    assert gate.main(_args(runs_dir, tmp_path / "s1", report, "--expect-runs", "3")) == 0
    assert "Run folders: 3 (expected 3)" in report.read_text()
    assert gate.main(_args(runs_dir, tmp_path / "s2", report, "--expect-runs", "15")) == 1
    assert "- FAILED: found 3 run folders, expected 15" in report.read_text()


# --- scoped to its own --runs folder (adversarial round 2, finding 8) ----------------------


def test_an_empty_runs_folder_fails_the_gate(gate: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    report = tmp_path / "report.md"
    assert gate.main(_args(tmp_path / "empty", tmp_path / "store", report)) == 1
    assert "- FAILED: no run folders under --runs" in report.read_text()
    assert gate.main(_args(tmp_path / "empty", tmp_path / "store", report, "--expect-runs", "0")) == 1


def test_a_store_holding_other_runs_fails_the_gate(gate: ModuleType, runs_dir: Path, tmp_path: Path) -> None:
    """The trace, index, size and leak checks read the whole store, so they speak for the
    runs under --runs only when the store holds nothing else. A store that already holds
    a run no folder under --runs imports fails the gate, naming how many."""
    import shutil

    root, report = tmp_path / "store", tmp_path / "report.md"
    assert gate.main(_args(runs_dir, root, report)) == 0  # all three runs stored
    subset = tmp_path / "subset"
    subset.mkdir()
    shutil.copytree(runs_dir / "ar-gate-r00", subset / "ar-gate-r00")
    assert gate.main(_args(subset, root, report)) == 1
    text = report.read_text()
    assert "- FAILED: the store holds 2 runs not under --runs (ar-gate-crashed, ar-gate-r01)" in text
    assert gate.main(_args(subset, tmp_path / "own-store", report)) == 0  # a store of its own passes
    assert gate.main(_args(runs_dir, root, report)) == 0  # and the full folder still passes on the first
