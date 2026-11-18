"""Comparing runs and confidence by group: the statistics the old Dojo did not have.

The design's "Comparing runs" layer pairs two runs by document, with a bootstrap
interval for score differences and a paired test for outcome flips; its "Confidence"
layer reports calibration error and ranking power per agent and class.

Both paired functions take ``{document id: value}`` mappings and pair by id: a
document scored in only one run is refused unless the caller allows it, and the result
does not depend on the mappings' order. ``paired_bootstrap_delta`` is checked by hand on
constant differences, against the ported percentile bootstrap run on the per-document
differences, and against the unpaired test on runs whose documents vary in difficulty.
``outcome_flip_test`` is checked by hand on small tables, at its alpha boundary and
against ``scipy.stats.binomtest``.
``calibration_by_group`` is checked against the population functions. Bad input gets an
exact error; random input keeps the invariants (hypothesis).
"""

from __future__ import annotations

import random
import re

import numpy
import pytest
from hypothesis import given
from hypothesis import strategies as st
from scipy.stats import binomtest

from mailroom_dojo.statistics import (
    auroc,
    bootstrap_ci,
    brier_score,
    calibration_by_group,
    delta_significance,
    expected_calibration_error,
    outcome_flip_test,
    paired_bootstrap_delta,
)

NAN = float("nan")
unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
scores = st.lists(unit, min_size=2, max_size=40)
seeds = st.integers(min_value=0, max_value=2**32)


def raises_exactly(error: type[Exception], message: str):
    """``pytest.raises`` matching the whole message literally."""
    return pytest.raises(error, match="^" + re.escape(message) + "$")


def by_document(values):
    """``{document id: value}`` with ids ``doc-0000``, ``doc-0001``, ... (sorted = list order)."""
    return {f"doc-{index:04d}": value for index, value in enumerate(values)}


# --- paired bootstrap of per-document differences ----------------------------


def test_constant_differences_give_a_point_interval():
    """Every document gained 0.25: the interval is exactly [0.25, 0.25] and significant."""
    out = paired_bootstrap_delta(by_document([0.5, 0.25, 0.0]), by_document([0.75, 0.5, 0.25]))
    assert out.to_dict() == {
        "delta": 0.25,
        "ci_lo": 0.25,
        "ci_hi": 0.25,
        "significant": True,
        "n_pairs": 3,
        "dropped_pairs": 0,
        "unpaired_ids": [],
        "seed": 42,
        "n_boot": 2000,
        "alpha": 0.05,
        "method": "paired-percentile-bootstrap",
    }


def test_identical_runs_show_no_difference():
    """Two identical runs: delta 0, interval [0, 0], not significant (zero is not excluded)."""
    scores = [0.1, 0.9, 0.4, 0.4, 1.0]
    out = paired_bootstrap_delta(by_document(scores), by_document(scores))
    assert (out.delta, out.ci_lo, out.ci_hi, out.significant) == (0.0, 0.0, 0.0, False)


def test_the_interval_is_the_percentile_bootstrap_of_the_differences():
    """Same resampler and seed as bootstrap_ci, applied to b - a per document."""
    rng = random.Random(5)
    a = [round(rng.random(), 3) for _ in range(40)]
    b = [round(rng.random(), 3) for _ in range(40)]
    paired = paired_bootstrap_delta(by_document(a), by_document(b), n_boot=500, seed=9)
    direct = bootstrap_ci([y - x for x, y in zip(a, b, strict=True)], n_boot=500, seed=9)
    assert (paired.ci_lo, paired.ci_hi) == (direct.lo, direct.hi)
    assert paired.delta == round(sum(b) / 40 - sum(a) / 40, 4)


def test_pairing_detects_a_small_gain_the_unpaired_test_misses():
    """Documents differ a lot in difficulty, B adds 0.03 to each: only the paired test sees it."""
    rng = random.Random(11)
    a = [round(rng.random(), 4) for _ in range(30)]
    b = [x + 0.03 for x in a]
    assert paired_bootstrap_delta(by_document(a), by_document(b)).significant is True
    assert delta_significance(a, b).significant is False


def test_unpaired_documents_are_refused_by_default():
    """A document in one run only, or None in one run, is a caller error unless allowed."""
    with raises_exactly(
        ValueError,
        "2 documents are not scored in both runs: ['b', 'c']; pass allow_unpaired=True to leave them out",
    ):
        paired_bootstrap_delta({"a": 0.5, "b": 0.2}, {"a": 0.6, "c": 0.3})
    with raises_exactly(
        ValueError,
        "1 documents are not scored in both runs: ['b']; pass allow_unpaired=True to leave them out",
    ):
        paired_bootstrap_delta({"a": 0.5, "b": 0.2, "c": 0.1}, {"a": 0.6, "b": None, "c": 0.3})


def test_allowed_unpaired_documents_are_left_out_and_named():
    """With allow_unpaired=True the unpaired ids are returned, sorted, and counted."""
    out = paired_bootstrap_delta(
        {"d": 0.1, "a": 0.5, "b": None, "c": 0.2},
        {"a": 0.6, "b": 0.7, "d": 0.3, "e": 0.9},
        n_boot=100,
        allow_unpaired=True,
    )
    assert (out.n_pairs, out.dropped_pairs, out.unpaired_ids) == (2, 3, ("b", "c", "e"))
    assert out.to_dict()["unpaired_ids"] == ["b", "c", "e"]
    assert paired_bootstrap_delta({"a": 0.5, "b": None}, {"a": None, "b": 0.5}, allow_unpaired=True) is None
    assert paired_bootstrap_delta({"a": 0.5, "b": 0.2}, {"a": 0.6}, allow_unpaired=True) is None


def test_paired_results_do_not_depend_on_mapping_order():
    """Documents are taken in sorted id order, so insertion order cannot move the interval."""
    rng = random.Random(3)
    ids = [f"doc-{rng.randrange(10**6):06d}" for _ in range(30)]
    a = {doc: round(rng.random(), 3) for doc in ids}
    b = {doc: round(rng.random(), 3) for doc in ids}
    reversed_a = dict(reversed(list(a.items())))
    shuffled = list(b.items())
    rng.shuffle(shuffled)
    forward = paired_bootstrap_delta(a, b, n_boot=300, seed=4)
    assert paired_bootstrap_delta(reversed_a, dict(shuffled), n_boot=300, seed=4) == forward
    in_sorted_order = [b[doc] - a[doc] for doc in sorted(ids)]
    direct = bootstrap_ci(in_sorted_order, n_boot=300, seed=4)
    assert (forward.ci_lo, forward.ci_hi) == (direct.lo, direct.hi)
    outcomes_a = {doc: value > 0.5 for doc, value in a.items()}
    outcomes_b = {doc: value > 0.5 for doc, value in b.items()}
    assert outcome_flip_test(outcomes_a, outcomes_b) == outcome_flip_test(
        dict(reversed(list(outcomes_a.items()))), outcomes_b
    )


def test_paired_functions_refuse_sequences_and_unorderable_ids():
    """Position is not a document id; ids must sort so the result is deterministic."""
    message = "values_a and values_b must be mappings from document id to value"
    with raises_exactly(TypeError, message):
        paired_bootstrap_delta([0.1, 0.2], [0.3, 0.4])
    with raises_exactly(TypeError, "outcomes_a and outcomes_b must be mappings from document id to value"):
        outcome_flip_test({"a": True}, [True])
    with raises_exactly(TypeError, "document ids must be mutually orderable; got ids of types int, str"):
        paired_bootstrap_delta({"a": 0.1, 1: 0.2}, {"a": 0.3, 1: 0.4})


def test_booleans_are_scores():
    """A right/wrong outcome is a 1.0/0.0 score."""
    out = paired_bootstrap_delta(
        by_document([False, False, True, False]), by_document([True, True, True, False]), n_boot=200
    )
    assert out.delta == 0.5


# --- exact McNemar test of outcome flips --------------------------------------


@pytest.mark.parametrize(
    ("a_only", "b_only", "p_value"),
    [(0, 6, 0.03125), (0, 5, 0.0625), (1, 1, 1.0), (0, 0, 1.0), (2, 9, 0.06542969), (10, 0, 0.00195312)],
)
def test_flip_test_hand_computed_p_values(a_only, b_only, p_value):
    """Exact McNemar: twice the binomial lower tail of the smaller flip count, capped at 1."""
    outcomes_a = [True] * a_only + [False] * b_only + [True] * 3 + [False] * 2
    outcomes_b = [False] * a_only + [True] * b_only + [True] * 3 + [False] * 2
    out = outcome_flip_test(by_document(outcomes_a), by_document(outcomes_b))
    assert out.p_value == pytest.approx(p_value, abs=1e-8)
    assert (out.a_only, out.b_only, out.both_right, out.both_wrong) == (a_only, b_only, 3, 2)
    assert out.net == b_only - a_only
    assert out.significant is (out.p_value < 0.05)


@pytest.mark.parametrize("flips", [1, 2, 7, 20, 51, 300, 2001])
def test_flip_test_agrees_with_scipy_binomtest(flips):
    """The p-value equals scipy's exact two-sided binomial test at p = 1/2."""
    rng = random.Random(flips)
    for _ in range(5):
        b_only = rng.randrange(flips + 1)
        a_only = flips - b_only
        out = outcome_flip_test(
            by_document([True] * a_only + [False] * b_only), by_document([False] * a_only + [True] * b_only)
        )
        expected = binomtest(b_only, flips, 0.5).pvalue
        assert out.p_value == pytest.approx(expected, rel=1e-9, abs=1e-300)


def test_flip_test_is_symmetric_in_the_runs():
    """Swapping A and B keeps the p-value and negates the net."""
    a = [True, True, False, False, True, False, True]
    b = [False, True, True, True, True, True, False]
    forward = outcome_flip_test(by_document(a), by_document(b))
    backward = outcome_flip_test(by_document(b), by_document(a))
    assert forward.p_value == backward.p_value
    assert forward.net == -backward.net
    assert (forward.a_only, forward.b_only) == (backward.b_only, backward.a_only)


def test_flip_test_refuses_unpaired_documents_unless_allowed():
    """A None or a missing id on either side is refused; allowed, it is left out and named."""
    a, b = {"w": True, "x": None, "y": False, "z": True}, {"w": False, "x": True, "y": None, "z": True}
    with raises_exactly(
        ValueError,
        "2 documents are not scored in both runs: ['x', 'y']; pass allow_unpaired=True to leave them out",
    ):
        outcome_flip_test(a, b)
    out = outcome_flip_test(a, b, allow_unpaired=True)
    assert (out.n_pairs, out.dropped_pairs, out.a_only, out.both_right) == (2, 2, 1, 1)
    assert out.unpaired_ids == ("x", "y")
    assert outcome_flip_test({"a": None}, {"a": True}, allow_unpaired=True) is None
    assert outcome_flip_test({"a": True}, {"b": True}, allow_unpaired=True) is None


def test_flip_test_alpha_decides_significance():
    """The same flips are significant at 0.05 and not at 0.01 (p = 0.03125)."""
    a, b = by_document([False] * 6), by_document([True] * 6)
    assert outcome_flip_test(a, b).significant is True
    assert outcome_flip_test(a, b, alpha=0.01).significant is False


def test_a_p_value_equal_to_alpha_is_not_significant():
    """Significant means p < alpha, strictly: five flips one way give p = 0.0625 exactly."""
    a, b = by_document([False] * 5), by_document([True] * 5)
    assert outcome_flip_test(a, b).p_value == 0.0625
    assert outcome_flip_test(a, b, alpha=0.0625).significant is False
    assert outcome_flip_test(a, b, alpha=0.0625000001).significant is True


# --- calibration and ranking power by group -----------------------------------


def test_calibration_by_group_reports_each_group_in_first_seen_order():
    """Keys are whatever the caller groups by (here agent and class); rows that cannot be used are counted."""
    rows = [
        (("sorter", "contract"), 0.9, True),
        (("judge", "contract"), 0.8, False),
        (("sorter", "contract"), 0.2, False),
        (("sorter", "contract"), NAN, True),
        (("judge", "contract"), 0.6, True),
    ]
    groups = calibration_by_group(rows)
    assert list(groups) == [("sorter", "contract"), ("judge", "contract")]
    sorter = groups[("sorter", "contract")]
    assert sorter.to_dict() == {"n": 2, "dropped": 1, "ece": 0.15, "brier": 0.025, "auroc": 1.0}
    assert groups[("judge", "contract")].auroc == 0.0  # its confidence ranks backwards


def test_a_one_class_group_has_calibration_but_no_ranking_power():
    """All right: ECE and Brier are defined, AUROC is None."""
    groups = calibration_by_group([("sorter", 0.9, True), ("sorter", 0.6, True)])
    assert groups["sorter"].auroc is None
    assert groups["sorter"].ece == 0.25
    assert groups["sorter"].brier == 0.085


# --- bad input -----------------------------------------------------------------


def test_empty_input_gives_none_or_no_groups():
    """Nothing to compare is None, never a zero."""
    assert paired_bootstrap_delta({}, {}) is None
    assert outcome_flip_test({}, {}) is None
    assert calibration_by_group([]) == {}


def test_paired_functions_refuse_nan_and_non_numbers():
    """Numbers (or bools) only; no silent coercion of text, unlike the old unpaired bootstrap."""
    with raises_exactly(ValueError, "values_b['doc-0001'] is not a finite number: nan"):
        paired_bootstrap_delta(by_document([0.1, 0.2]), by_document([0.3, NAN]))
    with raises_exactly(TypeError, "values_a['doc-0000'] is not a number: '0.5'"):
        paired_bootstrap_delta(by_document(["0.5", 0.2]), by_document([0.3, 0.4]))
    with raises_exactly(TypeError, "outcomes_b['doc-0001'] must be a bool or None; got 1"):
        outcome_flip_test(by_document([True, False]), by_document([True, 1]))
    with raises_exactly(TypeError, "outcomes_a['doc-0000'] must be a bool or None; got 'yes'"):
        outcome_flip_test(by_document(["yes"]), by_document([True]))


def test_mismatched_document_sets_name_the_missing_ids():
    """A document present in one run only is reported by id (at most five named)."""
    with raises_exactly(
        ValueError,
        "7 documents are not scored in both runs: ['doc-0003', 'doc-0004', 'doc-0005', 'doc-0006', "
        "'doc-0007'] and 2 more; pass allow_unpaired=True to leave them out",
    ):
        paired_bootstrap_delta(by_document([0.1] * 10), by_document([0.2] * 3))


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"n_boot": 0}, ValueError, "n_boot must be at least 1; got 0"),
        ({"n_boot": 2.5}, TypeError, "n_boot must be an integer"),
        ({"alpha": 1.5}, ValueError, "alpha must be in (0, 1); got 1.5"),
        ({"seed": None}, TypeError, "seed must be an integer"),
    ],
)
def test_paired_bootstrap_checks_its_resampling_parameters(kwargs, error, message):
    """The same rules and messages as the ported bootstraps."""
    with raises_exactly(error, message):
        paired_bootstrap_delta(by_document([0.1, 0.2]), by_document([0.3, 0.4]), **kwargs)


def test_parameter_errors_of_the_flip_test_and_groups():
    """alpha must be inside (0, 1); bins must be positive."""
    with raises_exactly(ValueError, "alpha must be in (0, 1); got 1.0"):
        outcome_flip_test({"a": True}, {"a": True}, alpha=1.0)
    with raises_exactly(ValueError, "bins must be positive"):
        calibration_by_group([("a", 0.5, True)], bins=0)


def test_numpy_bools_are_outcomes():
    """NumPy booleans are read as outcomes."""
    flips = outcome_flip_test(
        by_document(list(numpy.array([True, False]))), by_document(list(numpy.array([False, True])))
    )
    assert (flips.a_only, flips.b_only) == (1, 1)


# --- invariants (hypothesis) ---------------------------------------------------


@given(values=scores, other=scores, seed=seeds)
def test_a_seed_fixes_the_paired_result(values, other, seed):
    """Same seed, same answer, whatever ran in between."""
    paired_other = by_document((other * len(values))[: len(values)])
    first = paired_bootstrap_delta(by_document(values), paired_other, n_boot=200, seed=seed)
    bootstrap_ci(other, n_boot=50, seed=seed + 1)  # an unrelated call in between
    assert paired_bootstrap_delta(by_document(values), paired_other, n_boot=200, seed=seed) == first


@given(a=scores, data=st.data(), seed=seeds)
def test_paired_delta_is_the_mean_difference_inside_an_ordered_interval(a, data, seed):
    """The point estimate is mean(b) - mean(a); the interval is ordered; swapping runs negates the delta."""
    b = data.draw(st.lists(unit, min_size=len(a), max_size=len(a)))
    out = paired_bootstrap_delta(by_document(a), by_document(b), n_boot=200, seed=seed)
    expected = sum(y - x for x, y in zip(a, b, strict=True)) / len(a)
    assert out.delta == round(expected, 4)
    assert out.ci_lo <= out.ci_hi
    assert out.significant is False or out.ci_lo >= 0 or out.ci_hi <= 0
    swapped = paired_bootstrap_delta(by_document(b), by_document(a), n_boot=200, seed=seed)
    assert swapped.delta == round(-expected, 4)


@given(outcomes=st.lists(st.tuples(st.booleans(), st.booleans()), min_size=1, max_size=80))
def test_flip_test_p_value_is_a_probability_and_ignores_agreements(outcomes):
    """p is in (0, 1]; documents both runs got right or wrong do not change it."""
    a = [first for first, _ in outcomes]
    b = [second for _, second in outcomes]
    out = outcome_flip_test(by_document(a), by_document(b))
    assert 0.0 < out.p_value <= 1.0
    assert out.n_pairs == len(outcomes) == out.both_right + out.both_wrong + out.a_only + out.b_only
    padded = outcome_flip_test(by_document([*a, True, False]), by_document([*b, True, False]))
    assert padded.p_value == out.p_value


@given(rows=st.lists(st.tuples(st.sampled_from(["sorter", "judge"]), unit, st.booleans()), max_size=60))
def test_calibration_by_group_matches_the_population_functions(rows):
    """Each group's numbers are the population functions applied to that group's rows."""
    groups = calibration_by_group(rows)
    assert set(groups) == {group for group, _, _ in rows}
    for group, summary in groups.items():
        own = [(confidence, correct) for key, confidence, correct in rows if key == group]
        assert summary.n == len(own) and summary.dropped == 0
        assert summary.ece == expected_calibration_error(own)
        assert summary.brier == brier_score(own)
        assert summary.auroc == auroc([c for c, _ in own], [k for _, k in own])
