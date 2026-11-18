"""Task 12: the stage 1 gate script, run on synthetic run folders (the real runs are not here)."""

from __future__ import annotations

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
