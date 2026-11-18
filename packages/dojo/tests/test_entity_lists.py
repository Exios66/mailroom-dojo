"""Entity-list scoring: invariants, the explicit matcher choice and its reported fallback, faults and hostile
input."""

from __future__ import annotations

import logging
import math
import random
import sys
import time
from functools import cache

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dojo_testkit import MAILROOM_PROFILE_PATH, FakeSimilarity, mailroom_profile_mapping
from mailroom_dojo.field_scoring import entity_lists
from mailroom_dojo.field_scoring.entity_lists import EntityListScore, as_list, score_entity_list
from mailroom_dojo.field_scoring.scorer_registry import (
    FieldScorerRegistry,
    ScorerResultError,
    UnknownFieldTypeError,
)
from mailroom_dojo.field_scoring.similarity_backend import EmbeddingRescue
from mailroom_dojo.profile import load_scoring_profile

NAMES = [
    "Acme Corp",
    "ACME Corporation",
    "Beta LLC",
    "Gamma Trust",
    "John Smith",
    "J. Smith",
    "Seller",
    "Buyer",
    "the Company",
    "Delta Partners LP",
    "",
    "Omega",
    "alpha beta",
    "beta",
    "gamma",
]
IDENTIFIERS = ["C-1", "C1", "c 1", "X-9", "x9", "42", "", "ABC"]
CMS_REFERENCES = [
    "NPI 5040521237",
    "provider NPI 5040521237",
    "NDC 00703051002",
    "HCPCS detail (1 codes)",
    "HCPCS detail (2 codes)",
    "NPI 5040521238",
]
LABELS = ["approved", "denied", "Approved", "partial", ""]

item_lists = st.lists(st.sampled_from(NAMES), max_size=6)


@cache
def _mailroom():
    """The mailroom profile, loaded once (hypothesis examples cannot take function-scoped fixtures)."""
    return load_scoring_profile(MAILROOM_PROFILE_PATH)


def _with(profile_mapping: dict, **sections: dict) -> object:
    """Load the mailroom profile with some sections replaced."""
    for name, values in sections.items():
        profile_mapping.setdefault(name, {}).update(values)
    return load_scoring_profile(profile_mapping)


def test_empty_sides_follow_the_documented_rules(mailroom_profile):
    """Both empty is perfect; an empty side scores zero recall; counts follow the lists."""
    both = score_entity_list(mailroom_profile, "name", [], None)
    assert (both.precision, both.recall, both.f1, both.matcher) == (1.0, 1.0, 1.0, "none")
    no_prediction = score_entity_list(mailroom_profile, "name", None, ["Acme"])
    assert (no_prediction.precision, no_prediction.recall, no_prediction.n_expected) == (0.0, 0.0, 1)
    no_expectation = score_entity_list(mailroom_profile, "name", '["Acme", "Beta"]', "[]")
    assert (no_expectation.precision, no_expectation.recall, no_expectation.f1) == (1.0, 0.0, 0.0)
    assert no_expectation.n_predicted == 2


def test_role_words_are_credited_only_for_recall_scored_lists_that_name_a_party(mailroom_profile):
    """CUAD role labels match when the prediction names any party, and never push precision past 1."""
    recall = score_entity_list(
        mailroom_profile, "name", ["Acme Corp"], ["Seller", "Buyer"], recall_coverage=True
    )
    assert recall.matched == 1 and recall.precision == 1.0 and recall.recall == 0.5 and recall.score == 0.5
    only_roles = score_entity_list(
        mailroom_profile, "name", ["the Seller"], ["Seller", "Buyer"], recall_coverage=True
    )
    assert only_roles.matched == 1  # "the Seller" matches "Seller" as a name; no role credit without a party
    plain = score_entity_list(mailroom_profile, "name", ["Acme Corp"], ["Seller", "Buyer"])
    assert plain.matched == 0 and plain.score == 0.0


def test_role_credit_is_off_when_the_profile_lists_no_role_words():
    """The role-word rule is profile data: an empty list switches it off."""
    profile = _with(mailroom_profile_mapping(), list_matching={"role_words": []})
    result = score_entity_list(profile, "name", ["Acme Corp"], ["Seller", "Buyer"], recall_coverage=True)
    assert result.matched == 0


def test_contained_labels_of_three_to_six_tokens_are_credited(mailroom_profile):
    """A label quoted word for word inside a predicted item counts; a two-token label does not."""
    predicted = ["the Seller shall deliver the goods within thirty days"]
    three = score_entity_list(
        mailroom_profile, "free_text", predicted, ["deliver the goods"], recall_coverage=True
    )
    assert three.matched == 1
    seven = "the Seller shall deliver the goods within"
    assert score_entity_list(mailroom_profile, "name", predicted, [seven], recall_coverage=True).matched == 1
    two = score_entity_list(mailroom_profile, "id", predicted, ["the goods"], recall_coverage=True)
    assert two.matched == 0


def test_list_match_threshold_comes_from_the_profile(mailroom_profile):
    """The same pair matches under 0.6 and not under 1.0."""
    strict = _with(mailroom_profile_mapping(), thresholds={"list_match_threshold": 1.0})
    predicted, expected = ["Acme Corp", "Jon Smith"], ["ACME Corporation", "John Smith"]
    assert score_entity_list(mailroom_profile, "name", predicted, expected).matched == 2
    assert score_entity_list(strict, "name", predicted, expected).matched == 1


@given(item_lists, item_lists, st.booleans(), st.sampled_from(["name", "free_text", "id", "label"]))
def test_metrics_are_bounded_and_consistent(predicted, expected, recall_coverage, element_type):
    """Precision, recall and F1 stay in [0, 1]; F1 is their harmonic mean; counts add up; score picks the
    metric."""
    profile = _mailroom()
    result = score_entity_list(profile, element_type, predicted, expected, recall_coverage=recall_coverage)
    assert all(0.0 <= value <= 1.0 for value in (result.precision, result.recall, result.f1))
    assert 0 <= result.matched <= min(len(predicted), len(expected))
    if predicted and expected:
        assert result.n_predicted == len(predicted) and result.n_expected == len(expected)
        harmonic = (
            2 * result.precision * result.recall / (result.precision + result.recall)
            if result.matched
            else 0.0
        )
        assert result.f1 == harmonic
        assert result.precision == result.matched / len(predicted)
    assert result.score == (result.recall if recall_coverage else result.f1)
    serialized = result.to_dict()
    assert set(serialized) == {
        "field",
        "precision",
        "recall",
        "f1",
        "matched",
        "n_predicted",
        "n_expected",
        "partial_gt",
    }
    assert serialized["partial_gt"] is recall_coverage


@given(
    st.sampled_from([("id", IDENTIFIERS), ("label", LABELS), ("cms_doc_ref", CMS_REFERENCES)]).flatmap(
        lambda kind: st.tuples(
            st.just(kind[0]),
            st.lists(st.sampled_from(kind[1]), max_size=6),
            st.lists(st.sampled_from(kind[1]), max_size=6),
        )
    ),
    st.randoms(use_true_random=False),
)
def test_all_or_nothing_element_types_are_order_invariant(case, rng):
    """With 0/1 similarities every optimal pairing has the same count, so shuffling either side changes
    nothing."""
    element_type, predicted, expected = case
    profile = _mailroom()
    reference = score_entity_list(profile, element_type, predicted, expected).to_dict()
    shuffled_predicted, shuffled_expected = list(predicted), list(expected)
    rng.shuffle(shuffled_predicted)
    rng.shuffle(shuffled_expected)
    assert (
        score_entity_list(profile, element_type, shuffled_predicted, shuffled_expected).to_dict() == reference
    )


def test_graded_similarities_can_depend_on_item_order_as_in_the_old_dojo(mailroom_profile):
    """Two pairings with the same total similarity can differ in how many pairs pass the threshold.

    The Hungarian solver then picks by input order. The old Dojo did the same (this
    pair is in the differential corpus); the case is pinned so a change is deliberate.
    """
    expected = ["beta alpha beta", "alpha gamma"]
    assert score_entity_list(mailroom_profile, "free_text", ["alpha beta", "beta"], expected).matched == 1
    assert score_entity_list(mailroom_profile, "free_text", ["beta", "alpha beta"], expected).matched == 0


def test_hungarian_is_the_default_and_greedy_is_explicit(mailroom_profile):
    """The result names the matcher that ran; a single pair skips both."""
    predicted, expected = ["Acme Corp", "Beta LLC"], ["Beta LLC", "Acme Corporation"]
    assert score_entity_list(mailroom_profile, "name", predicted, expected).matcher == "hungarian"
    greedy = score_entity_list(mailroom_profile, "name", predicted, expected, matcher="greedy")
    assert greedy.matcher == "greedy" and greedy.matcher_fallback is None and greedy.matched == 2
    assert score_entity_list(mailroom_profile, "name", ["Acme"], ["Acme"]).matcher == "single_pair"
    with pytest.raises(ValueError, match="matcher must be"):
        score_entity_list(mailroom_profile, "name", predicted, expected, matcher="optimal")
    with pytest.raises(ValueError, match="fallback_matcher must be"):
        score_entity_list(mailroom_profile, "name", predicted, expected, fallback_matcher="random")


def _no_scipy(monkeypatch):
    """Make ``from scipy.optimize import ...`` fail the way a missing install does."""
    monkeypatch.setitem(sys.modules, "scipy.optimize", None)


def test_missing_scipy_raises_unless_the_greedy_fallback_is_allowed(mailroom_profile, monkeypatch, caplog):
    """Without scipy the default is an ImportError; an allowed fallback runs greedy and says why."""
    predicted, expected = ["Acme Corp", "Beta LLC", "Gamma"], ["Beta LLC", "Acme Corporation"]
    explicit_greedy = score_entity_list(mailroom_profile, "name", predicted, expected, matcher="greedy")
    _no_scipy(monkeypatch)
    with pytest.raises(ImportError):
        score_entity_list(mailroom_profile, "name", predicted, expected)
    with caplog.at_level(logging.WARNING, logger=entity_lists.__name__):
        result = score_entity_list(mailroom_profile, "name", predicted, expected, fallback_matcher="greedy")
    assert result.matcher == "greedy"
    assert result.matcher_fallback == (
        "scipy unavailable (ModuleNotFoundError: import of scipy.optimize halted; None in sys.modules)"
    )
    assert result.to_dict() == explicit_greedy.to_dict()
    assert any("greedy matcher" in record.getMessage() for record in caplog.records)


def test_a_failing_solver_is_never_hidden_by_the_fallback(mailroom_profile, monkeypatch):
    """Only an import failure falls back; a solver error propagates even when the fallback is allowed."""

    def broken_solver(cost):
        """Fail like a bug inside the solver."""
        raise RuntimeError("solver bug")

    monkeypatch.setattr(entity_lists, "_load_hungarian_solver", lambda: broken_solver)
    with pytest.raises(RuntimeError, match="solver bug"):
        score_entity_list(mailroom_profile, "name", ["a b", "c"], ["a", "c d"], fallback_matcher="greedy")


def test_a_nan_element_score_is_refused_before_it_reaches_the_matrix(mailroom_profile):
    """A plug-in returning NaN raises ScorerResultError at once, for a pair or for a matrix.

    The old Dojo silently switched to the greedy matcher when the solver refused the matrix.
    """
    registry = FieldScorerRegistry.for_profile(mailroom_profile).with_plugin(
        "broken", lambda a, b, r=None: math.nan
    )
    with pytest.raises(ScorerResultError, match="returned nan"):
        score_entity_list(mailroom_profile, "broken", ["a", "b"], ["c", "d"], registry=registry)
    with pytest.raises(ScorerResultError, match="returned nan"):
        score_entity_list(mailroom_profile, "broken", ["a"], ["c"], registry=registry)


def test_a_raising_element_scorer_propagates(mailroom_profile):
    """A scorer bug is not swallowed by any matcher."""

    def raising(predicted, expected, rescue=None):
        """Fail like a scorer bug."""
        raise ZeroDivisionError("scorer bug")

    registry = FieldScorerRegistry.for_profile(mailroom_profile).with_plugin("raising", raising)
    for matcher in ("hungarian", "greedy"):
        with pytest.raises(ZeroDivisionError):
            score_entity_list(
                mailroom_profile, "raising", ["a", "b"], ["c"], registry=registry, matcher=matcher
            )


def test_unknown_element_types_fail_unless_a_fallback_type_is_named(mailroom_profile):
    """An unregistered element type is an error, or scored as the named type on request."""
    with pytest.raises(UnknownFieldTypeError, match="widget"):
        score_entity_list(mailroom_profile, "widget", ["Acme"], ["Acme"])
    fallback = score_entity_list(mailroom_profile, "widget", ["Acme Corp"], ["ACME"], unknown_type_as="name")
    assert (
        fallback.to_dict() == score_entity_list(mailroom_profile, "name", ["Acme Corp"], ["ACME"]).to_dict()
    )


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "raises"},
        {"kind": "nan"},
        {"kind": "value", "value": "0.99"},
        {"kind": "value", "value": 7.0},
    ],
)
def test_a_failing_similarity_backend_keeps_string_scores_and_reports(spec):
    """A broken backend inside a list changes no score; each use is recorded on the rescue."""
    profile = _with(mailroom_profile_mapping(), embedding={"enabled": True})
    predicted, expected = ["Acme Corp", "Totally Different Name"], ["Acme Corporation", "Other Party Inc"]
    rescue = EmbeddingRescue.from_profile(profile, FakeSimilarity(spec))
    rescued = score_entity_list(profile, "name", predicted, expected, rescue=rescue)
    assert rescued.to_dict() == score_entity_list(profile, "name", predicted, expected).to_dict()
    assert rescue.fallbacks
    expected_reason = {"raises": "backend_raised", "nan": "not_a_number", "value": None}[spec["kind"]]
    reasons = {fallback.reason for fallback in rescue.fallbacks}
    assert reasons == ({expected_reason} if expected_reason else reasons) and reasons <= {
        "backend_raised",
        "not_a_number",
        "out_of_range",
    }


def test_a_working_backend_lifts_list_matches():
    """An in-range backend answer above the threshold pairs lexically distant items."""
    profile = _with(mailroom_profile_mapping(), embedding={"enabled": True})
    predicted, expected = ["the purchaser", "Zeta"], ["Buyer of the shares", "Omega"]
    plain = score_entity_list(profile, "name", predicted, expected)
    rescue = EmbeddingRescue.from_profile(profile, FakeSimilarity({"kind": "constant", "value": 0.9}))
    assert score_entity_list(profile, "name", predicted, expected, rescue=rescue).matched > plain.matched
    assert rescue.fallbacks == ()


@pytest.mark.parametrize(
    "value", [None, {"a": 1}, [[1, 2], [3]], 0, "", '{"not": "a list"}', math.nan, "日本"]
)
def test_hostile_shapes_are_read_as_lists(mailroom_profile, value):
    """None is empty, a scalar or mapping is one item, a JSON list text is parsed; the result stays
    bounded."""
    result = score_entity_list(mailroom_profile, "name", value, ["Acme"])
    assert result.n_predicted == len(as_list(value))
    assert 0.0 <= result.score <= 1.0


@pytest.mark.parametrize("element_type", ["name", "free_text", "id", "date", "cms_doc_ref"])
def test_ten_thousand_items_and_huge_strings_finish_in_time(mailroom_profile, element_type):
    """Ten thousand predicted items against three expected, and a megabyte item, finish within the budget."""
    rng = random.Random(element_type)
    predicted = [f"Item {rng.randint(0, 10**6)} Holdings" for _ in range(10_000)]
    started = time.perf_counter()
    result = score_entity_list(
        mailroom_profile, element_type, predicted, ["Item 5 Holdings", "Other", "2014-01-01"]
    )
    huge = score_entity_list(
        mailroom_profile, element_type, ["x " * 500_000, "Acme"], ["Acme", "y" * 100_000]
    )
    assert time.perf_counter() - started < 30.0
    assert result.n_predicted == 10_000 and 0.0 <= result.f1 <= 1.0 and 0.0 <= huge.f1 <= 1.0


def test_a_named_copy_keeps_every_metric(mailroom_profile):
    """named() only sets the field name."""
    result = score_entity_list(mailroom_profile, "name", ["Acme"], ["Acme", "Beta"])
    named = result.named("parties")
    assert isinstance(named, EntityListScore) and named.field_name == "parties"
    assert {**named.to_dict(), "field": ""} == result.to_dict()


def test_role_phrases_have_one_to_three_role_words(mailroom_profile):
    """A three-word role phrase is credited; a four-word one is not."""
    three = score_entity_list(
        mailroom_profile, "id", ["Acme Corp"], ["Licensee Licensor Party", "Omega"], recall_coverage=True
    )
    four = score_entity_list(
        mailroom_profile,
        "id",
        ["Acme Corp"],
        ["Licensee Licensor Party Vendor", "Omega"],
        recall_coverage=True,
    )
    assert (three.matched, four.matched) == (1, 0)


def test_contained_labels_stop_at_six_tokens(mailroom_profile):
    """With an all-or-nothing element type only the contained-label rule can credit these labels."""
    predicted = ["the Seller shall deliver the goods within thirty days"]
    six = score_entity_list(
        mailroom_profile, "id", predicted, ["Seller shall deliver the goods within"], recall_coverage=True
    )
    seven = score_entity_list(
        mailroom_profile,
        "id",
        predicted,
        ["Seller shall deliver the goods within thirty"],
        recall_coverage=True,
    )
    assert (six.matched, seven.matched) == (1, 0)


def test_free_text_items_quoting_the_label_score_their_coverage(mailroom_profile):
    """An item holding exactly the coverage share of the label's tokens scores that share."""
    from mailroom_dojo.field_scoring.scalar_scorers import score_free_text_field

    label = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    item = "alpha bravo charlie delta echo foxtrot golf " + " ".join(f"filler{index}" for index in range(30))
    similarity = entity_lists.element_similarity(
        mailroom_profile, "free_text", item, label, scorer=score_free_text_field
    )
    assert score_free_text_field(item, label) < 0.7 and similarity == 0.7


def test_the_greedy_matcher_lets_a_later_equal_candidate_win(mailroom_profile):
    """On a tie the greedy matcher takes the later expected item, as the old fallback did."""
    result = score_entity_list(
        mailroom_profile, "name", ["Acme Beta", "Acme"], ["Acme", "Beta"], matcher="greedy"
    )
    assert result.matched == 2
