"""The classification layer gives exactly the old Dojo's output on a frozen differential corpus.

``fixtures/golden/classification_cases.jsonl`` holds every classification call the old
Dojo's ``test_classification.py`` and ``test_paired_cardinality.py`` made, plus seeded
generated sorter outputs (JSON objects with ``doc_type``, fenced and quoted text, mixed
case, near-miss labels, error rows, None, numbers, unicode), paired label lists, raw
binary flags, F-beta edge values and length mismatches, each with the old output at
commit 23f8234c. The new functions run with the mailroom-dataset profile's label set
and must give the same canonical JSON (``1`` is not ``1.0``, ``None`` is not ``0.0``)
or raise the same exception type (and, for the length check, the same message).
"""

from __future__ import annotations

import json
from collections import Counter
from functools import cache
from typing import Any, Callable

import pytest
from dojo_testkit import OLD_DOJO_COMMIT, PACKAGE_ROOT, canonical, decode, encode

from mailroom_dojo import classification
from mailroom_dojo.classification import ClassLabelSet

CORPUS_PATH = PACKAGE_ROOT / "tests" / "fixtures" / "golden" / "classification_cases.jsonl"
OLD_SCORES_SHA256 = "42c7f3c667e526e9e7db39df66470a37ecfdae0eec313c60c98f60ec63ffc480"
OLD_INPUTS_SHA256 = "59292c3564116f87e770bd2e5ed504808f2c4c09caba36a28bbd42387523d580"


@cache
def corpus() -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """The corpus header and its cases."""
    lines = CORPUS_PATH.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0])["header"], tuple(json.loads(line) for line in lines[1:])


def _adapters(label_set: ClassLabelSet) -> dict[str, Callable[[dict[str, Any]], Any]]:
    """Map each corpus function name to a call of the new API with the given label set."""
    return {
        "normalize_label": lambda args: classification.normalize_label(args["value"], label_set),
        "exact_match": lambda args: classification.exact_match(args["output"], args["expected"], label_set),
        "failure": lambda args: classification.failure(args["output"], args["expected"]),
        "accuracy": lambda args: classification.accuracy(args["expected"], args["predicted"], label_set),
        "fbeta": lambda args: classification.fbeta(args["precision"], args["recall"], beta=args["beta"]),
        "per_class_stats": lambda args: classification.per_class_stats(args["expected"], args["predicted"], label_set),
        "macro_accuracy": lambda args: classification.macro_accuracy(args["expected"], args["predicted"], label_set),
        "confusion_matrix": lambda args: classification.confusion_matrix(
            args["expected"], args["predicted"], label_set, labels=args.get("labels")
        ),
        "binary_metrics": lambda args: classification.binary_metrics(
            args["expected"], args["predicted"], args["positive"], label_set=label_set,
            normalize=args.get("normalize", True),
        ),
        "macro_prf": lambda args: classification.macro_prf(
            args["expected"], args["predicted"], label_set, normalize=args.get("normalize", True)
        ),
        "confusion_accuracy": lambda args: classification.confusion_accuracy(args["matrix"]),
        "top_confusions": lambda args: classification.top_confusions(args["matrix"], args["labels"], k=args["k"]),
        "class_distribution": lambda args: classification.class_distribution(args["labels"], label_set),
        "require_equal_lengths": lambda args: classification.require_equal_lengths(args["expected"], args["predicted"]),
    }


def replay(case: dict[str, Any], label_set: ClassLabelSet) -> dict[str, Any]:
    """Run one case through the new API and encode its outcome like the corpus does."""
    try:
        result = _adapters(label_set)[case["function"]](decode(case["args"]))
    except Exception as error:  # the exception is the outcome being compared
        return {"raises": type(error).__name__, "message": str(error)}
    return {"result": encode(result)}


def same_outcome(recorded: dict[str, Any], replayed: dict[str, Any]) -> bool:
    """Equal results as canonical JSON, or the same exception type (and message for the length check)."""
    if "result" in recorded:
        return "result" in replayed and canonical(recorded["result"]) == canonical(replayed["result"])
    if replayed.get("raises") != recorded["raises"]:
        return False
    if recorded["message"].startswith("expected length"):
        return replayed["message"] == recorded["message"]
    return True


def test_the_corpus_header_names_the_old_dojo_and_its_baseline():
    """The corpus was produced from the old commit and version this port targets."""
    header, cases = corpus()
    assert header["old_commit"] == OLD_DOJO_COMMIT
    assert header["old_version"] == "0.30.1"
    assert header["old_scores_json_sha256"] == OLD_SCORES_SHA256
    assert header["old_inputs_json_sha256"] == OLD_INPUTS_SHA256
    assert header["cases"] == len(cases)
    assert dict(Counter(case["function"] for case in cases)) == header["cases_by_function"]
    assert {case["source"] for case in cases} == {"old_tests", "generated"}
    assert len({case["id"] for case in cases}) == len(cases)


def test_the_corpus_covers_every_function_and_the_failure_states():
    """Every public classification function has cases, including exceptions and error rows."""
    _, cases = corpus()
    functions = {case["function"] for case in cases}
    assert functions == set(_adapters(None))
    raised = Counter(case["outcome"]["raises"] for case in cases if "raises" in case["outcome"])
    assert raised["ValueError"] >= 30, raised
    assert raised["IndexError"] >= 1, raised
    error_rows = [case for case in cases if "ERROR: " in json.dumps(case["args"])]
    assert len(error_rows) >= 100


@pytest.mark.parametrize("function", sorted(json.loads(CORPUS_PATH.read_text().splitlines()[0])["header"]["cases_by_function"]))
def test_every_case_gives_the_old_output(function, mailroom_profile):
    """Replay every case of one function with the mailroom profile; list every difference."""
    label_set = ClassLabelSet.from_profile(mailroom_profile)
    _, cases = corpus()
    selected = [case for case in cases if case["function"] == function]
    differences = []
    for case in selected:
        replayed = replay(case, label_set)
        if not same_outcome(case["outcome"], replayed):
            differences.append((case["id"], case["args"], case["outcome"], replayed))
    assert selected
    assert not differences, f"{len(differences)} of {len(selected)} differ, first: {differences[:3]}"


def test_a_wrong_answer_is_detected(mailroom_profile):
    """The comparison is not vacuous: a changed result, a missing exception and 1 vs 1.0 all differ."""
    label_set = ClassLabelSet.from_profile(mailroom_profile)
    case = {"function": "accuracy", "args": {"expected": ["contract"], "predicted": ["contract"]}}
    assert replay(case, label_set) == {"result": 1.0}
    assert not same_outcome({"result": 1}, {"result": 1.0})
    assert not same_outcome({"result": 0.5}, {"result": 1.0})
    assert not same_outcome({"raises": "ValueError", "message": "x"}, {"result": 0.0})
    assert not same_outcome(
        {"raises": "ValueError", "message": "expected length 2 != predicted length 1"},
        {"raises": "ValueError", "message": "expected length 1 != predicted length 2"},
    )
