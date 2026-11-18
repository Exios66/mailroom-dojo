"""Every extraction layer gives exactly the old Dojo's output on the frozen differential corpus.

The corpus (``fixtures/differential/extraction_cases.jsonl``) holds the old golden
run's ``score_extraction`` calls (tied to the old ``scores.json``), the calls the old
Dojo's tests make into the extraction layers, and seeded generated inputs (lists,
audits with document text, presence, trace shapes, CMS rows, every mailroom class,
unknown and retired classes, hostile values, non-default settings, fake embedding
backends), each with the old output computed at commit 23f8234c. A case's
``settings`` are old settings that differ from the defaults; they are built into a
variant of the mailroom profile here, so the new code reads them from its profile.
Outputs are compared as canonical JSON.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from functools import cache
from typing import Any, Callable

import pytest
from dojo_testkit import (
    OLD_DOJO_COMMIT,
    PACKAGE_ROOT,
    FakeSimilarity,
    canonical,
    decode,
    encode,
    mailroom_profile_mapping,
)

from mailroom_dojo.field_scoring import audits, category_presence, entity_lists, extraction, row_variants, trace_capture
from mailroom_dojo.field_scoring.scorer_registry import FieldScorerRegistry
from mailroom_dojo.field_scoring.similarity_backend import EmbeddingRescue
from mailroom_dojo.normalization.dates import DateParts
from mailroom_dojo.profile import ScoringProfile, load_scoring_profile

EXTRACTION_CORPUS_PATH = PACKAGE_ROOT / "tests" / "fixtures" / "differential" / "extraction_cases.jsonl"
GOLDEN_CLASSIFICATION_CORPUS_PATH = PACKAGE_ROOT / "tests" / "fixtures" / "golden" / "classification_cases.jsonl"

#: Where each old setting lives in a scoring profile.
SETTING_PATHS: dict[str, tuple[str, str]] = {
    "ambiguous_band": ("thresholds", "ambiguous_band"),
    "bipartite_match_threshold": ("thresholds", "list_match_threshold"),
    "type_bands": ("thresholds", "type_bands"),
    "partial_gt_fields": ("field_name_defaults", "recall_coverage"),
    "containment_fields": ("field_name_defaults", "containment"),
    "verification_enabled": ("audit", "enabled"),
    "verification_token_coverage": ("audit", "token_coverage"),
    "embedding_enabled": ("embedding", "enabled"),
    "embedding_rescue_below": ("embedding", "rescue_below"),
    "presence_embedding_threshold": ("presence", "embedding_threshold"),
    "reasoning_routes_presence": ("presence", "route_by_reasoning"),
    "capture_confidence": ("trace_capture", "capture_confidence"),
    "capture_reasoning": ("trace_capture", "capture_reasoning"),
    "confidence_min": ("trace_capture", "confidence_min"),
    "confidence_band": ("trace_capture", "confidence_band"),
    "compute_calibration_error": ("trace_capture", "compute_calibration_error"),
    "missing_confidence": ("trace_capture", "missing_confidence"),
}


@cache
def extraction_corpus() -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Return the corpus header and its cases (read once per process)."""
    lines = EXTRACTION_CORPUS_PATH.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0])["header"], tuple(json.loads(line) for line in lines[1:])


@cache
def profile_for(settings_text: str) -> ScoringProfile:
    """The mailroom profile with a case's old settings applied (cached per distinct settings)."""
    mapping = mailroom_profile_mapping()
    for name, value in json.loads(settings_text).items():
        section, key = SETTING_PATHS[name]
        mapping.setdefault(section, {})[key] = value
    return load_scoring_profile(mapping)


def _similarity(args: dict[str, Any]) -> FakeSimilarity | None:
    """The fake backend a case's ``embedding`` argument describes."""
    spec = args.get("embedding")
    return FakeSimilarity(spec["$similarity"]) if spec is not None else None


def _rescue(args: dict[str, Any], profile: ScoringProfile) -> EmbeddingRescue | None:
    """The embedding rescue a case's ``embedding`` argument describes, under its profile."""
    return EmbeddingRescue.from_profile(profile, _similarity(args))


def _score_extraction(args: dict[str, Any], profile: ScoringProfile) -> dict[str, Any]:
    """score_extraction with the old argument names; unknown types score as name, as they did."""
    result = extraction.score_extraction(
        profile,
        args["doc_class"],
        args["predicted"],
        args["expected"],
        field_types=args["field_types"],
        doc_text=args.get("doc_text"),
        audit_predicted=args.get("audit_predicted"),
        apply_variants=args.get("cms_aware", True),
        similarity=_similarity(args),
        unknown_field_types="score_as_name",
    )
    return {
        "to_dict": result.to_dict(),
        "audit_support": result.audit_support,
        "needs_judge_review": result.needs_judge_review,
        "overall_verified_precision": result.overall_verified_precision,
    }


def _score_entity_list(args: dict[str, Any], profile: ScoringProfile) -> dict[str, Any]:
    """score_entity_list with the old argument names."""
    result = entity_lists.score_entity_list(
        profile,
        args["element_type"],
        args["pred"],
        args["exp"],
        recall_coverage=args.get("partial_gt", False),
        rescue=_rescue(args, profile),
        unknown_type_as="name",
    )
    return {"to_dict": result.to_dict(), "score": result.score}


def _score_field(args: dict[str, Any], profile: ScoringProfile) -> Any:
    """score_field with the old argument names; a list result as its dict and score."""
    result = extraction.score_field(
        profile, args["field_type"], args["pred"], args["exp"],
        recall_coverage=args.get("partial_gt", False), unknown_type_as="name",
    )
    if isinstance(result, entity_lists.EntityListScore):
        return {"list": result.to_dict(), "score": result.score}
    return result


def _date_grounded(args: dict[str, Any], profile: ScoringProfile) -> bool:
    """date_grounded_in_document with a tagged date-parts argument rebuilt."""
    predicted_date = args["pred_date"]
    if isinstance(predicted_date, dict):
        predicted_date = DateParts(*predicted_date["$date_parts"])
    return audits.date_grounded_in_document(predicted_date, args["doc_text"])


def _element_similarity(args: dict[str, Any], profile: ScoringProfile) -> float:
    """element_similarity with the element type's scorer from the profile's registry."""
    scorer = entity_lists.element_scorer(FieldScorerRegistry.for_profile(profile), args["element_type"], "name")
    return entity_lists.element_similarity(profile, args["element_type"], args["item"], args["exp"], scorer=scorer)


def _presence(args: dict[str, Any], profile: ScoringProfile) -> Any:
    """score_category_presence with the old default field."""
    similarity = category_presence.PresenceSimilarity.from_profile(profile, _similarity(args))
    return category_presence.score_category_presence(
        profile, args["predicted"], args["presence_expectations"], similarity=similarity, default_field="cuad_clauses"
    )


def _expected_marker(args: dict[str, Any], profile: ScoringProfile) -> str | None:
    """The marker value of the active row variant (the old expected_cms_source_table)."""
    active = row_variants.active_row_variant(profile.class_spec(args["doc_class"]), args["expected"])
    return active.marker_value if active is not None else None


def _unstated_marker(args: dict[str, Any], profile: ScoringProfile) -> bool:
    """is_unstated_marker for the insurance CMS variant marked by ``cms_table``."""
    variant = profile.require_class("insurance_claim").variants[0]
    active = None if args["cms_table"] is None else row_variants.ActiveVariant(variant, args["cms_table"])
    return row_variants.is_unstated_marker(active, args["key"], args["predicted"])


ADAPTERS: dict[str, Callable[[dict[str, Any], ScoringProfile], Any]] = {
    "score_extraction": _score_extraction,
    "score_entity_list": _score_entity_list,
    "score_field": _score_field,
    "verify_list_items": lambda args, profile: audits.verify_list_items(
        args["items"], args["doc_text"],
        profile.audit.token_coverage if args.get("token_coverage") is None else args["token_coverage"],
    ),
    "audit_list_field": lambda args, profile: audits.audit_list_field(
        profile, args["element_type"], args["pred_items"], args["exp_items"], args["doc_text"],
        token_coverage=args.get("token_coverage"), unknown_type_as="name",
    ),
    "audit_scalar_field": lambda args, profile: audits.audit_scalar_field(
        profile, args["field_type"], args["pred"], args["exp"], args["doc_text"], unknown_type_as="name"
    ),
    "verification_tokens": lambda args, profile: audits.verification_tokens(args["value"], args["field_type"]),
    "date_grounded_in_document": _date_grounded,
    "element_similarity": _element_similarity,
    "split_clause_spans": lambda args, profile: category_presence.split_clause_spans(args["text"]),
    "disaggregate_clause_spans": lambda args, profile: category_presence.disaggregate_clause_spans(
        args["items"], *([args["min_span_tokens"]] if "min_span_tokens" in args else [])
    ),
    "score_category_presence": _presence,
    "field_is_ambiguous": lambda args, profile: extraction.field_is_ambiguous(profile, args["field_type"], args["score"]),
    "expected_cms_source_table": _expected_marker,
    "is_unstated_cms_marker": _unstated_marker,
    "is_parse_compat_key": lambda args, profile: extraction.is_accepted_unscored_field(
        profile, args["doc_class"], args["key"], args.get("field_types")
    ),
    "field_audit_support": lambda args, profile: audits.field_audit_support(args["audits"]),
    "capture_trace": lambda args, profile: trace_capture.capture_trace(
        args["predicted"], profile.trace_capture, correctness=args.get("correctness")
    ),
    "parse_confidence": lambda args, profile: trace_capture.parse_confidence(args["value"]),
    "parse_reasoning": lambda args, profile: trace_capture.parse_reasoning(args["value"]),
    "confidence_gate": lambda args, profile: trace_capture.confidence_gate(args["confidence"], profile.trace_capture),
    "confidence_calibration_error": lambda args, profile: trace_capture.confidence_calibration_error(
        args["confidence"], args["correctness"]
    ),
}


def new_outcome(case: dict[str, Any]) -> dict[str, Any]:
    """Run one case through the new code and encode its outcome the way the corpus does."""
    profile = profile_for(json.dumps(case.get("settings") or {}, sort_keys=True))
    try:
        return {"result": encode(ADAPTERS[case["function"]](decode(case["args"]), profile))}
    except Exception as error:  # the exception type is the compared outcome
        return {"raises": type(error).__name__}


def test_corpus_header_names_the_old_commit_and_matches_its_content():
    """The corpus states where it came from, and its counts agree with its lines."""
    header, cases = extraction_corpus()
    assert header["old_commit"] == OLD_DOJO_COMMIT
    assert header["old_version"] == "0.30.1"
    assert header["cases"] == len(cases)
    assert header["cases_by_function"] == dict(sorted(Counter(case["function"] for case in cases).items()))
    assert header["cases_by_source"] == dict(sorted(Counter(case["source"] for case in cases).items()))
    assert len({case["id"] for case in cases}) == len(cases)


def test_every_corpus_function_has_an_adapter_and_every_adapter_has_cases():
    """No corpus function is silently skipped, and no adapter is left untested."""
    _, cases = extraction_corpus()
    assert {case["function"] for case in cases} == set(ADAPTERS)


def test_golden_cases_are_tied_to_the_old_golden_baseline():
    """The golden cases carry the old scores.json hash, their paths in it, and field scores that hash to the record."""
    header, cases = extraction_corpus()
    golden = [case for case in cases if case["source"] == "golden"]
    assert header["golden_calls"] == 16
    assert len(golden) == 11
    names = {case["golden"]["case"] for case in golden}
    assert {"suite:arbiter", "suite:audit_agent", "suite:insurance_claims_specialist"} <= names
    for case in golden:
        field_scores = case["outcome"]["result"]["to_dict"]["field_scores"]
        text = json.dumps(field_scores, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == case["golden"]["field_scores_sha256"]
        assert case["golden"]["scores_json_paths"], case["golden"]["case"]
    assert len(header["golden_scores_json_sha256"]) == 64


def test_golden_hashes_agree_with_the_classification_corpus():
    """Both corpora name the same old golden inputs and scores, so they come from one baseline."""
    if not GOLDEN_CLASSIFICATION_CORPUS_PATH.exists():
        pytest.skip("classification corpus not present")
    classification = json.loads(GOLDEN_CLASSIFICATION_CORPUS_PATH.read_text(encoding="utf-8").splitlines()[0])["header"]
    header, _ = extraction_corpus()
    assert header["golden_scores_json_sha256"] == classification["old_scores_json_sha256"]
    assert header["golden_inputs_json_sha256"] == classification["old_inputs_json_sha256"]


@pytest.mark.parametrize("source", ["golden", "old_tests", "generated"])
def test_corpus_holds_each_source(source):
    """All three input sources are present, so none was lost on a refresh."""
    _, cases = extraction_corpus()
    minimum = {"golden": 11, "old_tests": 1000, "generated": 4000}[source]
    assert sum(case["source"] == source for case in cases) >= minimum


@pytest.mark.parametrize("function", sorted(ADAPTERS))
def test_new_code_reproduces_every_old_output(function):
    """Each case's new outcome is identical to the old one (exact floats, same types)."""
    _, cases = extraction_corpus()
    mismatches = []
    for case in cases:
        if case["function"] != function:
            continue
        new = new_outcome(case)
        if canonical(new) != canonical(case["outcome"]):
            mismatches.append((case["id"], case["source"], case["settings"], case["outcome"], new))
    assert not mismatches, f"{len(mismatches)} mismatches, first: {mismatches[:3]}"
