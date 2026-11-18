"""The statistics capture script and the committed corpus agree.

The corpus is only as good as the script that wrote it: every seeded input the
generator makes is in the corpus under the same id, and every generated case in the
corpus is one the generator makes, so inputs added to the generator (and their old
outcomes) cannot be missing or hand-written. The script never runs the old Dojo here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import sys
from decimal import Decimal
from functools import cache
from types import ModuleType

import pytest

from dojo_testkit import PACKAGE_ROOT, decode, encode

SCRIPT_PATH = PACKAGE_ROOT / "scripts" / "capture_old_dojo_statistics_cases.py"
CORPUS_PATH = PACKAGE_ROOT / "tests" / "fixtures" / "differential" / "statistics_cases.jsonl"


@cache
def capture_script() -> ModuleType:
    """The capture script, loaded by path (it imports nothing from the old Dojo at load time)."""
    spec = importlib.util.spec_from_file_location("capture_old_dojo_statistics_cases", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def corpus() -> tuple[dict, list[dict]]:
    """The corpus header and cases."""
    lines = CORPUS_PATH.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0])["header"], [json.loads(line) for line in lines[1:]]


def test_the_corpus_holds_exactly_the_generated_cases():
    """Generator output (at the header's seed) and the corpus's generated cases are the same set."""
    script = capture_script()
    header, cases = corpus()
    generated = {}
    for case in script.generated_cases(random.Random(header["seed"])):
        case_id = hashlib.sha256(script.case_key(case).encode("utf-8")).hexdigest()[:16]
        generated.setdefault(case_id, case)
    in_corpus = {case["id"]: case for case in cases}
    missing = [case_id for case_id in generated if case_id not in in_corpus]
    assert not missing, f"{len(missing)} generated cases are not in the corpus, first {missing[:3]}"
    for case_id, case in generated.items():
        assert (in_corpus[case_id]["function"], in_corpus[case_id]["args"]) == (
            case["function"],
            case["args"],
        )
    extra = [case["id"] for case in cases if case["source"] == "generated" and case["id"] not in generated]
    assert not extra, f"{len(extra)} generated corpus cases the generator does not make, first {extra[:3]}"


def test_the_supplement_is_recorded_in_the_header():
    """Cases added by --supplement are counted in the header with how they were produced."""
    header, cases = corpus()
    added = sum(entry["cases_added"] for entry in header.get("supplements", []))
    assert added == 21
    assert all(case["source"] == "generated" for case in cases[-added:])
    assert all("loaded by file path" in entry["method"] for entry in header["supplements"])


def test_decimals_survive_the_tagged_json_round_trip():
    """A Decimal argument is stored as text and read back as the same Decimal."""
    assert encode({"p": Decimal("0.25")}) == {"p": {"$decimal": "0.25"}}
    assert decode({"p": {"$decimal": "0.25"}}) == {"p": Decimal("0.25")}
    assert type(decode(encode(Decimal("1E+2")))) is Decimal


# --- refusals: the capture writes nothing from the wrong old Dojo ----------------


def fake_checkout(root, commit):
    """A directory that looks like an old Dojo checkout at ``commit`` (a detached HEAD)."""
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text(commit + "\n", encoding="utf-8")
    return root


def run_main(monkeypatch, script, old_dojo, output, *extra):
    """Run the script's main() with arguments; the old tests and old modules must never run."""

    def never(*_args, **_kwargs):
        raise AssertionError("the capture went on to run the old Dojo")

    monkeypatch.setattr(script, "capture_child", never)
    monkeypatch.setattr(script, "old_functions_from_files", never)
    monkeypatch.setattr(
        "sys.argv", [str(SCRIPT_PATH), "--old-dojo", str(old_dojo), "--output", str(output), *extra]
    )
    monkeypatch.setattr(sys, "path", list(sys.path))
    return script.main()


def test_the_capture_refuses_an_old_dojo_at_another_commit(tmp_path, monkeypatch, capsys):
    """A checkout at any commit but the pinned one is refused before anything runs."""
    script = capture_script()
    old_dojo = fake_checkout(tmp_path / "old", "0" * 40)
    output = tmp_path / "out" / "cases.jsonl"
    assert run_main(monkeypatch, script, old_dojo, output) == 1
    assert f"old Dojo is at {'0' * 40}, expected {script.EXPECTED_OLD_COMMIT}" in capsys.readouterr().err
    assert not output.exists()


def test_the_capture_refuses_an_old_dojo_imported_from_elsewhere(tmp_path, monkeypatch, capsys):
    """If llm_dojo_scoring resolves outside --old-dojo (another install), nothing runs."""
    script = capture_script()
    old_dojo = fake_checkout(tmp_path / "old", script.EXPECTED_OLD_COMMIT)
    old_sources(old_dojo, monkeypatch, script)
    decoy = ModuleType("llm_dojo_scoring")
    decoy.__file__ = str(tmp_path / "site-packages" / "llm_dojo_scoring" / "__init__.py")
    monkeypatch.setitem(sys.modules, "llm_dojo_scoring", decoy)
    output = tmp_path / "out" / "cases.jsonl"
    assert run_main(monkeypatch, script, old_dojo, output) == 1
    assert "llm_dojo_scoring was imported from" in capsys.readouterr().err
    assert not output.exists()


def test_the_old_dojo_import_check_accepts_the_checkout_itself(tmp_path, monkeypatch):
    """The package inside --old-dojo passes the check."""
    script = capture_script()
    old_dojo = tmp_path / "old"
    inside = ModuleType("llm_dojo_scoring")
    inside.__file__ = str(old_dojo / "llm_dojo_scoring" / "__init__.py")
    monkeypatch.setitem(sys.modules, "llm_dojo_scoring", inside)
    assert script.import_old_dojo(old_dojo) is inside


def old_sources(root, monkeypatch, script, *, edited=None):
    """Write stand-in old module files under ``root`` and pin their hashes in the script.

    ``edited`` names one file whose content is changed after its hash was pinned.
    """
    pinned = {}
    for relative in script.TARGETS:
        path = root / "llm_dojo_scoring" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"# stand-in for {relative}\n".encode())
        pinned[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        if relative == edited:
            path.write_bytes(path.read_bytes() + b"x = 1\n")
    monkeypatch.setattr(script, "EXPECTED_OLD_SOURCE_SHA256", pinned)


def corpus_copy(path, **header_changes):
    """A copy of the committed corpus at ``path`` with header fields replaced; returns its bytes."""
    lines = CORPUS_PATH.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])["header"] | header_changes
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([json.dumps({"header": header}, sort_keys=True), *lines[1:]]) + "\n")
    return path.read_bytes()


def test_the_pinned_old_source_hashes_cover_both_old_modules():
    """Every old module the supplement loads by path has a pinned SHA-256."""
    script = capture_script()
    assert set(script.EXPECTED_OLD_SOURCE_SHA256) == set(script.TARGETS)
    assert all(len(digest) == 64 for digest in script.EXPECTED_OLD_SOURCE_SHA256.values())


def test_the_supplement_refuses_an_old_dojo_at_another_commit(tmp_path, monkeypatch, capsys):
    """--supplement checks the commit before it reads or writes the corpus."""
    script = capture_script()
    old_dojo = fake_checkout(tmp_path / "old", "0" * 40)
    old_sources(old_dojo, monkeypatch, script)
    output = tmp_path / "out" / "cases.jsonl"
    before = corpus_copy(output)
    assert run_main(monkeypatch, script, old_dojo, output, "--supplement") == 1
    assert f"old Dojo is at {'0' * 40}, expected {script.EXPECTED_OLD_COMMIT}" in capsys.readouterr().err
    assert output.read_bytes() == before


@pytest.mark.parametrize(
    ("header_changes", "extra"),
    [
        ({"seed": 7}, ()),
        ({}, ("--seed", "7")),
        ({"old_commit": "1" * 40}, ()),
    ],
)
def test_the_supplement_refuses_a_corpus_from_another_seed_or_commit(
    tmp_path, monkeypatch, capsys, header_changes, extra
):
    """A corpus whose header names another seed or old commit than this run is left alone."""
    script = capture_script()
    old_dojo = fake_checkout(tmp_path / "old", script.EXPECTED_OLD_COMMIT)
    old_sources(old_dojo, monkeypatch, script)
    output = tmp_path / "out" / "cases.jsonl"
    before = corpus_copy(output, **header_changes)
    assert run_main(monkeypatch, script, old_dojo, output, "--supplement", *extra) == 1
    assert "the corpus was captured with another seed or old commit" in capsys.readouterr().err
    assert output.read_bytes() == before


@pytest.mark.parametrize("mode", ["--supplement", "full"])
@pytest.mark.parametrize("relative", ["analysis/bootstrap.py", "analysis/calibration.py"])
def test_the_capture_refuses_an_edited_old_module(tmp_path, monkeypatch, capsys, mode, relative):
    """At the pinned commit but with an old module edited on disk, nothing is loaded or written."""
    script = capture_script()
    old_dojo = fake_checkout(tmp_path / "old", script.EXPECTED_OLD_COMMIT)
    old_sources(old_dojo, monkeypatch, script, edited=relative)
    output = tmp_path / "out" / "cases.jsonl"
    before = corpus_copy(output)
    extra = ("--supplement",) if mode == "--supplement" else ()
    assert run_main(monkeypatch, script, old_dojo, output, *extra) == 1
    assert f"llm_dojo_scoring/{relative} does not have the pinned SHA-256" in capsys.readouterr().err
    assert output.read_bytes() == before


def test_the_capture_refuses_a_missing_old_module(tmp_path, monkeypatch, capsys):
    """A checkout without one of the old modules is refused, not half-loaded."""
    script = capture_script()
    old_dojo = fake_checkout(tmp_path / "old", script.EXPECTED_OLD_COMMIT)
    old_sources(old_dojo, monkeypatch, script)
    (old_dojo / "llm_dojo_scoring" / "analysis" / "calibration.py").unlink()
    output = tmp_path / "out" / "cases.jsonl"
    before = corpus_copy(output)
    assert run_main(monkeypatch, script, old_dojo, output, "--supplement") == 1
    assert "llm_dojo_scoring/analysis/calibration.py is missing" in capsys.readouterr().err
    assert output.read_bytes() == before


def test_loading_the_old_modules_by_path_refuses_an_edited_file(tmp_path, monkeypatch):
    """old_functions_from_files checks the hashes itself, so no caller can skip the check."""
    script = capture_script()
    old_sources(tmp_path, monkeypatch, script, edited="analysis/bootstrap.py")
    with pytest.raises(SystemExit, match="does not have the pinned SHA-256"):
        script.old_functions_from_files(tmp_path)
