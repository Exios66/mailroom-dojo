"""Golden parity: every recorded old-Dojo boundary call gives the identical result through the new package.

``fixtures/golden/old_dojo_boundary_calls.json`` records each call that crossed into
the old in-scope scoring modules while llm-dojo-scoring 0.30.1 regenerated its golden
baseline (``scores.json``), with arguments and result. Each call is replayed here
through the new API with the mailroom-dataset profile and compared as canonical JSON.
The 16 ``score_extraction`` results are also tied to the old ``scores.json``: each
recorded ``field_scores`` must hash to the value recorded for the ``scores.json``
paths where it appears, and with an old checkout available (``OLD_DOJO_CHECKOUT``)
the paths are read from the real file. Differences must be listed in
``intended_differences.json``; an unlisted or stale entry fails.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import pytest
from dojo_testkit import OLD_DOJO_COMMIT, canonical, decode
from golden_replay import (
    BOUNDARY_FIXTURE,
    CLASSIFICATION_FIXTURE,
    FIXTURES,
    HUB_ROW_FIXTURE,
    OLD_INPUTS_SHA256,
    OLD_SCORES_SHA256,
    OLD_VERSION,
    boundary_document,
    check_against_ledger,
    intended_differences,
    jsonl_corpus,
    outcome_of,
)

from mailroom_dojo import classification
from mailroom_dojo.classification import ClassLabelSet
from mailroom_dojo.field_scoring import (
    ActiveVariant,
    active_row_variant,
    is_accepted_unscored_field,
    is_unstated_marker,
    parse_money,
    score_extraction,
    score_field,
    score_money_field,
)
from mailroom_dojo.labels_interface import hub_rows
from mailroom_dojo.profile import ScoringProfile
from mailroom_dojo.values import is_empty_value, parse_json_container

#: Old functions replayed, by the old name recorded in the fixture.
REPLAYED = (
    "accuracy", "binary_metrics", "confusion_matrix", "derive_presence_from_gt", "exact_match",
    "expected_cms_source_table", "fbeta", "gt_presence_map", "is_empty_value", "is_parse_compat_key",
    "is_unstated_cms_marker", "macro_accuracy", "macro_prf", "normalize_field_values", "normalize_label",
    "parse_gt_fields", "parse_json_container", "parse_money", "require_equal_lengths", "score_extraction",
    "score_field", "score_money_field", "scoring_gt_fields", "top_confusions",
)


def _cms_variant(profile: ScoringProfile):
    """The mailroom profile's CMS row variant (the old hard-coded CMS rules)."""
    return profile.require_class("insurance_claim").variants[0]


def adapters(profile: ScoringProfile) -> dict[str, Callable[[dict[str, Any]], Any]]:
    """Map each recorded old function to the new call with the same meaning."""
    label_set = ClassLabelSet.from_profile(profile)
    return {
        "normalize_label": lambda a: classification.normalize_label(a["value"], label_set),
        "exact_match": lambda a: classification.exact_match(a["output"], a["expected"], label_set),
        "accuracy": lambda a: classification.accuracy(a["expected"], a["predicted"], label_set),
        "fbeta": lambda a: classification.fbeta(a["precision"], a["recall"], beta=a["beta"]),
        "macro_accuracy": lambda a: classification.macro_accuracy(a["expected"], a["predicted"], label_set),
        "confusion_matrix": lambda a: classification.confusion_matrix(
            a["expected"], a["predicted"], label_set, labels=a["labels"]),
        "top_confusions": lambda a: classification.top_confusions(a["matrix"], a["labels"], k=a["k"]),
        "macro_prf": lambda a: classification.macro_prf(
            a["expected"], a["predicted"], label_set, normalize=a["normalize"]),
        "binary_metrics": lambda a: classification.binary_metrics(
            a["expected"], a["predicted"], a["positive"], label_set=label_set, normalize=a["normalize"]),
        "require_equal_lengths": lambda a: classification.require_equal_lengths(a["expected"], a["predicted"]),
        "is_empty_value": lambda a: is_empty_value(a["value"]),
        "parse_json_container": lambda a: parse_json_container(a["value"]),
        "parse_money": lambda a: parse_money(a["text"]),
        "score_money_field": lambda a: score_money_field(a["pred"], a["exp"]),
        "score_field": lambda a: score_field(profile, a["field_type"], a["pred"], a["exp"],
                                             recall_coverage=a["partial_gt"]),
        "score_extraction": lambda a: score_extraction(
            profile, a["doc_class"], a["predicted"], a["expected"], field_types=a["field_types"],
            doc_text=a["doc_text"], audit_predicted=a["audit_predicted"], apply_variants=a["cms_aware"]),
        "is_parse_compat_key": lambda a: is_accepted_unscored_field(profile, a["doc_class"], a["key"],
                                                                    a["field_types"]),
        "expected_cms_source_table": lambda a: getattr(
            active_row_variant(profile.class_spec(a["doc_class"]), a["expected"]), "marker_value", None),
        "is_unstated_cms_marker": lambda a: is_unstated_marker(
            ActiveVariant(_cms_variant(profile), a["cms_table"]) if a["cms_table"] is not None else None,
            a["key"], a["predicted"]),
        "parse_gt_fields": lambda a: hub_rows.parse_hub_fields(a["raw"]),
        "gt_presence_map": lambda a: hub_rows.hub_presence_statuses(a["fields"]),
        "normalize_field_values": lambda a: hub_rows.parse_field_values(a["record"]),
        "scoring_gt_fields": lambda a: hub_rows.scoring_fields_from_hub_row(
            a["fields"], profile, field_types=a["field_types"], extra_keys=a["extra_keys"],
            drop_unmapped=a["drop_unmapped"], class_key=a["doc_type"], subclass=a["subclass"]),
        "derive_presence_from_gt": lambda a: hub_rows.presence_expectations_from_hub_row(a["fields"], field=a["field"]),
    }


def _calls(function: str | None = None) -> list[dict[str, Any]]:
    """The recorded boundary calls (of one function)."""
    return [call for call in boundary_document()["calls"] if function in (None, call["function"])]


def _replay(call: dict[str, Any], profile: ScoringProfile) -> dict[str, Any]:
    """Replay one boundary call (arguments decoded from a fresh copy, so a mutating call cannot touch the fixture)."""
    return outcome_of(lambda: adapters(profile)[call["function"]](decode(json.loads(json.dumps(call["args"])))))


# ---------------------------------------------------------------------------
# The fixture is what it claims to be
# ---------------------------------------------------------------------------


def test_the_boundary_header_names_the_old_dojo_and_its_baseline():
    """Old commit, version and the hashes of the old inputs.json and scores.json; counts match the calls."""
    header = boundary_document()["header"]
    assert header["old_commit"] == OLD_DOJO_COMMIT
    assert header["old_version"] == OLD_VERSION
    assert header["old_inputs_json_sha256"] == OLD_INPUTS_SHA256
    assert header["old_scores_json_sha256"] == OLD_SCORES_SHA256
    assert header["regenerated_baseline_identical"] is True
    assert header["calls"] == len(_calls()) == 674
    assert dict(Counter(call["function"] for call in _calls())) == header["calls_by_function"]
    assert [call["index"] for call in _calls()] == list(range(len(_calls())))


def test_every_corpus_names_the_same_old_baseline():
    """The three fixtures were produced from one old checkout."""
    for name in (CLASSIFICATION_FIXTURE, HUB_ROW_FIXTURE):
        header, _ = jsonl_corpus(name)
        assert (header["old_commit"], header["old_version"]) == (OLD_DOJO_COMMIT, OLD_VERSION)
        assert header["old_scores_json_sha256"] == OLD_SCORES_SHA256


def test_every_recorded_function_is_replayed_or_explicitly_left_out():
    """No recorded function is silently skipped: it is replayed, or listed with a reason in golden-parity.md."""
    recorded = set(boundary_document()["header"]["calls_by_function"])
    assert recorded == set(REPLAYED) == adapters_names()


def adapters_names() -> set[str]:
    """The function names the adapter table covers (built without a profile)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(adapters))
    return {key.value for node in ast.walk(tree) if isinstance(node, ast.Dict) for key in node.keys
            if isinstance(key, ast.Constant)}


def test_score_extraction_results_are_tied_to_old_scores_json():
    """Each recorded field_scores hashes to the recorded value, at one or two named scores.json paths."""
    calls = _calls("score_extraction")
    assert len(calls) == 16
    cases = Counter(call["case"] for call in calls)
    assert all(case.startswith("suite:") for case in cases)
    for call in calls:
        field_scores = decode(call["result"])["field_scores"]
        assert hashlib.sha256(canonical(field_scores).encode()).hexdigest() == call["field_scores_sha256"]
        suite = call["case"].removeprefix("suite:")
        assert call["scores_json_paths"], call["case"]
        assert all(path.startswith(f"/suites/{suite}/") and path.endswith("/field_scores")
                   for path in call["scores_json_paths"])


@pytest.mark.skipif(not os.environ.get("OLD_DOJO_CHECKOUT"), reason="set OLD_DOJO_CHECKOUT to an llm-dojo-scoring checkout")
def test_score_extraction_results_sit_in_the_real_old_scores_json():
    """With an old checkout, scores.json has the recorded hash and holds each field_scores at its paths."""
    scores_path = Path(os.environ["OLD_DOJO_CHECKOUT"]) / "tests" / "fixtures" / "golden" / "scores.json"
    raw = scores_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == OLD_SCORES_SHA256
    scores = json.loads(raw)
    for call in _calls("score_extraction"):
        for path in call["scores_json_paths"]:
            node = scores
            for part in path.strip("/").split("/"):
                node = node[int(part)] if isinstance(node, list) else node[part]
            assert canonical(node) == canonical(decode(call["result"])["field_scores"])


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("function", REPLAYED)
def test_every_boundary_call_gives_the_old_result(function, mailroom_profile):
    """Replay every recorded call of one function; any difference must be in the ledger, and no ledger entry may be stale."""
    calls = _calls(function)
    assert calls
    replayed = [(str(call["index"]), function, {"result": call["result"]}, _replay(call, mailroom_profile))
                for call in calls]
    problems = check_against_ledger(BOUNDARY_FIXTURE, replayed)
    assert not problems, f"{len(problems)} problems of {len(calls)} calls; first: {problems[:3]}"


def test_score_extraction_replay_covers_every_specialist_and_auditor(mailroom_profile):
    """The 16 replayed extractions span the five specialists (twice each), four auditors, the arbiter and the audit agent."""
    cases = Counter(call["case"] for call in _calls("score_extraction"))
    specialists = {case for case, count in cases.items() if count == 2}
    assert len(specialists) == 5 and all(case.endswith("_specialist") for case in specialists)
    assert {"suite:arbiter", "suite:audit_agent"} <= set(cases)
    variants = Counter(call["args"]["cms_aware"] for call in _calls("score_extraction"))
    assert variants == {True: 10, False: 6}


def test_a_changed_score_is_caught(mailroom_profile):
    """The replay is not vacuous: a recorded extraction with one field score changed is an unlisted difference."""
    call = json.loads(json.dumps(_calls("score_extraction")[0]))
    result = decode(call["result"])
    first_field = sorted(result["field_scores"])[0]
    result["field_scores"][first_field] = round(1.0 - result["field_scores"][first_field], 4) or 0.5
    replayed = _replay(call, mailroom_profile)
    problems = check_against_ledger(BOUNDARY_FIXTURE, [(str(call["index"]), "score_extraction",
                                                        {"result": result}, replayed)])
    assert len(problems) == 1 and problems[0].startswith("unlisted difference")


# ---------------------------------------------------------------------------
# The intended-difference ledger
# ---------------------------------------------------------------------------


def test_every_ledger_entry_names_a_real_case_with_a_reason():
    """Each entry names a fixture and an existing case of it, the function, a reason and the new outcome."""
    known = {
        BOUNDARY_FIXTURE: {str(call["index"]): call["function"] for call in _calls()},
        CLASSIFICATION_FIXTURE: {case["id"]: case["function"] for case in jsonl_corpus(CLASSIFICATION_FIXTURE)[1]},
        HUB_ROW_FIXTURE: {case["id"]: case["function"] for case in jsonl_corpus(HUB_ROW_FIXTURE)[1]},
    }
    for (fixture, case), entry in intended_differences().items():
        assert fixture in FIXTURES
        assert known[fixture].get(case) == entry["function"], (fixture, case)
        assert entry["reason"].strip()
        assert set(entry["new_outcome"]) <= {"result", "raises", "message"} and entry["new_outcome"]


def _ledger_with(monkeypatch, entries):
    """Swap the ledger for ``entries`` for one test."""
    import golden_replay

    monkeypatch.setattr(golden_replay, "intended_differences", lambda: {
        (entry["fixture"], entry["case"]): entry for entry in entries})


def test_the_ledger_rejects_stale_and_changed_entries(monkeypatch):
    """A listed difference that no longer occurs, or that now gives another value, is a failure."""
    base = {"fixture": BOUNDARY_FIXTURE, "function": "fbeta", "reason": "test"}
    _ledger_with(monkeypatch, [
        {**base, "case": "1", "new_outcome": {"result": 0.5}},
        {**base, "case": "2", "new_outcome": {"result": 0.25}},
        {**base, "case": "3", "new_outcome": {"result": 0.5}},
        {**base, "case": "99", "new_outcome": {"result": 0.5}},
    ])
    problems = check_against_ledger(BOUNDARY_FIXTURE, [
        ("1", "fbeta", {"result": 0.5}, {"result": 0.5}),
        ("2", "fbeta", {"result": 0.5}, {"result": 0.75}),
        ("3", "fbeta", {"result": 0.4}, {"result": 0.5}),
    ])
    assert [problem.split(" in ")[0] for problem in problems] == [
        "stale intended difference", "intended difference", "intended difference"]
    assert "changed again" in problems[1] and "names no replayed case" in problems[2]
