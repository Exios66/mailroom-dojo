"""The metric-registry capture script refuses what would write a wrong or unsafe fixture.

It never runs the old Dojo here: the checkout's commit is stubbed and the refusals
happen before anything is imported from it.
"""

from __future__ import annotations

import importlib.util
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
    """An output path inside the old checkout (also through ``..`` or the checkout itself) is refused first."""
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
