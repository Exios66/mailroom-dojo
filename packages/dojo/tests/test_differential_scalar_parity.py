"""Every scalar layer gives exactly the old Dojo's output on the frozen differential corpus.

The corpus (``fixtures/differential/scalar_cases.jsonl``) holds the inputs the old
Dojo's direct tests used plus seeded generated inputs (dates in many formats, money,
names, CMS identifiers, every label surface, blanks, unicode, hostile types and
embedding rescue with fake backends), each with the old output computed at commit
23f8234c. Outputs are compared as canonical JSON, so ``1`` and ``1.0``, and ``None``
and ``0.0``, are different answers.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest

from dojo_testkit import OLD_DOJO_COMMIT, FakeSimilarity, canonical, decode, encode, scalar_corpus
from mailroom_dojo.field_scoring import scalar_scorers
from mailroom_dojo.field_scoring.plugins import cms_identifiers
from mailroom_dojo.field_scoring.scorer_registry import heuristic_field_type, is_entity_list
from mailroom_dojo.field_scoring.similarity_backend import EmbeddingRescue
from mailroom_dojo.normalization import dates, money, text
from mailroom_dojo.profile import LabelVocabulary, ScoringProfile
from mailroom_dojo.values import emptiness

#: The old Dojo's embedding rescue threshold (``embedding_rescue_below``).
OLD_RESCUE_BELOW = 0.7


def _rescue(spec: Any) -> EmbeddingRescue | None:
    """Build the rescue a corpus case's ``embedding`` argument describes."""
    if spec is None:
        return None
    return EmbeddingRescue(FakeSimilarity(spec["$similarity"]), OLD_RESCUE_BELOW)


def _scorer(function: Callable[..., float]) -> Callable[[dict[str, Any], ScoringProfile], Any]:
    """Adapt a new scalar scorer to a corpus case's ``pred``/``exp``/``embedding`` arguments."""
    return lambda args, profile: function(args["pred"], args["exp"], _rescue(args.get("embedding")))


def _normalize_intent(args: dict[str, Any], profile: ScoringProfile) -> str:
    """The old normalize_intent: the class's intent vocabulary, looked up by the exact class key."""
    vocabulary = profile.label_vocabulary(args["doc_type"], "intent") if args["doc_type"] else None
    return vocabulary.fold(args["value"]) if vocabulary is not None else ""


ADAPTERS: dict[str, Callable[[dict[str, Any], ScoringProfile], Any]] = {
    "normalize_text": lambda args, profile: text.normalize_text(args["text"]),
    "tokenize": lambda args, profile: text.tokenize(args["text"]),
    "containment_tokens": lambda args, profile: text.containment_tokens(args["text"]),
    "sequence_ratio": lambda args, profile: text.sequence_ratio(args["a"], args["b"]),
    "token_set_ratio": lambda args, profile: text.token_set_ratio(args["a"], args["b"]),
    "token_f1": lambda args, profile: text.token_f1(args["pred"], args["exp"]),
    "jaro_winkler": lambda args, profile: text.jaro_winkler(args["a"], args["b"]),
    "parse_date_parts": lambda args, profile: dates.parse_date_parts(args["text"]),
    "parse_date": lambda args, profile: dates.parse_date(args["text"]),
    "parse_money": lambda args, profile: money.parse_money(args["text"]),
    "stated_years": lambda args, profile: dates.stated_years(args["text"]),
    "integral_float_as_text": lambda args, profile: dates.integral_float_as_text(args["value"]),
    "expected_date_is_relative": lambda args, profile: dates.expected_date_is_relative(args["exp"]),
    "expected_date_is_null": lambda args, profile: dates.expected_date_is_null(args["exp"]),
    "date_is_blank_or_bare_label": lambda args, profile: dates.date_is_blank_or_bare_label(args["exp"]),
    "date_phrase_contained": lambda args, profile: dates.date_phrase_contained(
        args["container"], args["target"]
    ),
    "contains_phrase": lambda args, profile: dates.contains_phrase(args["haystack"], args["phrase"]),
    "score_id_field": _scorer(scalar_scorers.score_id_field),
    "score_money_field": _scorer(scalar_scorers.score_money_field),
    "score_name_field": _scorer(scalar_scorers.score_name_field),
    "score_label_field": _scorer(scalar_scorers.score_label_field),
    "score_free_text_field": _scorer(scalar_scorers.score_free_text_field),
    "score_containment_field": _scorer(scalar_scorers.score_containment_field),
    "score_date_field": _scorer(scalar_scorers.score_date_field),
    "date_match_similarity": _scorer(scalar_scorers.date_match_similarity),
    "date_year_unverified": lambda args, profile: scalar_scorers.date_year_unverified(
        args["pred"], args["exp"]
    ),
    "cms_document_signature": lambda args, profile: cms_identifiers.cms_document_signature(args["text"]),
    "score_cms_doc_ref": _scorer(cms_identifiers.score_cms_doc_ref),
    "heuristic_field_type": lambda args, profile: heuristic_field_type(args["field_name"], args["value"]),
    "is_entity_list": lambda args, profile: is_entity_list(args["field_type"]),
    "parse_json_container": lambda args, profile: emptiness.parse_json_container(args["value"]),
    "is_empty_value": lambda args, profile: emptiness.is_empty_value(args["value"]),
    "canonical_label": lambda args, profile: profile.canonical_label(
        args["doc_type"], args["field"], args["value"]
    ),
    "normalize_intent": _normalize_intent,
    "normalize_subtype": lambda args, profile: profile.require_class("contract").subclasses.normalize(
        args["value"]
    ),
    "normalize_doc_subclass": lambda args, profile: LabelVocabulary(
        tokens=tuple(sorted(args["allowed"])), fold_method="allowed_set"
    ).normalize(args["value"]),
    "normalize_corpus_subclass": lambda args, profile: profile.normalize_subclass(
        args["doc_type"], args["value"]
    ),
}


def _outcome(
    adapter: Callable[[dict[str, Any], ScoringProfile], Any], args: Any, profile: ScoringProfile
) -> dict:
    """Run one case through the new code and encode its outcome the way the corpus does."""
    try:
        return {"result": encode(adapter(decode(args), profile))}
    except Exception as error:  # the exception type is the compared outcome
        return {"raises": type(error).__name__}


def test_corpus_header_names_the_old_commit_and_matches_its_content():
    """The corpus states where it came from, and its counts agree with its lines."""
    header, cases = scalar_corpus()
    assert header["old_commit"] == OLD_DOJO_COMMIT
    assert header["old_version"] == "0.30.1"
    assert header["cases"] == len(cases)
    assert header["cases_by_function"] == dict(sorted(Counter(case["function"] for case in cases).items()))
    assert len({case["id"] for case in cases}) == len(cases)


def test_every_corpus_function_has_an_adapter_and_every_adapter_has_cases():
    """No corpus function is silently skipped, and no adapter is left untested."""
    _, cases = scalar_corpus()
    assert {case["function"] for case in cases} == set(ADAPTERS)


@pytest.mark.parametrize("function", sorted(ADAPTERS))
def test_new_code_reproduces_every_old_output(function, mailroom_profile):
    """Each case's new outcome is identical to the old one (exact floats, same types)."""
    _, cases = scalar_corpus()
    adapter = ADAPTERS[function]
    mismatches = []
    for case in cases:
        if case["function"] != function:
            continue
        new = _outcome(adapter, case["args"], mailroom_profile)
        if canonical(new) != canonical(case["outcome"]):
            mismatches.append((case["id"], case["args"], case["outcome"], new))
    assert not mismatches, f"{len(mismatches)} mismatches, first: {mismatches[:5]}"


def test_corpus_exercises_the_generated_and_captured_sources():
    """Both input sources are present, so neither half of the corpus was lost on a refresh."""
    _, cases = scalar_corpus()
    sources = Counter(case["source"] for case in cases)
    assert sources["old_tests"] > 1000
    assert sources["generated"] > 10000
