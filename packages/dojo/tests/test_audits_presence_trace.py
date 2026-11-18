"""Factuality audits, category presence and trace capture: documented behaviour, faults and invariants."""

from __future__ import annotations

import datetime
import logging
from functools import cache

import pytest
from dojo_testkit import MAILROOM_PROFILE_PATH, FakeSimilarity, mailroom_profile_mapping
from hypothesis import given
from hypothesis import strategies as st

from mailroom_dojo.field_scoring import category_presence
from mailroom_dojo.field_scoring.audits import (
    audit_list_field,
    audit_scalar_field,
    date_grounded_in_document,
    field_audit_support,
    verification_tokens,
    verify_list_items,
)
from mailroom_dojo.field_scoring.category_presence import (
    PresenceSimilarity,
    disaggregate_clause_spans,
    score_category_presence,
)
from mailroom_dojo.field_scoring.trace_capture import (
    TRACE_KEYS,
    capture_trace,
    confidence_gate,
    parse_confidence,
    parse_reasoning,
)
from mailroom_dojo.normalization.dates import DateParts
from mailroom_dojo.normalization.text import tokenize
from mailroom_dojo.profile import TraceCaptureOptions, load_scoring_profile


@cache
def _mailroom():
    """The mailroom profile, loaded once (hypothesis examples cannot take function-scoped fixtures)."""
    return load_scoring_profile(MAILROOM_PROFILE_PATH)


def _mailroom_with(**sections: dict):
    """The mailroom profile with some sections updated."""
    mapping = mailroom_profile_mapping()
    for name, values in sections.items():
        mapping.setdefault(name, {}).update(values)
    return load_scoring_profile(mapping)


# --- audits ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("predicted, document, grounded", [
    ("2014-08-18", "signed on the 18th day of August, 2014 by both parties", True),
    ("2014-08-18", "signed 08/18/2014", True),
    ("2014-08-18", "signed 18.08.2014", True),  # day first only when the first number cannot be a month
    ("2014-07-08", "signed 08.07.2014", False),  # otherwise numeric dates read month first
    ("August 18", "dated August 18, 2014", True),
    ("2014-08-19", "dated August 18, 2014", False),
    ("August 2014", "the 18t h day of August, 2014", True),
    ("2015", "dated August 18, 2014", False),
    ("not a date", "dated August 18, 2014", False),
])
def test_a_date_is_grounded_only_by_an_agreeing_document_date(mailroom_profile, predicted, document, grounded):
    """Date audits compare stated components across formats and never invent a missing one."""
    audit = audit_scalar_field(mailroom_profile, "date", predicted, None, document)
    assert audit["verified_in_doc"] == int(grounded)
    assert audit["true_items"] == int(grounded) and audit["hallucinated"] == int(not grounded)


def test_date_grounding_accepts_full_and_partial_dates():
    """A full date and its parts give the same answer; no document or no date is never grounded."""
    text = "Effective April 1, 2006."
    assert date_grounded_in_document(datetime.date(2006, 4, 1), text)
    assert date_grounded_in_document(DateParts(None, 4, 1), text)
    assert not date_grounded_in_document(DateParts(2006, 4, 2), text)
    assert not date_grounded_in_document(None, text) and not date_grounded_in_document(datetime.date(2006, 4, 1), "")


def test_scalar_audits_match_ground_truth_or_the_document(mailroom_profile):
    """True when it matches the label or appears in the text; blank predictions are not audited."""
    matched = audit_scalar_field(mailroom_profile, "name", "Acme Corp", "ACME Corporation", "unrelated")
    assert (matched["matched_gt"], matched["verified_in_doc"], matched["true_items"]) == (1, 0, 1)
    invented = audit_scalar_field(mailroom_profile, "name", "Ghost Holdings", "Acme", "Acme signs here")
    assert invented["hallucination_rate"] == 1.0
    assert audit_scalar_field(mailroom_profile, "name", "  ", "Acme", "text")["n_predicted"] == 0
    assert audit_scalar_field(mailroom_profile, "name", "", "Acme", None)["doc_verification"] is False
    assert verification_tokens("2014-08-18", "date") == {"august", "18", "2014"}


def test_list_audits_count_each_item_and_round_rates(mailroom_profile):
    """Each predicted item is true by label or by document; rates are rounded to four places."""
    audit = audit_list_field(mailroom_profile, "name", ["Acme Corp", "Beta LLC", "Ghost Co"], ["ACME Corporation"],
                             "Beta LLC signs.")
    assert audit == {"n_predicted": 3, "matched_gt": 1, "verified_in_doc": 1, "true_items": 2,
                     "verified_precision": 0.6667, "hallucinated": 1, "hallucination_rate": 0.3333,
                     "doc_verification": True}
    assert audit_list_field(mailroom_profile, "name", [], ["Acme"], "text")["doc_verification"] is False


def test_audit_coverage_comes_from_the_profile_or_the_argument(mailroom_profile):
    """The grounding share is the profile's unless the caller passes one."""
    items, text = ["Acme widget factory"], "Acme widget plant"
    assert verify_list_items(items, text, 0.6) == [True] and verify_list_items(items, text, 0.7) == [False]
    loose = _mailroom_with(audit={"token_coverage": 0.6})
    assert audit_list_field(loose, "id", items, [], text)["verified_in_doc"] == 1
    assert audit_list_field(mailroom_profile, "id", items, [], text)["verified_in_doc"] == 0
    assert audit_list_field(mailroom_profile, "id", items, [], text, token_coverage=0.6)["verified_in_doc"] == 1
    assert verify_list_items(["", "the of"], "the of Acme", 0.0) == [False, False]


def test_audit_support_votes_per_field_and_fails_on_a_malformed_counted_audit():
    """Fields with predictions vote once; an empty audit set is None; a counted audit missing a rate raises."""
    audits = {"a": {"n_predicted": 3, "verified_precision": 1.0, "hallucination_rate": 0.0},
              "b": {"n_predicted": 1, "verified_precision": 0.0, "hallucination_rate": 1.0},
              "c": {"n_predicted": 0}}
    assert field_audit_support(audits) == {"n_audited": 2, "verified_precision": 0.5, "hallucination_rate": 0.5}
    assert field_audit_support({}) == {"n_audited": 0, "verified_precision": None, "hallucination_rate": None}
    with pytest.raises(KeyError, match="hallucination_rate"):
        field_audit_support({"a": {"n_predicted": 1, "verified_precision": 1.0}})


# --- presence --------------------------------------------------------------------------------------

EXPECTATIONS = {
    "Governing Law": {"expected": True, "answer": "laws of the State of Delaware", "field": "cuad_clauses"},
    "Audit Rights": {"expected": True, "answer": "right to audit the books", "field": "cuad_clauses"},
    "Non-Compete": {"expected": False, "answer": "", "field": "cuad_clauses"},
}


def test_presence_scores_the_share_of_present_categories_found(mailroom_profile):
    """One of two present categories is covered; absent categories are listed but not scored."""
    predicted = {"cuad_clauses": ["This Agreement follows the laws of the State of Delaware. Notices go by mail."]}
    score, detail = score_category_presence(mailroom_profile, predicted, EXPECTATIONS)
    assert score == 0.5
    assert list(detail) == sorted(EXPECTATIONS)
    assert detail["Non-Compete"] == {"expected": False, "matched": None, "field": "cuad_clauses"}
    assert detail["Governing Law"]["matched"] is True and detail["Audit Rights"]["matched"] is False
    assert score_category_presence(mailroom_profile, None, {})[0] == 1.0


def test_reasoning_routes_spans_unless_the_profile_turns_it_off(mailroom_profile):
    """Tagged reasoning evidence replaces the field's spans; with routing off the field is used."""
    predicted = {"cuad_clauses": ["right to audit the books and records"],
                 "reasoning": {"entries": [{"field": "Audit Rights", "evidence": "no audit clause here"}]}}
    expectation = {"Audit Rights": EXPECTATIONS["Audit Rights"]}
    assert score_category_presence(mailroom_profile, predicted, expectation)[0] == 0.0
    unrouted = _mailroom_with(presence={"route_by_reasoning": False})
    assert score_category_presence(unrouted, predicted, expectation)[0] == 1.0


def test_reasoning_written_as_text_is_an_attribute_error_as_in_the_old_dojo(mailroom_profile):
    """Routing reads ``reasoning.entries``; a text reasoning has no entries mapping (kept for parity)."""
    with pytest.raises(AttributeError):
        score_category_presence(mailroom_profile, {"reasoning": "free text"}, EXPECTATIONS)


def test_an_expectation_without_a_field_needs_a_default(mailroom_profile):
    """The engine has no built-in default field; the caller supplies one or gets an error naming the category."""
    expectation = {"Audit Rights": {"expected": True, "answer": "right to audit"}}
    with pytest.raises(ValueError, match="'Audit Rights' names no field"):
        score_category_presence(mailroom_profile, {}, expectation)
    _, detail = score_category_presence(mailroom_profile, {"clauses": ["right to audit"]}, expectation,
                                        default_field="clauses")
    assert detail["Audit Rights"]["matched"] is True


@pytest.mark.parametrize("spec, reason", [({"kind": "raises"}, "backend_raised"), ({"kind": "nan"}, "not_a_number"),
                                          ({"kind": "value", "value": "0.9"}, "not_a_number"),
                                          ({"kind": "value", "value": True}, "not_a_number"),
                                          ({"kind": "value", "value": 1.5}, "out_of_range")])
def test_a_failing_presence_backend_never_matches_and_is_reported(spec, reason, caplog):
    """A broken backend leaves the containment answer and records why, with a warning."""
    profile = _mailroom_with(embedding={"enabled": True})
    similarity = PresenceSimilarity.from_profile(profile, FakeSimilarity(spec))
    predicted = {"cuad_clauses": ["unrelated words entirely"]}
    with caplog.at_level(logging.WARNING, logger=category_presence.__name__):
        score, _ = score_category_presence(profile, predicted, EXPECTATIONS, similarity=similarity)
    assert score == 0.0
    assert {fallback.reason for fallback in similarity.fallbacks} == {reason}
    assert all(fallback.string_score == 0.0 for fallback in similarity.fallbacks)
    assert any("presence similarity fallback" in record.getMessage() for record in caplog.records)


def test_a_working_presence_backend_matches_at_its_threshold():
    """An in-range answer at the threshold matches; None declines silently; a disabled profile refuses a backend."""
    profile = _mailroom_with(embedding={"enabled": True}, presence={"embedding_threshold": 0.8})
    predicted = {"cuad_clauses": ["unrelated words entirely"]}
    at = PresenceSimilarity.from_profile(profile, FakeSimilarity({"kind": "constant", "value": 0.8}))
    assert score_category_presence(profile, predicted, EXPECTATIONS, similarity=at)[0] == 1.0
    below = PresenceSimilarity.from_profile(profile, FakeSimilarity({"kind": "constant", "value": 0.79}))
    assert score_category_presence(profile, predicted, EXPECTATIONS, similarity=below)[0] == 0.0
    declines = PresenceSimilarity.from_profile(profile, FakeSimilarity({"kind": "none"}))
    assert score_category_presence(profile, predicted, EXPECTATIONS, similarity=declines)[0] == 0.0
    assert declines.fallbacks == ()
    with pytest.raises(ValueError, match="disables embeddings"):
        PresenceSimilarity.from_profile(_mailroom(), FakeSimilarity({"kind": "none"}))
    with pytest.raises(ValueError, match="between 0 and 1"):
        PresenceSimilarity(FakeSimilarity({"kind": "none"}), 1.5)


@given(st.lists(st.text(alphabet="Ab .;\n$1(", max_size=60), max_size=5), st.integers(min_value=1, max_value=6))
def test_clause_spans_are_stripped_and_unique(items, minimum):
    """Every span is non-blank and stripped, and no two spans have the same tokens."""
    spans = disaggregate_clause_spans(items, minimum)
    assert all(span and span == span.strip() for span in spans)
    keys = [" ".join(tokenize(span)) for span in spans]
    assert len(keys) == len(set(keys))


# --- trace capture ----------------------------------------------------------------------------------


@pytest.mark.parametrize("value, parsed", [
    (None, None), ("", None), (True, None), (0.5, 0.5), ("85", 0.85), (85, 0.85), (100, 1.0), (100.5, None),
    (-0.1, None), (float("nan"), None), ("abc", None), ({"score": 0.7}, 0.7), ({"other": 1}, None), ([0.5], None),
    (0.123456, 0.1235),
])
def test_confidence_parsing(value, parsed):
    """Percentages scale, out-of-range and non-numeric values are None, mappings are read by key."""
    assert parse_confidence(value) == parsed


def test_reasoning_parsing_shapes():
    """Text, entry lists, specialist objects and bare entries all normalize; empty shapes are None."""
    assert parse_reasoning("  why ") == {"summary": "why", "entries": [], "text": "why"}
    assert parse_reasoning(["a", "", None]) == {
        "summary": None, "entries": [{"field": None, "evidence": "a", "section_ref": None}]}
    assert parse_reasoning({"field": " parties ", "evidence": 5}) == {
        "summary": None, "entries": [{"field": "parties", "evidence": "5", "section_ref": None}]}
    assert parse_reasoning({"summary": "", "entries": []}) is None
    assert parse_reasoning(42) == {"summary": "42", "entries": [], "text": "42"}


@pytest.mark.parametrize("options, confidence, gate", [
    (TraceCaptureOptions(), None, "missing"),
    (TraceCaptureOptions(missing_confidence="assume_1"), None, "pass"),
    (TraceCaptureOptions(missing_confidence="assume_0", confidence_min=0.5), None, "below_min"),
    (TraceCaptureOptions(confidence_band=(0.4, 0.8)), 0.8, "pass"),
    (TraceCaptureOptions(confidence_band=(0.4, 0.8)), 0.4, "in_band"),
    (TraceCaptureOptions(confidence_min=0.5, confidence_band=(0.0, 1.0)), 0.3, "below_min"),
])
def test_confidence_gates(options, confidence, gate):
    """The floor is checked before the half-open band; missing confidence is filled by policy first."""
    assert confidence_gate(confidence, options) == gate


def test_capture_respects_every_switch():
    """Turning capture off leaves keys None but keeps the payload shape."""
    predicted = {"confidence": 0.9, "reasoning": ["because"]}
    full = capture_trace(predicted, TraceCaptureOptions(), correctness=0.5)
    assert full == {"confidence": 0.9, "reasoning": {"summary": None, "entries": [
        {"field": None, "evidence": "because", "section_ref": None}]}, "confidence_gate": "pass",
        "calibration_error": 0.4, "n_reasoning_entries": 1}
    off = capture_trace(predicted, TraceCaptureOptions(capture_confidence=False, capture_reasoning=False,
                                                       compute_calibration_error=False), correctness=0.5)
    assert off == dict.fromkeys(TRACE_KEYS)
    with pytest.raises(ValueError):
        capture_trace("not a mapping", TraceCaptureOptions())  # type: ignore[arg-type]


def test_a_span_covering_exactly_the_coverage_share_matches(mailroom_profile):
    """Containment at exactly ``audit.token_coverage`` covers the clause (the comparison is inclusive)."""
    answer = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    expectation = {"Custom": {"expected": True, "answer": answer, "field": "clauses"}}
    covering = {"clauses": ["alpha bravo charlie delta echo foxtrot golf"]}
    short = {"clauses": ["alpha bravo charlie delta echo foxtrot"]}
    assert score_category_presence(mailroom_profile, covering, expectation)[0] == 1.0
    assert score_category_presence(mailroom_profile, short, expectation)[0] == 0.0


def test_an_enormous_integer_confidence_is_no_confidence_instead_of_a_crash():
    """float() of an integer too large for a float raised OverflowError (also in the old Dojo); it reads as absent."""
    from mailroom_dojo.field_scoring.trace_capture import parse_confidence

    assert parse_confidence(10**400) is None
    assert parse_confidence({"confidence": -(10**400)}) is None
    assert parse_confidence(0.5) == 0.5
