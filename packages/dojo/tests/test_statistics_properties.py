"""Invariants of the statistics layer over random inputs (hypothesis).

Intervals contain their point estimate and narrow as the sample grows; calibration
error, Brier score and AUROC stay in ``[0, 1]``; ranking measures respond to reversal
as they must; bounds and cutoffs keep their promises; a seed fixes every result and
no call leaves state behind for the next.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

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

unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
scores = st.lists(unit, min_size=2, max_size=40)
seeds = st.integers(min_value=0, max_value=2**32)
pairs = st.lists(st.tuples(unit, st.booleans()), min_size=1, max_size=60)


@given(
    centre=st.floats(min_value=0.2, max_value=0.8),
    offsets=st.lists(st.floats(min_value=0.0, max_value=0.2), min_size=1, max_size=15),
    seed=seeds,
)
def test_interval_contains_the_mean_of_symmetric_data(centre, offsets, seed):
    """Data mirrored around its mean: the bootstrap interval holds the mean."""
    values = [centre + offset for offset in offsets] + [centre - offset for offset in offsets]
    mean = sum(values) / len(values)
    ci = bootstrap_ci(values, n_boot=400, seed=seed)
    assert ci.lo <= round(mean, 4) <= ci.hi
    assert ci.half >= 0


@given(values=scores, seed=seeds)
def test_bootstrap_interval_narrows_when_the_sample_grows(values, seed):
    """The same empirical distribution over nine times the documents: the interval is no wider."""
    small = bootstrap_ci(values, n_boot=300, seed=seed)
    large = bootstrap_ci(values * 9, n_boot=300, seed=seed)
    assert large.hi - large.lo <= small.hi - small.lo + 1e-4
    # Resampled means stay between the extremes (up to the 4-place rounding of the bounds).
    assert min(values) - 5e-5 <= small.lo <= small.hi <= max(values) + 5e-5


@given(p=unit, n=st.integers(min_value=1, max_value=5000), factor=st.integers(min_value=2, max_value=50))
def test_wilson_interval_shrinks_with_n_and_contains_the_rate(p, n, factor):
    """Exact Wilson bounds hold the rate, and more trials at the same rate give a narrower interval."""
    lo, hi = wilson_bounds(p, n)
    lo_more, hi_more = wilson_bounds(p, n * factor)
    assert 0.0 <= lo <= p + 1e-12 and p - 1e-12 <= hi <= 1.0
    assert hi_more - lo_more < hi - lo
    rounded = wilson_ci(p, n)
    assert rounded.lo <= lo and rounded.hi >= hi  # rounded outward


@given(pairs=pairs, bins=st.integers(min_value=1, max_value=20))
def test_calibration_measures_stay_in_the_unit_interval(pairs, bins):
    """ECE and Brier are in [0, 1]; the reliability bins count every pair once and their gaps make the ECE."""
    ece = expected_calibration_error(pairs, bins=bins)
    brier = brier_score(pairs)
    assert 0.0 <= ece <= 1.0
    assert 0.0 <= brier <= 1.0
    table = reliability_bins(pairs, bins=bins)
    assert len(table) == bins
    assert sum(item.n for item in table) == len(pairs)
    weighted = sum(item.n * item.gap for item in table if item.n) / len(pairs)
    assert abs(weighted - ece) <= 1e-5


@given(values=st.lists(st.tuples(unit, st.booleans()), min_size=2, max_size=60))
def test_auroc_is_a_probability_and_reverses_with_the_scores(values):
    """AUROC is in [0, 1], and negating every score gives one minus it."""
    confidences = [confidence for confidence, _ in values]
    labels = [label for _, label in values]
    assume(any(labels) and not all(labels))
    forward = auroc(confidences, labels)
    backward = auroc([-confidence for confidence in confidences], labels)
    assert 0.0 <= forward <= 1.0
    assert abs(forward + backward - 1.0) <= 2e-6


@given(xs=st.lists(st.integers(-5, 5), min_size=3, max_size=30), data=st.data())
def test_spearman_is_bounded_and_symmetric(xs, data):
    """Rank correlation is in [-1, 1], symmetric, and 1 against itself unless constant."""
    ys = data.draw(st.lists(st.integers(-5, 5), min_size=len(xs), max_size=len(xs)))
    rho = spearman(xs, ys)
    if rho is not None:
        assert -1.0 <= rho <= 1.0
        assert rho == spearman(ys, xs)
    assert spearman(xs, xs) in (None, 1.0)


@given(
    errors=st.integers(min_value=0, max_value=50),
    extra=st.integers(min_value=0, max_value=2000),
    confidence=st.floats(min_value=0.5, max_value=0.99),
)
def test_clopper_pearson_bound_is_above_the_observed_rate_and_grows_with_confidence(
    errors, extra, confidence
):
    """The upper bound is at least the observed error rate and loosens with a higher confidence."""
    n = errors + extra
    assume(n > 0)
    bound = clopper_pearson_upper(errors, n, confidence=confidence)
    assert errors / n <= bound <= 1.0
    assert clopper_pearson_upper(errors, n, confidence=min(0.999, confidence + 0.005)) >= bound


@given(errors=st.integers(min_value=0, max_value=6), risk=st.sampled_from([0.01, 0.02, 0.05, 0.1]))
def test_required_samples_is_the_smallest_sample_that_clears_the_risk(errors, risk):
    """The bound at the returned size clears the risk and one fewer does not."""
    needed = required_samples(errors, risk=risk)
    assert clopper_pearson_upper(errors, needed) <= risk
    assert needed - 1 < max(errors, 1) or clopper_pearson_upper(errors, needed - 1) > risk


@given(rows=pairs, target=st.sampled_from([0.5, 0.8, 0.9, 1.0]))
def test_cutoff_at_target_keeps_its_promise(rows, target):
    """At the returned cutoff the accepted rows reach the target; None means no positive cutoff does."""
    cutoff = cutoff_at_target(rows, target)
    candidates = sorted({confidence for confidence, _ in rows if confidence > 0})
    reaching = [
        level
        for level in candidates
        if sum(hit for confidence, hit in rows if confidence >= level)
        / sum(1 for confidence, _ in rows if confidence >= level)
        >= target
    ]
    assert cutoff == (min(reaching) if reaching else None)


@given(values=scores, other=scores, seed=seeds)
def test_a_seed_fixes_every_result_and_calls_leave_no_state(values, other, seed):
    """Same seed, same answer, whatever ran in between."""
    first = (
        bootstrap_ci(values, n_boot=200, seed=seed),
        delta_significance(values, other, n_boot=200, seed=seed),
        cross_fit_cutoffs([(value, value > 0.5) for value in values], seed=seed),
    )
    bootstrap_ci(other, n_boot=50, seed=seed + 1)  # an unrelated call in between
    second = (
        bootstrap_ci(values, n_boot=200, seed=seed),
        delta_significance(values, other, n_boot=200, seed=seed),
        cross_fit_cutoffs([(value, value > 0.5) for value in values], seed=seed),
    )
    assert first == second


@given(
    rows=st.lists(st.tuples(st.sampled_from([0.0, -0.0, 0.1, 0.5, 0.9, 1.0]) | unit, st.booleans()), max_size=60),
    precision=st.sampled_from([0.1, 0.5, 0.8, 0.9, 1.0]),
)
def test_cutoff_for_precision_is_the_lowest_cutoff_a_brute_force_search_finds(rows, precision):
    """Every distinct score tried from the lowest up, as the old Dojo did; the first that keeps the precision wins."""
    scores = [score for score, _ in rows]
    labels = [label for _, label in rows]
    found = cutoff_for_precision(scores, labels, precision).to_dict()
    positives = sum(labels)
    expected = {"cutoff": None, "precision": None, "accept_rate": 0.0, "recall": 0.0}
    for cutoff in sorted(set(scores)):
        accepted = [label for score, label in rows if score >= cutoff]
        if sum(accepted) / len(accepted) >= precision:
            expected = {
                "cutoff": cutoff,
                "precision": round(sum(accepted) / len(accepted), 6),
                "accept_rate": round(len(accepted) / len(rows), 6),
                "recall": round(sum(accepted) / positives, 6) if positives else None,
            }
            break
    assert found == expected
    assert repr(found["cutoff"]) == repr(expected["cutoff"])  # -0.0 and 0.0 kept apart
