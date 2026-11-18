"""score_extraction: profile-driven rules, row variants, reported fallbacks, faults, threads and hostile input."""

from __future__ import annotations

import math
import socket
import threading
import time
from functools import cache

import pytest
from dojo_testkit import MAILROOM_PROFILE_PATH, FakeSimilarity, canonical, mailroom_profile_mapping, minimal_profile_mapping
from hypothesis import given
from hypothesis import strategies as st

from mailroom_dojo.field_scoring import (
    ExtractionScoreResult,
    UnknownFieldTypeError,
    UnmappedFieldError,
    is_accepted_unscored_field,
    score_extraction,
)
from mailroom_dojo.profile import load_scoring_profile

SAMPLE_VALUES = {
    "id": "C-1001",
    "date": "August 18, 2014",
    "money": "$1,000.00",
    "name": "Acme Corporation",
    "free_text": "The Seller shall deliver the goods within thirty days.",
    "containment": "the laws of the State of Delaware",
    "label": "other",
}
OLD_RESULT_KEYS = {"doc_class", "field_scores", "overall_score", "ambiguous_fields", "entity_list_scores",
                   "entity_list_audit", "trace"}


@cache
def _mailroom():
    """The mailroom profile, loaded once (hypothesis examples cannot take function-scoped fixtures)."""
    return load_scoring_profile(MAILROOM_PROFILE_PATH)


def _mailroom_with(**sections: dict):
    """The mailroom profile with some top-level sections updated."""
    mapping = mailroom_profile_mapping()
    for name, values in sections.items():
        if isinstance(values, dict):
            mapping.setdefault(name, {}).update(values)
        else:
            mapping[name] = values
    return load_scoring_profile(mapping)


def _sample(field_type: str) -> object:
    """A plausible value for a field type."""
    if field_type.startswith("entity_list"):
        return ["Acme Corporation", "Beta LLC"]
    return SAMPLE_VALUES[field_type]


@cache
def _fleet_profile():
    """A synthetic client whose class has a row variant: no Python names any of it."""
    mapping = minimal_profile_mapping()
    fields = mapping["classes"][0]["fields"]
    fields["garaging_address"] = "name"
    fields["vehicles"] = "entity_list:name"
    mapping["classes"][0]["variants"] = [{
        "name": "commercial_fleet",
        "marker": {"field": "fleet_kind", "values": ["fleet"], "fallback_fields": ["coverage_tier"],
                   "skip_when_unstated": True},
        "guard": {"field": "coverage_tier", "allowed": ["premium"], "allow_empty": True},
        "documented_absent_fields": ["garaging_address"],
        "field_type_overrides": {"vehicles": "entity_list:id"},
        "added_fields": {"fleet_kind": {"type": "label", "vocabulary": {"tokens": ["fleet"], "fold": "exact"}}},
    }]
    return load_scoring_profile(mapping)


FLEET = "auto_policy_declaration"


# --- field types: the profile's, an explicit map, unknown and unmapped ----------------------------


@pytest.mark.parametrize("class_key", ["contract", "corporate_record", "due_diligence", "correspondence",
                                       "compliance_filing", "court_opinion", "insurance_claim", "merger_agreement"])
def test_no_field_type_map_means_the_profiles_map(mailroom_profile, class_key):
    """``field_types=None`` scores exactly like passing the class's map from the profile."""
    types = mailroom_profile.field_types(class_key)
    expected = {name: _sample(field_type) for name, field_type in types.items()}
    predicted = {name: value for index, (name, value) in enumerate(expected.items()) if index % 3}
    text = "Acme Corporation signed on 08/18/2014 for $1,000.00."
    from_profile = score_extraction(mailroom_profile, class_key, predicted, expected, doc_text=text)
    explicit = score_extraction(mailroom_profile, class_key, predicted, expected, field_types=types, doc_text=text)
    assert canonical(from_profile.to_dict()) == canonical(explicit.to_dict())
    assert from_profile.field_scores and from_profile.entity_list_audit


def test_an_unknown_class_needs_an_explicit_map(mailroom_profile):
    """Without a map an unknown class is a KeyError naming the known classes; with one it scores."""
    with pytest.raises(KeyError, match="unknown document class 'audit'"):
        score_extraction(mailroom_profile, "audit", {"finding": "ok"}, {"finding": "ok"})
    result = score_extraction(mailroom_profile, "audit", {"finding": "ok"}, {"finding": "ok"},
                              field_types={"finding": "free_text"})
    assert result.field_scores == {"finding": 1.0}


def test_unregistered_types_fail_unless_scoring_them_as_name_is_asked(mailroom_profile):
    """An unknown type in a caller's map is refused, or scored as name and reported."""
    with pytest.raises(UnknownFieldTypeError, match="'severity': 'severity_scale'"):
        score_extraction(mailroom_profile, "audit", {"severity": "high"}, {"severity": "High"},
                         field_types={"severity": "severity_scale"})
    result = score_extraction(mailroom_profile, "audit", {"severity": "high"}, {"severity": "High"},
                              field_types={"severity": "severity_scale"}, unknown_field_types="score_as_name")
    as_name = score_extraction(mailroom_profile, "audit", {"severity": "high"}, {"severity": "High"},
                               field_types={"severity": "name"})
    assert result.field_type_fallbacks == {"severity": "severity_scale"}
    assert result.field_scores == as_name.field_scores
    with pytest.raises(ValueError, match="unknown_field_types must be"):
        score_extraction(mailroom_profile, "audit", {}, {}, field_types={}, unknown_field_types="guess")


@pytest.mark.parametrize("arguments, message", [
    ({"field_types": {"finding": None}}, "not text"),
    ({"field_types": ["finding"]}, "field_types must be a mapping"),
    ({"predicted": [("finding", "x")]}, "predicted must be a mapping"),
    ({"expected": "finding=x"}, "expected must be a mapping"),
    ({"audit_predicted": ["x"]}, "audit_predicted must be a mapping"),
])
def test_malformed_arguments_are_type_errors(mailroom_profile, arguments, message):
    """Malformed maps fail at once with a TypeError naming the argument, never deep inside a scorer."""
    call = {"predicted": {"finding": "x"}, "expected": {"finding": "x"}, "field_types": {"finding": "name"}}
    call.update(arguments)
    with pytest.raises(TypeError, match=message):
        score_extraction(mailroom_profile, "audit", call["predicted"], call["expected"],
                         field_types=call["field_types"], doc_text="x", audit_predicted=call.get("audit_predicted"))


def test_unmapped_expected_keys_follow_the_profile_policy():
    """heuristic types a key by its name, skip leaves it out, error refuses it."""
    expected = {"policy_number": "P-1", "renewal_date": "2014-08-18"}
    predicted = {"policy_number": "P-1", "renewal_date": "August 18, 2014"}
    for policy, outcome in (("heuristic", {"policy_number": 1.0, "renewal_date": 1.0}), ("skip", {"policy_number": 1.0})):
        profile = load_scoring_profile(minimal_profile_mapping(unmapped_fields=policy))
        assert score_extraction(profile, FLEET, predicted, expected).field_scores == outcome
    strict = load_scoring_profile(minimal_profile_mapping(unmapped_fields="error"))
    with pytest.raises(UnmappedFieldError, match="'renewal_date'"):
        score_extraction(strict, FLEET, predicted, expected)


def test_trace_retired_and_accepted_unscored_keys(mailroom_profile):
    """Trace and annotation keys are never scored; retired and accepted-unscored keys only when the map names them."""
    expected = {"parties": ["Acme"], "confidence": 0.9, "reasoning": "x", "gt_presence": "{}",
                "key_obligations": ["pay"], "merger_consideration": "all_cash"}
    predicted = {"parties": ["Acme"], "key_obligations": ["pay"], "merger_consideration": "all_cash", "confidence": 0.9}
    default = score_extraction(mailroom_profile, "contract", predicted, expected)
    assert set(default.field_scores) == {"parties"}
    opted_in = score_extraction(mailroom_profile, "contract", predicted, expected, field_types={
        "parties": "entity_list:name", "key_obligations": "entity_list:free_text", "merger_consideration": "label",
        "confidence": "money"})
    assert set(opted_in.field_scores) == {"parties", "key_obligations", "merger_consideration"}
    merger = score_extraction(mailroom_profile, "merger_agreement", predicted, expected)
    assert "merger_consideration" in merger.field_scores
    assert is_accepted_unscored_field(mailroom_profile, " CONTRACT ", "maud_clauses")
    assert not is_accepted_unscored_field(mailroom_profile, "contract", "maud_clauses", {"maud_clauses": "name"})


# --- row variants ----------------------------------------------------------------------------------


def test_a_variant_row_scores_documented_absent_fields_both_ways():
    """Silence on a documented-absent field earns 1.0, a stated value 0.0, no prediction at all nothing."""
    profile = _fleet_profile()
    expected = {"fleet_kind": "fleet", "garaging_address": "", "policy_number": "P-1"}
    silent = score_extraction(profile, FLEET, {"policy_number": "P-1", "fleet_kind": "fleet"}, expected)
    assert silent.field_scores == {"fleet_kind": 1.0, "garaging_address": 1.0, "policy_number": 1.0}
    stated = score_extraction(profile, FLEET, {"fleet_kind": "fleet", "garaging_address": ["  ", "Depot 9"]}, expected)
    assert stated.field_scores["garaging_address"] == 0.0
    blank_items = score_extraction(profile, FLEET, {"fleet_kind": "fleet", "garaging_address": [None, ""]}, expected)
    assert blank_items.field_scores["garaging_address"] == 1.0
    nothing = score_extraction(profile, FLEET, None, expected)
    assert nothing.field_scores == {"policy_number": 0.0}  # no documented-absent credit, marker unstated


def test_an_omitted_documented_absent_field_is_added_only_when_the_prediction_states_it():
    """A stated value for a field the label omits is a false positive; silence on it is not scored."""
    profile = _fleet_profile()
    expected = {"fleet_kind": "fleet"}
    assert "garaging_address" not in score_extraction(profile, FLEET, {"fleet_kind": "fleet"}, expected).field_scores
    stated = score_extraction(profile, FLEET, {"fleet_kind": "fleet", "garaging_address": "Depot 9"}, expected)
    assert stated.field_scores["garaging_address"] == 0.0
    narrowed = score_extraction(profile, FLEET, {"fleet_kind": "fleet", "garaging_address": "Depot 9"}, expected,
                                field_types={"policy_number": "id"})
    assert "garaging_address" not in narrowed.field_scores


def test_variant_type_overrides_marker_skip_and_fallback():
    """Overrides apply on variant rows only; an unstated marker is skipped; a blank marker reads its fallback field."""
    profile = _fleet_profile()
    expected = {"fleet_kind": "fleet", "vehicles": ["VIN-1"], "coverage_tier": ""}
    near_miss = {"vehicles": ["VIN 1x"]}
    variant = score_extraction(profile, FLEET, near_miss, expected)
    assert variant.field_scores == {"vehicles": 0.0}  # id elements, and the unstated marker is not scored
    plain = score_extraction(profile, FLEET, near_miss, expected, apply_variants=False)
    assert plain.field_scores["vehicles"] == 1.0 and plain.field_scores["fleet_kind"] == 0.0
    from_fallback = score_extraction(profile, FLEET, {"coverage_tier": "FLEET"}, expected)
    assert from_fallback.field_scores["fleet_kind"] == 1.0
    guarded = score_extraction(profile, FLEET, near_miss, {**expected, "coverage_tier": "basic"})
    assert guarded.field_scores["vehicles"] == 1.0  # the guard rejects the row, so no override


def test_a_cms_table_token_on_claim_type_scores_as_an_exact_label(mailroom_profile):
    """On any insurance row, a predicted table token is never fuzzy-matched to the product line."""
    expected = {"claim_type": "health"}
    assert score_extraction(mailroom_profile, "insurance_claim", {"claim_type": "pde"}, expected).field_scores == {
        "claim_type": 0.0}
    cms_row = {"claim_type": "health", "cms_source_table": "pde"}
    result = score_extraction(mailroom_profile, "insurance_claim", {"claim_type": "pde"}, cms_row)
    assert result.field_scores == {"claim_type": 0.0, "cms_source_table": 1.0}


# --- embeddings, trace options, serialization ----------------------------------------------------


def test_a_backend_needs_a_profile_that_enables_embeddings(mailroom_profile):
    """Passing a backend to a profile that disables embeddings is refused, not ignored."""
    with pytest.raises(ValueError, match="disables embedding rescue"):
        score_extraction(mailroom_profile, "contract", {}, {}, similarity=FakeSimilarity({"kind": "constant", "value": 1}))


@pytest.mark.parametrize("spec, reason", [({"kind": "raises"}, "backend_raised"), ({"kind": "nan"}, "not_a_number"),
                                          ({"kind": "value", "value": 1.5}, "out_of_range")])
def test_a_failing_backend_keeps_string_scores_and_is_reported(spec, reason):
    """Every backend failure keeps the string score and lands on the result, outside to_dict."""
    profile = _mailroom_with(embedding={"enabled": True})
    expected = {"parties": ["Acme Corporation", "Zeta Ventures"], "document_name": "Master Services Agreement",
                "governing_law": "Delaware"}
    predicted = {"parties": ["Omega Partners"], "document_name": "Supply contract", "governing_law": "New York"}
    plain = score_extraction(profile, "contract", predicted, expected)
    broken = score_extraction(profile, "contract", predicted, expected, similarity=FakeSimilarity(spec))
    assert broken.to_dict() == plain.to_dict()
    assert broken.similarity_fallbacks and {fallback.reason for fallback in broken.similarity_fallbacks} == {reason}
    assert plain.similarity_fallbacks == ()


def test_trace_options_come_from_the_profile():
    """The confidence gate and calibration follow the profile's trace_capture section."""
    gated = _mailroom_with(trace_capture={"confidence_min": 0.9, "compute_calibration_error": False})
    trace = score_extraction(gated, "contract", {"governing_law": "Delaware", "confidence": "80"},
                             {"governing_law": "Delaware"}).trace
    assert trace == {"confidence": 0.8, "reasoning": None, "confidence_gate": "below_min", "calibration_error": None,
                     "n_reasoning_entries": None}
    default = score_extraction(_mailroom(), "contract", {"governing_law": "Delaware", "confidence": "80"},
                               {"governing_law": "Delaware"}).trace
    assert default["confidence_gate"] == "pass" and default["calibration_error"] == 0.2


def test_the_result_serializes_exactly_the_old_keys(mailroom_profile):
    """to_dict has the old shape; reports and summaries are attributes and properties."""
    result = score_extraction(mailroom_profile, "contract", {"parties": ["Acme", "Ghost Co"]},
                              {"parties": ["Acme"]}, doc_text="Acme signs.")
    assert set(result.to_dict()) == OLD_RESULT_KEYS and isinstance(result, ExtractionScoreResult)
    assert result.entity_list_audit["parties"]["hallucinated"] == 1
    assert result.audit_support == {"n_audited": 1, "verified_precision": 0.5, "hallucination_rate": 0.5}
    assert result.overall_verified_precision == 0.5 and result.matcher_fallbacks == {}
    with pytest.raises(AttributeError):
        result.overall_score = 1.0  # type: ignore[misc]


# --- isolation, network, hostile input, invariants ------------------------------------------------


def test_two_profiles_in_threads_never_see_each_others_rules():
    """Eight threads alternate two profiles; each always gets the answer its own profile gives serially."""
    lenient, strict = _mailroom(), _mailroom_with(thresholds={"list_match_threshold": 1.0, "ambiguous_band": [0.0, 1.0]})
    predicted = {"parties": ["Acme Corp", "Jon Smith"], "governing_law": "laws of Delaware"}
    expected = {"parties": ["ACME Corporation", "John Smith"], "governing_law": "the laws of the State of Delaware"}
    answers = {id(profile): canonical(score_extraction(profile, "contract", predicted, expected).to_dict())
               for profile in (lenient, strict)}
    assert len(set(answers.values())) == 2
    failures: list[str] = []

    def work(profile):
        """Score repeatedly and record any answer that is not this profile's."""
        for _ in range(150):
            if canonical(score_extraction(profile, "contract", predicted, expected).to_dict()) != answers[id(profile)]:
                failures.append(profile.client)

    threads = [threading.Thread(target=work, args=((lenient, strict)[index % 2],)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []


def test_scoring_the_extraction_corpus_opens_no_socket(monkeypatch):
    """Every third corpus case is replayed with socket creation, connection and name lookup forbidden."""
    from test_differential_extraction_parity import extraction_corpus, new_outcome

    def forbidden(*args, **kwargs):
        """Fail any network use."""
        raise AssertionError("network access during scoring")

    _, cases = extraction_corpus()
    sample = cases[::3]
    for case in sample[:20]:
        new_outcome(case)  # warm the per-settings profile cache before the network is cut
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    outcomes = [new_outcome(case) for case in sample]
    assert not [outcome for outcome in outcomes if outcome.get("raises") == "AssertionError"]
    assert len(outcomes) > 2000


HOSTILE_VALUES = [None, "", "null", "[]", {}, [], 0, False, math.nan, math.inf, "日本 Ünïcödé", [[["deep"]]],
                  {"nested": {"dict": [1, 2]}}, "x" * 1_000_000, [f"Party {index}" for index in range(10_000)]]


@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=lambda value: type(value).__name__ + str(len(str(value)))[:6])
def test_hostile_values_score_in_bounds_and_in_time(mailroom_profile, value):
    """Any value in any field type completes within the budget with every score in [0, 1]."""
    expected = {name: _sample(field_type) for name, field_type in mailroom_profile.field_types("contract").items()}
    predicted = {name: value for name in expected}
    started = time.perf_counter()
    result = score_extraction(mailroom_profile, "contract", predicted, expected, doc_text="Acme on 08/18/2014")
    reversed_result = score_extraction(mailroom_profile, "contract", expected, predicted, doc_text=str(value)[:2000])
    assert time.perf_counter() - started < 60.0
    for scored in (result, reversed_result):
        assert all(0.0 <= score <= 1.0 for score in scored.field_scores.values())


field_names = st.sampled_from(sorted(_mailroom().field_types("insurance_claim")) + ["confidence", "gt_presence",
                                                                                 "cms_source_table", "notes"])
values = st.one_of(st.none(), st.text(max_size=20), st.sampled_from(list(SAMPLE_VALUES.values()) + ["pde", "health"]),
                   st.lists(st.sampled_from(["Acme", "NPI 5040521237", "", "Seller"]), max_size=4),
                   st.floats(allow_nan=True), st.integers())


@given(st.dictionaries(field_names, values, max_size=8), st.dictionaries(field_names, values, max_size=8),
       st.booleans())
def test_extraction_invariants(predicted, expected, apply_variants):
    """Scores are bounded, the overall score is their mean, and only expected or variant fields are scored."""
    profile = _mailroom()
    result = score_extraction(profile, "insurance_claim", predicted, expected, apply_variants=apply_variants,
                              doc_text="Acme filed claim C-1001 on 08/18/2014.")
    scores = result.field_scores
    assert all(0.0 <= score <= 1.0 for score in scores.values())
    assert result.overall_score == (round(sum(scores.values()) / len(scores), 4) if scores else None)
    assert set(result.ambiguous_fields) <= set(scores)
    assert not {"confidence", "gt_presence"} & set(scores)
    assert set(scores) <= set(expected) | {"adjuster", "denial_reasons"}
    assert set(result.entity_list_scores) <= set(scores)
    for audit in result.entity_list_audit.values():
        assert audit["true_items"] + audit["hallucinated"] == audit["n_predicted"]
