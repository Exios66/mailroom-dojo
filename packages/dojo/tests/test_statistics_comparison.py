"""Comparing runs and confidence by group: the statistics the old Dojo did not have.

The design's "Comparing runs" layer pairs two runs by document, with a bootstrap
interval for score differences and a paired test for outcome flips; its "Confidence"
layer reports calibration error and ranking power per agent and class.

``paired_bootstrap_delta`` is checked by hand on constant differences, against the
ported percentile bootstrap run on the per-document differences, and against the
unpaired test on runs whose documents vary in difficulty. ``outcome_flip_test`` is
checked by hand on small tables and against ``scipy.stats.binomtest``.
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


# --- paired bootstrap of per-document differences ----------------------------


def test_constant_differences_give_a_point_interval():
    """Every document gained 0.25: the interval is exactly [0.25, 0.25] and significant."""
    out = paired_bootstrap_delta([0.5, 0.25, 0.0], [0.75, 0.5, 0.25])
    assert out.to_dict() == {
        "delta": 0.25,
        "ci_lo": 0.25,
        "ci_hi": 0.25,
        "significant": True,
        "n_pairs": 3,
        "dropped_pairs": 0,
        "seed": 42,
        "n_boot": 2000,
        "alpha": 0.05,
        "method": "paired-percentile-bootstrap",
    }


def test_identical_runs_show_no_difference():
    """Two identical runs: delta 0, interval [0, 0], not significant (zero is not excluded)."""
    scores = [0.1, 0.9, 0.4, 0.4, 1.0]
    out = paired_bootstrap_delta(scores, list(scores))
    assert (out.delta, out.ci_lo, out.ci_hi, out.significant) == (0.0, 0.0, 0.0, False)


def test_the_interval_is_the_percentile_bootstrap_of_the_differences():
    """Same resampler and seed as bootstrap_ci, applied to b - a per document."""
    rng = random.Random(5)
    a = [round(rng.random(), 3) for _ in range(40)]
    b = [round(rng.random(), 3) for _ in range(40)]
    paired = paired_bootstrap_delta(a, b, n_boot=500, seed=9)
    direct = bootstrap_ci([y - x for x, y in zip(a, b, strict=True)], n_boot=500, seed=9)
    assert (paired.ci_lo, paired.ci_hi) == (direct.lo, direct.hi)
    assert paired.delta == round(sum(b) / 40 - sum(a) / 40, 4)


def test_pairing_detects_a_small_gain_the_unpaired_test_misses():
    """Documents differ a lot in difficulty, B adds 0.03 to each: only the paired test sees it."""
    rng = random.Random(11)
    a = [round(rng.random(), 4) for _ in range(30)]
    b = [x + 0.03 for x in a]
    assert paired_bootstrap_delta(a, b).significant is True
    assert delta_significance(a, b).significant is False


def test_unpaired_documents_are_dropped_and_counted():
    """A document scored in only one run cannot be paired."""
    out = paired_bootstrap_delta([0.5, None, 0.2, 0.1], [0.6, 0.7, None, 0.3], n_boot=100)
    assert (out.n_pairs, out.dropped_pairs) == (2, 2)
    assert paired_bootstrap_delta([0.5, None], [None, 0.5]) is None
    assert paired_bootstrap_delta([0.5, 0.2], [0.6, None]) is None


def test_booleans_are_scores():
    """A right/wrong outcome is a 1.0/0.0 score."""
    out = paired_bootstrap_delta([False, False, True, False], [True, True, True, False], n_boot=200)
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
    out = outcome_flip_test(outcomes_a, outcomes_b)
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
        out = outcome_flip_test([True] * a_only + [False] * b_only, [False] * a_only + [True] * b_only)
        expected = binomtest(b_only, flips, 0.5).pvalue
        assert out.p_value == pytest.approx(expected, rel=1e-9, abs=1e-300)


def test_flip_test_is_symmetric_in_the_runs():
    """Swapping A and B keeps the p-value and negates the net."""
    a = [True, True, False, False, True, False, True]
    b = [False, True, True, True, True, True, False]
    forward, backward = outcome_flip_test(a, b), outcome_flip_test(b, a)
    assert forward.p_value == backward.p_value
    assert forward.net == -backward.net
    assert (forward.a_only, forward.b_only) == (backward.b_only, backward.a_only)


def test_flip_test_drops_unpaired_documents():
    """A None on either side leaves the document out and counts it."""
    out = outcome_flip_test([True, None, False, True], [False, True, None, True])
    assert (out.n_pairs, out.dropped_pairs, out.a_only, out.both_right) == (2, 2, 1, 1)
    assert outcome_flip_test([None], [True]) is None


def test_flip_test_alpha_decides_significance():
    """The same flips are significant at 0.05 and not at 0.01 (p = 0.03125)."""
    a, b = [False] * 6, [True] * 6
    assert outcome_flip_test(a, b).significant is True
    assert outcome_flip_test(a, b, alpha=0.01).significant is False


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
    assert paired_bootstrap_delta([], []) is None
    assert outcome_flip_test([], []) is None
    assert calibration_by_group([]) == {}


def test_paired_functions_refuse_nan_and_non_numbers():
    """Numbers (or bools) only; no silent coercion of text, unlike the old unpaired bootstrap."""
    with raises_exactly(ValueError, "values_b[1] is not a finite number: nan"):
        paired_bootstrap_delta([0.1, 0.2], [0.3, NAN])
    with raises_exactly(TypeError, "values_a[0] is not a number: '0.5'"):
        paired_bootstrap_delta(["0.5", 0.2], [0.3, 0.4])
    with raises_exactly(TypeError, "outcomes_b[1] must be a bool or None; got 1"):
        outcome_flip_test([True, False], [True, 1])
    with raises_exactly(TypeError, "outcomes_a[0] must be a bool or None; got 'yes'"):
        outcome_flip_test(["yes"], [True])


def test_mismatched_lengths_name_both_lengths():
    """Paired inputs are aligned by document; a length mismatch is a caller bug."""
    with raises_exactly(
        ValueError, "values_a and values_b must have the same length (one entry per document); got 3 and 2"
    ):
        paired_bootstrap_delta([0.1, 0.2, 0.3], [0.1, 0.2])
    with raises_exactly(
        ValueError,
        "outcomes_a and outcomes_b must have the same length (one entry per document); got 1 and 2",
    ):
        outcome_flip_test([True], [True, False])


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
        paired_bootstrap_delta([0.1, 0.2], [0.3, 0.4], **kwargs)


def test_parameter_errors_of_the_flip_test_and_groups():
    """alpha must be inside (0, 1); bins must be positive."""
    with raises_exactly(ValueError, "alpha must be in (0, 1); got 1.0"):
        outcome_flip_test([True], [True], alpha=1.0)
    with raises_exactly(ValueError, "bins must be positive"):
        calibration_by_group([("a", 0.5, True)], bins=0)


def test_numpy_bools_are_outcomes():
    """A NumPy boolean array is read as outcomes."""
    flips = outcome_flip_test(numpy.array([True, False]), numpy.array([False, True]))
    assert (flips.a_only, flips.b_only) == (1, 1)


# --- invariants (hypothesis) ---------------------------------------------------


@given(values=scores, other=scores, seed=seeds)
def test_a_seed_fixes_the_paired_result(values, other, seed):
    """Same seed, same answer, whatever ran in between."""
    paired_other = (other * len(values))[: len(values)]
    first = paired_bootstrap_delta(values, paired_other, n_boot=200, seed=seed)
    bootstrap_ci(other, n_boot=50, seed=seed + 1)  # an unrelated call in between
    assert paired_bootstrap_delta(values, paired_other, n_boot=200, seed=seed) == first


@given(a=scores, data=st.data(), seed=seeds)
def test_paired_delta_is_the_mean_difference_inside_an_ordered_interval(a, data, seed):
    """The point estimate is mean(b) - mean(a); the interval is ordered; swapping runs negates the delta."""
    b = data.draw(st.lists(unit, min_size=len(a), max_size=len(a)))
    out = paired_bootstrap_delta(a, b, n_boot=200, seed=seed)
    expected = sum(y - x for x, y in zip(a, b, strict=True)) / len(a)
    assert out.delta == round(expected, 4)
    assert out.ci_lo <= out.ci_hi
    assert out.significant is False or out.ci_lo >= 0 or out.ci_hi <= 0
    assert paired_bootstrap_delta(b, a, n_boot=200, seed=seed).delta == round(-expected, 4)


@given(outcomes=st.lists(st.tuples(st.booleans(), st.booleans()), min_size=1, max_size=80))
def test_flip_test_p_value_is_a_probability_and_ignores_agreements(outcomes):
    """p is in (0, 1]; documents both runs got right or wrong do not change it."""
    a = [first for first, _ in outcomes]
    b = [second for _, second in outcomes]
    out = outcome_flip_test(a, b)
    assert 0.0 < out.p_value <= 1.0
    assert out.n_pairs == len(outcomes) == out.both_right + out.both_wrong + out.a_only + out.b_only
    padded = outcome_flip_test([*a, True, False], [*b, True, False])
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
