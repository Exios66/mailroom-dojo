"""Bad inputs to the statistics layer: each one gives an exact answer (None, a dropped row) or an exact error.

Empty input, NaN and infinities, mismatched lengths, one-class populations, bad
parameters and wrong types. Where an error is raised, its type and message are asserted.
"""

from __future__ import annotations

import math
import re

import numpy
import pytest

from mailroom_dojo.statistics import (
    auroc,
    bootstrap_ci,
    brier_score,
    clopper_pearson_upper,
    cross_fit_cutoffs,
    cutoff_at_target,
    cutoff_for_precision,
    delta_significance,
    expected_calibration_error,
    reliability_bins,
    required_samples,
    spearman,
    wilson_bounds,
    wilson_ci,
)

NAN = float("nan")
INF = float("inf")


def raises_exactly(error: type[Exception], message: str):
    """``pytest.raises`` matching the whole message literally."""
    return pytest.raises(error, match="^" + re.escape(message) + "$")


# --- empty and too-small input ----------------------------------------------


@pytest.mark.parametrize("values", [[], [None], [None, None, 0.5], ["x", {}, 1.0]])
def test_bootstrap_ci_under_two_usable_values_is_none(values):
    """Fewer than two usable values: no interval, not a zero-width one."""
    assert bootstrap_ci(values) is None


def test_empty_populations_give_none_everywhere():
    """Nothing to measure is None (or the documented empty result), never a number."""
    assert delta_significance([], [1.0, 0.0]) is None
    assert expected_calibration_error([]) is None
    assert brier_score([]) is None
    assert auroc([], []) is None
    assert spearman([], []) is None
    assert cutoff_at_target([], 0.9) is None
    assert clopper_pearson_upper(0, 0) is None
    assert [item.n for item in reliability_bins([], bins=3)] == [0, 0, 0]
    assert cutoff_for_precision([], [], 0.9).to_dict() == {
        "cutoff": None,
        "precision": None,
        "accept_rate": 0.0,
        "recall": 0.0,
    }
    empty_fit = cross_fit_cutoffs([])
    assert empty_fit.cutoff is None and empty_fit.holdout_documents == 0 and empty_fit.meets_target is False


# --- NaN and infinities -----------------------------------------------------


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([1.0, NAN, 0.0], "values[1] is not a finite number: nan"),
        ([INF, 0.5], "values[0] is not a finite number: inf"),
        ([0.5, None, -INF], "values[2] is not a finite number: -inf"),
        ([0.1, "nan", 0.9], "values[1] is not a finite number: 'nan'"),
        ([numpy.float64("nan"), 1.0], "values[0] is not a finite number: np.float64(nan)"),
    ],
)
def test_bootstrap_ci_refuses_a_non_finite_score(values, message):
    """A NaN or infinite score would poison every resampled mean: refused, naming its position."""
    with raises_exactly(ValueError, message):
        bootstrap_ci(values)


def test_delta_significance_names_the_side_holding_the_nan():
    """The message names which run's list holds the bad value."""
    with raises_exactly(ValueError, "values_b[0] is not a finite number: nan"):
        delta_significance([0.1, 0.2], [NAN, 0.3])
    with raises_exactly(ValueError, "values_a[1] is not a finite number: inf"):
        delta_significance([0.1, INF], [0.2, 0.3])


def test_an_integer_too_large_for_a_float_still_overflows():
    """Kept from the old Dojo: ``float(10**400)`` raises OverflowError."""
    with raises_exactly(OverflowError, "int too large to convert to float"):
        bootstrap_ci([10**400, 1.0])


@pytest.mark.parametrize(
    ("p", "message"),
    [(NAN, "p must be a rate in [0, 1]; got nan"), (INF, "p must be a rate in [0, 1]; got inf")],
)
def test_wilson_refuses_a_non_finite_rate(p, message):
    """The old Dojo answered [0, 1] for a NaN rate; now it is an error."""
    with raises_exactly(ValueError, message):
        wilson_ci(p, 10)
    with raises_exactly(ValueError, message):
        wilson_bounds(p, 10)


def test_cutoff_searches_refuse_a_non_finite_confidence():
    """A NaN confidence has no place in a sorted cutoff search."""
    rows = [(0.9, True), (NAN, False), (0.5, True)]
    with raises_exactly(ValueError, "rows[1] has a non-finite confidence: nan"):
        cutoff_at_target(rows, 0.8)
    with raises_exactly(ValueError, "rows[1] has a non-finite confidence: nan"):
        cross_fit_cutoffs(rows)
    with raises_exactly(ValueError, "scores[2] is not a finite number: inf"):
        cutoff_for_precision([0.2, 0.4, INF], [True, False, True], 0.9)


def test_calibration_drops_unusable_confidences_and_ranking_drops_nan():
    """ECE and Brier leave out confidences that are not finite numbers in [0, 1]; ranking drops NaN."""
    noisy = [
        (NAN, True),
        (INF, False),
        (-0.1, True),
        (1.1, False),
        (True, True),
        ("0.5", False),
        (None, True),
    ]
    assert expected_calibration_error(noisy) is None
    assert brier_score(noisy) is None
    assert expected_calibration_error([*noisy, (0.75, True)]) == 0.25
    assert auroc([0.3, NAN, 0.7, 0.2], [False, True, True, False]) == 1.0
    assert spearman([1, NAN, 2, 3], [1, 5, 2, 3]) == 1.0


# --- mismatched lengths -----------------------------------------------------


def test_mismatched_lengths_are_refused_with_the_exact_message():
    """Inputs read in pairs must line up."""
    with raises_exactly(ValueError, "scores and labels must have the same length"):
        auroc([0.1, 0.2], [True])
    with raises_exactly(ValueError, "xs and ys must have the same length"):
        spearman([1, 2, 3], [1, 2])
    with raises_exactly(ValueError, "scores and labels must have the same length"):
        cutoff_for_precision([0.1], [True, False], 0.9)


def test_unpaired_delta_accepts_different_lengths():
    """The two-sample test is unpaired by design: different sample sizes are fine."""
    out = delta_significance([0.0, 1.0], [1.0, 1.0, 0.0, 1.0], n_boot=50)
    assert (out.n_a, out.n_b) == (2, 4)


# --- one-class populations --------------------------------------------------


def test_one_class_populations_have_no_ranking_power():
    """All right or all wrong: AUROC is None (nothing to rank); ECE and Brier are still defined."""
    assert auroc([0.9, 0.8, 0.1], [True, True, True]) is None
    assert auroc([0.9, 0.8, 0.1], [False, False, False]) is None
    assert expected_calibration_error([(0.9, True), (0.6, True)]) == 0.25
    assert brier_score([(0.9, True), (0.6, True)]) == 0.085
    assert spearman([1, 1, 1], [1, 2, 3]) is None


def test_all_wrong_and_all_right_bounds():
    """Every accept wrong bounds the error rate at 1.0; a perfect class gets the closed-form sample size."""
    assert clopper_pearson_upper(5, 5) == 1.0
    assert required_samples(0) == 299
    assert cutoff_at_target([(0.9, False), (0.5, False)], 0.5) is None


# --- bad parameters and types -----------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"n_boot": 0}, ValueError, "n_boot must be at least 1; got 0"),
        ({"n_boot": 2.5}, TypeError, "n_boot must be an integer"),
        ({"n_boot": True}, TypeError, "n_boot must be an integer"),
        ({"alpha": 0.0}, ValueError, "alpha must be in (0, 1); got 0.0"),
        ({"alpha": 1.5}, ValueError, "alpha must be in (0, 1); got 1.5"),
        ({"alpha": NAN}, ValueError, "alpha must be in (0, 1); got nan"),
        ({"seed": "42"}, TypeError, "seed must be an integer"),
        ({"seed": None}, TypeError, "seed must be an integer"),
    ],
)
def test_resampling_parameters_are_checked_by_both_bootstraps(kwargs, error, message):
    """The same parameter rules and messages for both bootstraps."""
    with raises_exactly(error, message):
        bootstrap_ci([0.1, 0.2], **kwargs)
    with raises_exactly(error, message):
        delta_significance([0.1, 0.2], [0.3, 0.4], **kwargs)


@pytest.mark.parametrize(
    ("args", "error", "message"),
    [
        ((1.1, 10), ValueError, "p must be a rate in [0, 1]; got 1.1"),
        ((-0.1, 10), ValueError, "p must be a rate in [0, 1]; got -0.1"),
        ((True, 10), TypeError, "p must be a real number; got True"),
        ((0.5, 2.5), TypeError, "n must be an integer; got 2.5"),
        ((0.5, 10.0), TypeError, "n must be an integer; got 10.0"),
        ((0.5, True), TypeError, "n must be an integer; got True"),
        ((0.5, 10, -1.0), ValueError, "z must be non-negative; got -1.0"),
        ((0.5, 10, NAN), ValueError, "z must be non-negative; got nan"),
        ((0.5, 10, INF), ValueError, "z must be finite; got inf"),
    ],
)
def test_wilson_argument_errors(args, error, message):
    """A rate, a support and a z that cannot describe an interval are refused."""
    with raises_exactly(error, message):
        wilson_ci(*args)


def test_wilson_none_and_zero_support():
    """No rate or no trials is no interval; the exact-bounds function refuses zero trials."""
    assert wilson_ci(None, 10) is None
    assert wilson_ci(0.5, 0) is None
    assert wilson_ci(0.5, -3) is None
    with raises_exactly(ValueError, "n must be at least 1; got 0"):
        wilson_bounds(0.5, 0)


def test_numpy_counts_and_scores_are_accepted():
    """NumPy integers count as integers and NumPy numbers as scores."""
    assert wilson_ci(0.5, numpy.int64(10)) == wilson_ci(0.5, 10)
    assert clopper_pearson_upper(numpy.int32(1), numpy.int64(405)) == clopper_pearson_upper(1, 405)
    assert auroc([numpy.float32(0.9), numpy.int64(0)], [True, False]) == 1.0


@pytest.mark.parametrize(
    ("call", "error", "message"),
    [
        (lambda: clopper_pearson_upper(True, 10), TypeError, "errors must be an integer"),
        (lambda: clopper_pearson_upper(1, 10.5), TypeError, "n must be an integer"),
        (lambda: clopper_pearson_upper(11, 10), ValueError, "errors (11) cannot exceed n (10)"),
        (lambda: clopper_pearson_upper(-1, 10), ValueError, "errors must be between 0 and infinity"),
        (
            lambda: clopper_pearson_upper(1, 10, confidence=NAN),
            ValueError,
            "confidence must be in (0, 1); got nan",
        ),
        # The old Dojo's message names "confidence" for a bad risk too; kept for parity.
        (lambda: required_samples(0, risk=1.0), ValueError, "confidence must be in (0, 1); got 1.0"),
        (lambda: cutoff_at_target([(0.9, True)], 0.0), ValueError, "target must be in (0, 1]; got 0.0"),
        (lambda: cross_fit_cutoffs([(0.9, True)], folds=1), ValueError, "folds must be at least 2"),
        (lambda: expected_calibration_error([(0.5, True)], bins=0), ValueError, "bins must be positive"),
        (lambda: reliability_bins([(0.5, True)], bins=-1), ValueError, "bins must be positive"),
        (
            lambda: cutoff_for_precision([0.5], [True], 0.0),
            ValueError,
            "precision must be in (0, 1]; got 0.0",
        ),
    ],
)
def test_parameter_errors_have_exact_messages(call, error, message):
    """Each refusal names the parameter and the bad value."""
    with raises_exactly(error, message):
        call()


def test_required_samples_for_a_vanishing_risk_is_none():
    """A risk near the float minimum needs more than the search cap: None, not an overflow."""
    assert required_samples(0, risk=1e-300) is None
    assert math.isfinite(clopper_pearson_upper(0, 1_000_000))
