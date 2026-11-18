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
