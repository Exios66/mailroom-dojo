"""The metric-registry capture script refuses what would write a wrong or unsafe fixture.

It never runs the old Dojo here: the checkout's commit is stubbed and the refusals
happen before anything is imported from it.
"""

from __future__ import annotations

import errno
import importlib.util
import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest

from dojo_testkit import PACKAGE_ROOT

SCRIPT_PATH = PACKAGE_ROOT / "scripts" / "capture_old_metric_registry.py"


@cache
def capture_script() -> ModuleType:
    """The capture script, loaded by path (it imports nothing from the old Dojo at load time)."""
    spec = importlib.util.spec_from_file_location("capture_old_metric_registry", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _never(*_args: object) -> None:
    raise AssertionError("the old checkout must not be read after a refusal")


def test_a_checkout_at_another_commit_is_refused(monkeypatch, tmp_path):
    """A different commit stops the capture before the registry is read, and nothing is written."""
    script = capture_script()
    monkeypatch.setattr(script, "_old_commit", lambda _path: "0" * 40)
    monkeypatch.setattr(script, "_registry", _never)
    output = tmp_path / "out" / "old_metric_registry.json"
    with pytest.raises(SystemExit, match=f"is at {'0' * 40}, expected {script.EXPECTED_OLD_COMMIT}"):
        script.main(["--old-dojo", str(tmp_path / "old"), "--output", str(output)])
    assert not output.exists()


@pytest.mark.parametrize(
    "inside",
    ["old_metric_registry.json", "tests/fixtures/golden/scores.json", "sub/../x.json", "."],
)
def test_an_output_inside_the_old_checkout_is_refused(monkeypatch, tmp_path, inside):
    """An output inside the old checkout (also through ``..``, or the checkout itself) is refused first."""
    script = capture_script()
    monkeypatch.setattr(script, "build", _never)
    old = tmp_path / "old"
    old.mkdir()
    output = old / inside
    with pytest.raises(SystemExit, match="inside the old checkout"):
        script.main(["--old-dojo", str(old), "--output", str(output)])
    assert sorted(old.iterdir()) == []


def test_an_output_reaching_the_checkout_through_a_symlink_is_refused(monkeypatch, tmp_path):
    """A symlinked output directory that points into the old checkout is refused."""
    script = capture_script()
    monkeypatch.setattr(script, "build", _never)
    old = tmp_path / "old"
    old.mkdir()
    link = tmp_path / "link"
    link.symlink_to(old, target_is_directory=True)
    with pytest.raises(SystemExit, match="inside the old checkout"):
        script.main(["--old-dojo", str(old), "--output", str(link / "x.json")])
    assert sorted(old.iterdir()) == []


def test_an_output_beside_the_checkout_is_written(monkeypatch, tmp_path):
    """A sibling path whose name only starts like the checkout's is outside it, and is written."""
    script = capture_script()
    monkeypatch.setattr(script, "build", lambda _path: _DOCUMENT)
    old = tmp_path / "old"
    old.mkdir()
    output = tmp_path / "old-capture" / "x.json"
    assert script.main(["--old-dojo", str(old), "--output", str(output)]) == 0
    assert Path(output).read_text(encoding="utf-8").startswith("{")


_DOCUMENT = {
    "old_commit": "c",
    "old_version": "v",
    "metrics": {},
    "golden": {"names": {}, "breakdowns": {}},
}


def test_a_checkout_with_local_changes_is_refused(monkeypatch, tmp_path):
    """At the pinned commit but with an edited or added file, the capture stops before reading it."""
    script = capture_script()
    monkeypatch.setattr(script, "_old_commit", lambda _path: script.EXPECTED_OLD_COMMIT)
    monkeypatch.setattr(script, "_local_changes", lambda _path: [" M llm_dojo_scoring/metrics/registry.py"])
    monkeypatch.setattr(script, "_registry", _never)
    output = tmp_path / "out" / "old_metric_registry.json"
    with pytest.raises(SystemExit, match=r"has local changes \( M llm_dojo_scoring/metrics/registry.py\)"):
        script.main(["--old-dojo", str(tmp_path / "old"), "--output", str(output)])
    assert not output.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_local_changes_are_read_from_git_status(tmp_path):
    """A clean repository has none; an added file (tracked or not) is one."""
    script = capture_script()
    old = tmp_path / "old"
    subprocess.run(["git", "init", "-q", str(old)], check=True)
    assert script._local_changes(old) == []
    (old / "registry.py").write_text("x = 1\n", encoding="utf-8")
    assert script._local_changes(old) == ["?? registry.py"]
    subprocess.run(["git", "-C", str(old), "add", "registry.py"], check=True)
    assert script._local_changes(old) == ["A  registry.py"]


def test_an_old_dojo_imported_from_elsewhere_is_refused(monkeypatch, tmp_path):
    """An llm_dojo_scoring already imported from outside --old-dojo is never read as the pinned one."""
    script = capture_script()
    decoy = ModuleType("llm_dojo_scoring")
    decoy.__file__ = str(tmp_path / "site-packages" / "llm_dojo_scoring" / "__init__.py")
    monkeypatch.setitem(sys.modules, "llm_dojo_scoring", decoy)
    monkeypatch.setattr(sys, "path", list(sys.path))
    old = tmp_path / "old"
    with pytest.raises(SystemExit, match="llm_dojo_scoring was imported from .*site-packages.*, not from"):
        script._registry(old)


def test_a_failed_write_keeps_the_existing_fixture(monkeypatch, tmp_path):
    """The fixture is replaced only after the new one is fully written; a disk error leaves the old one."""
    script = capture_script()
    monkeypatch.setattr(script, "build", lambda _path: _DOCUMENT)
    old = tmp_path / "old"
    old.mkdir()
    output = tmp_path / "fixtures" / "old_metric_registry.json"
    output.parent.mkdir()
    output.write_text('{"previous": true}\n', encoding="utf-8")
    real_write_text = Path.write_text

    def disk_full(self, data, *args, **kwargs):
        real_write_text(self, "", *args, **kwargs)  # truncated, as a real write would leave it
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Path, "write_text", disk_full)
    with pytest.raises(OSError, match="No space left"):
        script.main(["--old-dojo", str(old), "--output", str(output)])
    monkeypatch.undo()
    assert output.read_text(encoding="utf-8") == '{"previous": true}\n'
    assert sorted(path.name for path in output.parent.iterdir()) == ["old_metric_registry.json"]
