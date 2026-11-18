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


def run_main(monkeypatch, script, old_dojo, output):
    """Run the script's main() with arguments; the old tests must never start."""

    def never(*_args, **_kwargs):
        raise AssertionError("the capture went on to run the old Dojo")

    monkeypatch.setattr(script, "capture_child", never)
    monkeypatch.setattr("sys.argv", [str(SCRIPT_PATH), "--old-dojo", str(old_dojo), "--output", str(output)])
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
