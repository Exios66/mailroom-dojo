"""The classification layer gives exactly the old Dojo's output on a frozen differential corpus.

``fixtures/golden/classification_cases.jsonl`` holds every classification call the old
Dojo's ``test_classification.py`` and ``test_paired_cardinality.py`` made, plus seeded
generated sorter outputs (JSON objects with ``doc_type``, fenced and quoted text, mixed
case, near-miss labels, error rows, None, numbers, unicode), paired label lists, raw
binary flags, F-beta edge values and length mismatches, each with the old output at
commit 23f8234c. The new functions run with the mailroom-dataset profile's label set
and must give the same canonical JSON or raise the same exception; a difference must
be listed in ``intended_differences.json``.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Callable

import pytest
from dojo_testkit import OLD_DOJO_COMMIT, decode
from golden_replay import (
    CLASSIFICATION_FIXTURE,
    GOLDEN_DIRECTORY,
    OLD_INPUTS_SHA256,
    OLD_SCORES_SHA256,
    check_against_ledger,
    jsonl_corpus,
    outcome_of,
    same_outcome,
)

from mailroom_dojo import classification
from mailroom_dojo.classification import ClassLabelSet


def _adapters(label_set: ClassLabelSet | None) -> dict[str, Callable[[dict[str, Any]], Any]]:
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
    return outcome_of(lambda: _adapters(label_set)[case["function"]](decode(case["args"])))


def test_the_corpus_header_names_the_old_dojo_and_its_baseline():
    """The corpus was produced from the old commit and version this port targets."""
    header, cases = jsonl_corpus(CLASSIFICATION_FIXTURE)
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
    _, cases = jsonl_corpus(CLASSIFICATION_FIXTURE)
    assert {case["function"] for case in cases} == set(_adapters(None))
    raised = Counter(case["outcome"]["raises"] for case in cases if "raises" in case["outcome"])
    assert raised["ValueError"] >= 30, raised
    assert raised["IndexError"] >= 1, raised
    assert sum(1 for case in cases if "ERROR: " in json.dumps(case["args"])) >= 100


FUNCTIONS = sorted(json.loads((GOLDEN_DIRECTORY / CLASSIFICATION_FIXTURE).read_text().splitlines()[0])["header"][
    "cases_by_function"])


@pytest.mark.parametrize("function", FUNCTIONS)
def test_every_case_gives_the_old_output(function, mailroom_profile):
    """Replay every case of one function with the mailroom profile; differences must be in the ledger."""
    label_set = ClassLabelSet.from_profile(mailroom_profile)
    _, cases = jsonl_corpus(CLASSIFICATION_FIXTURE)
    selected = [case for case in cases if case["function"] == function]
    assert selected
    problems = check_against_ledger(
        CLASSIFICATION_FIXTURE,
        [(case["id"], function, case["outcome"], replay(case, label_set)) for case in selected],
    )
    assert not problems, f"{len(problems)} problems of {len(selected)} cases, first: {problems[:3]}"


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
