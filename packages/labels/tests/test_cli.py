"""The ``mailroom-labels`` CLI: exit codes through typer's CliRunner and one real process."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from labels_testkit import PROFILE_PATH, make_snapshot, near_gold_row, review_row
from mailroom_labels.cli import app

runner = CliRunner()


def args(*items: object) -> list[str]:
    return [str(i) for i in items]


def test_verify_clean_set_exits_zero(snapshot_dirs) -> None:
    result = runner.invoke(app, args("verify", "--profile", PROFILE_PATH, *snapshot_dirs))
    assert result.exit_code == 0, result.output
    assert "0 error(s), 0 warning(s)" in result.output


def test_verify_exits_one_on_an_error_and_prints_it(tmp_path: Path, snapshot_dirs) -> None:
    bad = make_snapshot(
        tmp_path, "near_gold", [near_gold_row("D4", {"urgency": "panic"})], snapshot_id="ng-x"
    )
    result = runner.invoke(app, args("verify", "--profile", PROFILE_PATH, *snapshot_dirs, bad))
    assert result.exit_code == 1
    assert "ERROR vocabulary_violation: D4 urgency near_gold" in result.output
    assert "1 error(s), 0 warning(s)" in result.output


def test_verify_warnings_only_exits_zero(tmp_path: Path, snapshot_dirs) -> None:
    orphan = make_snapshot(tmp_path, "review", [review_row("D4", "recipient", "Eve")], snapshot_id="rv-x")
    result = runner.invoke(app, args("verify", "--profile", PROFILE_PATH, *snapshot_dirs, orphan))
    assert result.exit_code == 0
    assert "WARNING orphan_decision" in result.output


def test_verify_tampered_snapshot_exits_one(snapshot_dirs) -> None:
    path = snapshot_dirs[1] / "rows.jsonl"
    path.write_bytes(path.read_bytes()[:-3])
    result = runner.invoke(app, args("verify", "--profile", PROFILE_PATH, *snapshot_dirs))
    assert result.exit_code == 1
    assert "manifest_mismatch" in result.output and "truncated" in result.output


def test_bad_profile_and_missing_option_exit_two(tmp_path: Path, snapshot_dirs) -> None:
    bad = tmp_path / "profile.yaml"
    bad.write_text(
        PROFILE_PATH.read_text().replace("type: name, tier: tier1", "type: person, tier: tier1", 1)
    )
    result = runner.invoke(app, args("verify", "--profile", bad, *snapshot_dirs))
    assert result.exit_code == 2
    assert "is not a Dojo field type" in result.output
    assert runner.invoke(app, args("verify", *snapshot_dirs)).exit_code == 2


def test_coverage_writes_both_reports(tmp_path: Path, snapshot_dirs) -> None:
    out_json, out_md = tmp_path / "c.json", tmp_path / "c.md"
    result = runner.invoke(
        app,
        args(
            "coverage", "--profile", PROFILE_PATH, "--json-out", out_json, "--md-out", out_md, *snapshot_dirs
        ),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(out_json.read_text())["totals"]["scorable_labels"] == 11
    assert out_md.read_text().startswith("# Label coverage: synthetic-client")
    # the reversed order writes fresh files, so a run that failed or wrote nothing cannot pass
    again_json, again_md = tmp_path / "again.json", tmp_path / "again.md"
    again = runner.invoke(
        app,
        args(
            "coverage",
            "--profile",
            PROFILE_PATH,
            "--json-out",
            again_json,
            "--md-out",
            again_md,
            *reversed(snapshot_dirs),
        ),
    )
    assert again.exit_code == 0, again.output
    assert again_json.read_bytes() == out_json.read_bytes()
    assert again_md.read_bytes() == out_md.read_bytes()


def test_coverage_refuses_and_writes_nothing_on_an_error(tmp_path: Path, snapshot_dirs) -> None:
    bad = make_snapshot(tmp_path, "near_gold", [near_gold_row("D4", {"colour": "red"})], snapshot_id="ng-x")
    out_json, out_md = tmp_path / "c.json", tmp_path / "c.md"
    result = runner.invoke(
        app,
        args(
            "coverage",
            "--profile",
            PROFILE_PATH,
            "--json-out",
            out_json,
            "--md-out",
            out_md,
            *snapshot_dirs,
            bad,
        ),
    )
    assert result.exit_code == 1
    assert not out_json.exists() and not out_md.exists()


def test_real_process_exit_codes(tmp_path: Path, snapshot_dirs) -> None:
    """The installed console script, in a real subprocess: 0 clean, 1 after tampering."""
    exe = shutil.which("mailroom-labels", path=str(Path(sys.executable).parent))
    assert exe is not None, "the documented console script mailroom-labels is not installed"
    cmd = [exe]
    clean = subprocess.run(
        [*cmd, "verify", "--profile", str(PROFILE_PATH), *map(str, snapshot_dirs)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
    (snapshot_dirs[0] / "rows.jsonl").unlink()
    broken = subprocess.run(
        [*cmd, "verify", "--profile", str(PROFILE_PATH), *map(str, snapshot_dirs)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert broken.returncode == 1
    assert "manifest_mismatch" in broken.stdout
