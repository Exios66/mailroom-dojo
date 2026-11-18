"""The version lock and scripts/update_lock.py, on a real copy of the data directory."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from mailroom_common.errors import ConfigError
from mailroom_dictionaries import DATA_DIR
from mailroom_dictionaries.lock import LOCK_NAME, lock_problems, read_lock, write_lock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_lock.py"
ROW = {"version": 1, "hash": "a" * 64}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    shutil.copytree(DATA_DIR, target)
    return target


def run_update(data_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--data-dir", str(data_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def edit(data_dir: Path, name: str, *, description: str | None = None, version: int | None = None) -> None:
    path = data_dir / f"{name}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if description is not None:
        data["entries"][0]["description"] = description
    if version is not None:
        data["version"] = version
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def kinds(
    locked: dict[str, dict[str, object]], current: dict[str, dict[str, object]]
) -> list[tuple[str, str]]:
    return [(p.kind, p.name) for p in lock_problems(locked, current)]


def test_lock_problem_kinds() -> None:
    assert kinds({"a": ROW}, {"a": ROW}) == []
    assert kinds({}, {"a": ROW}) == [("unlocked", "a")]
    assert kinds({"a": ROW}, {}) == [("orphaned", "a")]
    assert kinds({"a": ROW}, {"a": {"version": 1, "hash": "b" * 64}}) == [("unbumped", "a")]
    assert kinds({"a": ROW}, {"a": {"version": 2, "hash": "b" * 64}}) == [("stale", "a")]
    assert kinds({"a": ROW}, {"a": {"version": 2, "hash": "a" * 64}}) == [("stale", "a")]
    assert kinds({"a": {"version": 3, "hash": "a" * 64}}, {"a": ROW}) == [("backwards", "a")]


def test_unbumped_message_says_what_to_do() -> None:
    (problem,) = lock_problems({"urgency": ROW}, {"urgency": {"version": 1, "hash": "c" * 64}})
    assert problem.message == (
        "content of 'urgency' changed but its version is still 1: "
        "bump `version`, then run scripts/update_lock.py"
    )


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("{", "cannot read dictionary lock"),
        ("[]", "must map each name"),
        ('{"a": {"version": "1", "hash": "x"}}', "must map each name"),
        ('{"a": {"version": true, "hash": "x"}}', "must map each name"),
        ('{"a": {"version": 1}}', "must map each name"),
    ],
)
def test_malformed_lock(tmp_path: Path, text: str, match: str) -> None:
    path = tmp_path / LOCK_NAME
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        read_lock(path)


def test_missing_lock(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read dictionary lock"):
        read_lock(tmp_path / LOCK_NAME)


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    path = tmp_path / LOCK_NAME
    write_lock(path, {"b": ROW, "a": {"version": 2, "hash": "f" * 64}})
    assert path.read_text(encoding="utf-8").endswith("}\n")
    assert list(read_lock(path)) == ["a", "b"]


def test_update_lock_is_a_no_op_on_current_data(data_dir: Path) -> None:
    before = (data_dir / LOCK_NAME).read_bytes()
    result = run_update(data_dir)
    assert result.returncode == 0, result.stderr
    assert "lock is current (19 dictionaries)" in result.stdout
    assert (data_dir / LOCK_NAME).read_bytes() == before


def test_update_lock_refuses_a_change_without_a_version_bump(data_dir: Path) -> None:
    before = (data_dir / LOCK_NAME).read_bytes()
    edit(data_dir, "urgency", description="Changed.")
    result = run_update(data_dir)
    assert result.returncode == 1
    assert "refused: content of 'urgency' changed but its version is still 1" in result.stderr
    assert (data_dir / LOCK_NAME).read_bytes() == before


def test_update_lock_refuses_a_version_going_backwards(data_dir: Path) -> None:
    lock = json.loads((data_dir / LOCK_NAME).read_text(encoding="utf-8"))
    lock["urgency"]["version"] = 5
    (data_dir / LOCK_NAME).write_text(json.dumps(lock), encoding="utf-8")
    result = run_update(data_dir)
    assert result.returncode == 1 and "went backwards: lock has 5, file has 1" in result.stderr


def test_update_lock_records_a_bumped_change(data_dir: Path) -> None:
    edit(data_dir, "urgency", description="Changed.", version=2)
    result = run_update(data_dir)
    assert result.returncode == 0, result.stderr
    assert "updated urgency: stale" in result.stdout
    lock = read_lock(data_dir / LOCK_NAME)
    assert lock["urgency"]["version"] == 2
    assert run_update(data_dir).stdout.startswith("lock is current")


def test_update_lock_adds_a_new_file_and_drops_a_removed_one(data_dir: Path) -> None:
    shutil.copy(data_dir / "urgency.yaml", data_dir / "priority.yaml")
    edit(data_dir, "priority")
    text = (data_dir / "priority.yaml").read_text(encoding="utf-8").replace("name: urgency", "name: priority")
    (data_dir / "priority.yaml").write_text(text, encoding="utf-8")
    (data_dir / "outcomes.yaml").unlink()
    result = run_update(data_dir)
    assert result.returncode == 0, result.stderr
    assert "updated outcomes: orphaned" in result.stdout and "updated priority: unlocked" in result.stdout
    lock = read_lock(data_dir / LOCK_NAME)
    assert "priority" in lock and "outcomes" not in lock


def test_update_lock_reports_an_invalid_dictionary(data_dir: Path) -> None:
    (data_dir / "urgency.yaml").write_text("name: urgency\n", encoding="utf-8")
    result = run_update(data_dir)
    assert result.returncode == 1 and result.stderr.startswith("error: invalid dictionary file")
