"""The statistics layer gives exactly the old Dojo's output on the frozen differential corpus.

The corpus (``fixtures/differential/statistics_cases.jsonl``) holds every encodable call
the old Dojo made into ``analysis/bootstrap.py`` and ``analysis/calibration.py`` while its
golden baseline was regenerated and while the old tests that reach them ran (four old-test
calls with NumPy arguments are not encodable; the header counts them in
``captured_test_skipped``), plus seeded generated inputs, each with the old outcome
computed at commit 23f8234c. Results are
compared as canonical JSON (``1`` is not ``1.0``, ``None`` is not ``0.0``); an exception
matches only with the same type and the same message.

A difference must be listed in ``statistics_intended_differences.json`` with its reason
and the new outcome; every listed one is an invalid input (NaN, a rate outside
``[0, 1]`` or given as text, a support that is not a whole number, a bad resampling
parameter or a text seed), or a ``Decimal`` rate ``wilson_bounds`` refused before too.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from functools import cache
from typing import Any

import pytest

from dojo_testkit import OLD_DOJO_COMMIT, PACKAGE_ROOT, canonical, decode, encode
from mailroom_dojo import statistics

DIFFERENTIAL = PACKAGE_ROOT / "tests" / "fixtures" / "differential"
CORPUS_PATH = DIFFERENTIAL / "statistics_cases.jsonl"
LEDGER_PATH = DIFFERENTIAL / "statistics_intended_differences.json"

#: Every old public function, by its (unchanged) name.
FUNCTIONS = (
    "auroc",
    "bootstrap_ci",
    "brier_score",
    "clopper_pearson_upper",
    "cross_fit_cutoffs",
    "cutoff_at_target",
    "cutoff_for_precision",
    "delta_significance",
    "expected_calibration_error",
    "reliability_bins",
    "required_samples",
    "round_down",
    "round_up",
    "spearman",
    "wilson_bounds",
    "wilson_ci",
)


@cache
def corpus() -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """The corpus header and its cases."""
    lines = CORPUS_PATH.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0])["header"], tuple(json.loads(line) for line in lines[1:])


@cache
def ledger() -> dict[str, dict[str, Any]]:
    """The intended differences, by case id."""
    document = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    entries: dict[str, dict[str, Any]] = {}
    for entry in document["differences"]:
        if entry["case"] in entries:
            raise AssertionError(f"intended difference {entry['case']} is listed twice")
        entries[entry["case"]] = entry
    return entries


def plain(value: Any) -> Any:
    """Result objects as plain data: ``to_dict()``, also for each item of a list."""
    if isinstance(value, list):
        return [plain(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    return to_dict() if callable(to_dict) else value


def replay(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through the new code; encode the result or exception like the corpus."""
    try:
        result = getattr(statistics, case["function"])(**decode(case["args"]))
    except Exception as error:  # the exception is the compared outcome
        return {"raises": type(error).__name__, "message": str(error)}
    return {"result": encode(plain(result))}


def ledger_problems(
    replayed: Iterable[tuple[dict[str, Any], dict[str, Any]]], entries: dict[str, dict[str, Any]]
) -> list[str]:
    """Every unlisted, stale or changed difference among ``(case, new outcome)`` pairs."""
    pending = dict(entries)
    problems: list[str] = []
    for case, new in replayed:
        entry = pending.pop(case["id"], None)
        differs = canonical(new) != canonical(case["outcome"])
        if differs and entry is None:
            problems.append(
                f"unlisted difference {case['id']} ({case['function']}): "
                f"old {canonical(case['outcome'])[:200]} new {canonical(new)[:200]}"
            )
        elif not differs and entry is not None:
            problems.append(
                f"stale intended difference {case['id']} ({case['function']}): it no longer differs"
            )
        elif differs and canonical(entry["new_outcome"]) != canonical(new):
            problems.append(
                f"intended difference {case['id']} ({case['function']}) changed again: "
                f"listed {canonical(entry['new_outcome'])[:200]} now {canonical(new)[:200]}"
            )
    problems += [f"intended difference {case_id} names no replayed case" for case_id in pending]
    return problems


def test_corpus_header_names_the_old_commit_and_matches_its_content():
    """The corpus states where it came from, and its counts agree with its lines."""
    header, cases = corpus()
    assert header["old_commit"] == OLD_DOJO_COMMIT
    assert header["old_version"] == "0.30.1"
    assert header["cases"] == len(cases)
    assert header["cases_by_function"] == dict(sorted(Counter(case["function"] for case in cases).items()))
    assert len({case["id"] for case in cases}) == len(cases)


def test_every_old_function_has_cases_from_every_source_it_was_called_from():
    """Every old public function is in the corpus; the golden run and the old tests both contributed."""
    _, cases = corpus()
    assert {case["function"] for case in cases} == set(FUNCTIONS)
    sources = Counter(case["source"] for case in cases)
    assert sources["golden_run"] >= 10
    assert sources["old_tests"] >= 400
    assert sources["generated"] >= 1000
    golden = {case["function"] for case in cases if case["source"] == "golden_run"}
    assert golden == {"bootstrap_ci", "delta_significance"}


@pytest.mark.parametrize("function", FUNCTIONS)
def test_new_code_reproduces_every_old_output(function):
    """Each case's new outcome equals the old one, or is a listed intended difference."""
    _, cases = corpus()
    selected = [case for case in cases if case["function"] == function]
    entries = {case_id: entry for case_id, entry in ledger().items() if entry["function"] == function}
    problems = ledger_problems(((case, replay(case)) for case in selected), entries)
    assert not problems, f"{len(problems)} problems, first: {problems[:5]}"


def test_no_valid_old_case_differs():
    """Only invalid inputs differ: every golden-run and old-test case reproduces exactly."""
    _, cases = corpus()
    listed = ledger()
    recorded = [case for case in cases if case["source"] != "generated"]
    assert not [case["id"] for case in recorded if case["id"] in listed]
    assert len(recorded) >= 450


def test_every_ledger_entry_names_a_real_case_with_a_reason():
    """Each entry names an existing case and its function, a reason and an exception (all are refusals)."""
    _, cases = corpus()
    functions = {case["id"]: case["function"] for case in cases}
    assert ledger()
    for case_id, entry in ledger().items():
        assert functions.get(case_id) == entry["function"], case_id
        assert entry["reason"].strip()
        assert set(entry["new_outcome"]) == {"raises", "message"}
        assert entry["new_outcome"]["raises"] in {"ValueError", "TypeError"}


def test_the_ledger_check_catches_unlisted_stale_and_changed_differences():
    """Positive control: the comparison sees each kind of ledger problem."""
    case = {"id": "c1", "function": "brier_score", "outcome": {"result": 0.5}}
    same = {"result": 0.5}
    other = {"result": 0.25}
    assert ledger_problems([(case, same)], {}) == []
    assert "unlisted difference c1" in ledger_problems([(case, other)], {})[0]
    listed = {"c1": {"function": "brier_score", "new_outcome": {"result": 0.125}}}
    assert "stale intended difference c1" in ledger_problems([(case, same)], listed)[0]
    assert "changed again" in ledger_problems([(case, other)], listed)[0]
    assert "names no replayed case" in ledger_problems([], listed)[0]
    raised = {"raises": "ValueError", "message": "other text"}
    raised_case = {**case, "outcome": {"raises": "ValueError", "message": "bins must be positive"}}
    assert ledger_problems([(raised_case, raised)], {}) != []  # same type, other message
    integer_case = {"id": "c2", "function": "required_samples", "outcome": {"result": 1}}
    assert ledger_problems([(integer_case, {"result": 1.0})], {}) != []
