"""Provenance tests for ``Measure``: every honesty rule refuses what it should and nothing more.

Property tests draw values, sample sizes, intervals and vocabulary words at random and
check each rule both ways: a measure that satisfies every rule builds and round-trips,
and one broken rule raises ``MeasureError`` naming it.
"""

from __future__ import annotations

import dataclasses
import decimal
import fractions
import math

import numpy
import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from mailroom_dojo.measures import (
    COST_BASES,
    LABEL_STATUS_WORDS,
    LABEL_STATUSES,
    PROVENANCE_KEYS,
    ConfidenceInterval,
    IncomparableMeasuresError,
    Measure,
    MeasureError,
    Provenance,
    Subject,
    assert_comparable,
    combined_label_status,
    comparability_problems,
)

#: The old Dojo's COST_BASIS_VALUES (metrics/scorecard_honesty.py at 23f8234c).
OLD_COST_BASIS_VALUES = {"busy_window", "billed_incl_cold", "token_price_estimate"}

finite = st.floats(allow_nan=False, allow_infinity=False, min_value=-1e12, max_value=1e12)
counts = st.integers(min_value=1, max_value=10**9)
verified_statuses = st.sampled_from([status for status in LABEL_STATUSES if status != "unverified"])
cost_bases = st.sampled_from(COST_BASES)
methods = st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True)


@st.composite
def intervals(draw: st.DrawFn) -> ConfidenceInterval:
    low, high = sorted((draw(finite), draw(finite)))
    level = draw(st.floats(min_value=0.01, max_value=0.99))
    return ConfidenceInterval(
        low=low, high=high, level=level, method=draw(st.sampled_from(["wilson", "bootstrap"]))
    )


#: A full provenance stamp, as a run would carry it.
RUN = Provenance(
    dataset_revision="ccd040f6",
    split="test",
    draw_seed="7",
    prompt_id="deploy_v1/sorter@abc123",
    scorer_version="0.1.0",
    serving_kind="api",
    model_id="model-a",
)
words = st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True)


@st.composite
def provenances(draw: st.DrawFn) -> Provenance:
    return Provenance(**{key: draw(words) for key in PROVENANCE_KEYS})


@st.composite
def subjects(draw: st.DrawFn) -> Subject:
    return Subject(
        agent=draw(st.none() | words), doc_class=draw(st.none() | words), field=draw(st.none() | words)
    )


@st.composite
def valid_measures(draw: st.DrawFn) -> Measure:
    return Measure(
        value=draw(finite | st.integers(min_value=-(10**6), max_value=10**6)),
        n=draw(counts),
        cost_basis=draw(cost_bases),
        label_status=draw(verified_statuses),
        method=draw(methods),
        ci=draw(st.none() | intervals()),
        subject=draw(subjects()),
        provenance=draw(provenances()),
    )


def _measure(**overrides: object) -> Measure:
    fields: dict[str, object] = {
        "value": 0.5,
        "n": 10,
        "cost_basis": "none",
        "label_status": "gold",
        "method": "accuracy",
        "provenance": RUN,
    }
    fields.update(overrides)
    return Measure(**fields)  # type: ignore[arg-type]


@given(valid_measures())
def test_a_measure_that_keeps_every_rule_builds_and_round_trips(measure: Measure) -> None:
    out = measure.to_dict()
    assert set(out) == {
        "value",
        "n",
        "ci",
        "cost_basis",
        "label_status",
        "method",
        "reason",
        "subject",
        "provenance",
    }
    assert out["value"] == measure.value and out["n"] == measure.n
    assert (out["ci"] is None) == (measure.ci is None)
    assert out["subject"] == measure.subject.to_dict()
    assert out["provenance"] == (None if measure.provenance is None else measure.provenance.to_dict())
    rebuilt = {field.name: getattr(measure, field.name) for field in dataclasses.fields(measure)}
    assert Measure(**rebuilt) == measure


@given(finite)
def test_n_zero_has_no_value(value: float) -> None:
    with pytest.raises(MeasureError, match="n == 0 has no value"):
        _measure(value=value, n=0)


def test_n_zero_without_a_value_is_a_reported_gap() -> None:
    measure = Measure.missing(method="accuracy", reason="no_rows")
    assert measure.value is None and measure.n == 0 and measure.reason == "no_rows"


def test_a_missing_measure_fails_closed_by_default() -> None:
    assert Measure.missing(method="accuracy", reason="gt_missing").label_status == "unverified"
    assert (
        Measure.missing(method="ttft_seconds", reason="no_requests", label_status="none").label_status
        == "none"
    )


@pytest.mark.parametrize("n", [numpy.int64(5), numpy.int32(5), numpy.uint8(5)])
def test_n_accepts_any_integral_type_and_stores_an_int(n: object) -> None:
    measure = _measure(n=n)
    assert measure.n == 5 and type(measure.n) is int
    assert measure.to_dict()["n"] == 5 and type(measure.to_dict()["n"]) is int


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, False, "0.5", [0.5]])
def test_a_value_is_a_finite_real_number(value: object) -> None:
    with pytest.raises(MeasureError, match="value must be a finite number"):
        _measure(value=value)


@pytest.mark.parametrize(
    "value",
    [numpy.int64(1), numpy.float32(0.5), numpy.float64(0.25), numpy.uint8(1), fractions.Fraction(1, 4), 1],
    ids=["numpy int64", "numpy float32", "numpy float64", "numpy uint8", "fraction", "int"],
)
def test_a_value_of_any_real_type_is_stored_as_a_float(value: object) -> None:
    measure = _measure(value=value)
    assert type(measure.value) is float and measure.value == float(value)  # type: ignore[arg-type]
    interval = ConfidenceInterval(low=value, high=value, level=0.95, method="wilson")  # type: ignore[arg-type]
    assert type(interval.low) is float and type(interval.high) is float


@pytest.mark.parametrize(
    "value",
    [numpy.float32("nan"), numpy.float64("inf"), numpy.bool_(True), 10**400, decimal.Decimal("0.5")],
    ids=["numpy nan", "numpy inf", "numpy bool", "int beyond float", "decimal"],
)
def test_a_value_of_a_real_type_is_still_finite_and_not_a_bool(value: object) -> None:
    with pytest.raises(MeasureError, match="value must be a finite number"):
        _measure(value=value)
    with pytest.raises(MeasureError, match="ci.low must be a finite number"):
        ConfidenceInterval(low=value, high=1.0, level=0.95, method="wilson")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "n", [-1, 1.0, True, None, "3", numpy.float64(3.0), numpy.bool_(True), numpy.int64(-1)]
)
def test_n_is_a_non_negative_int(n: object) -> None:
    with pytest.raises(MeasureError, match="n must be a non-negative int"):
        _measure(n=n)


@given(finite, counts)
def test_unverified_labels_never_carry_a_value(value: float, n: int) -> None:
    with pytest.raises(MeasureError, match="unverified labels give no value"):
        _measure(value=value, n=n, label_status="unverified")
    kept = _measure(value=None, n=n, label_status="unverified", reason="gt_stale_hash")
    assert kept.value is None


@given(counts, verified_statuses)
def test_a_missing_value_needs_a_reason(n: int, status: str) -> None:
    with pytest.raises(MeasureError, match="a missing value needs a reason"):
        _measure(value=None, n=n, label_status=status)


@pytest.mark.parametrize("reason", ["", "   ", 3])
def test_a_reason_is_a_non_empty_string(reason: object) -> None:
    with pytest.raises(MeasureError, match="reason must be a non-empty string"):
        _measure(value=None, reason=reason)


@given(st.text(max_size=12))
def test_cost_basis_is_closed(word: str) -> None:
    assume(word not in COST_BASES)
    with pytest.raises(MeasureError, match="cost_basis"):
        _measure(cost_basis=word)


@given(st.text(max_size=12))
def test_label_status_is_closed(word: str) -> None:
    assume(word not in LABEL_STATUSES)
    with pytest.raises(MeasureError, match="label_status"):
        _measure(label_status=word)


@pytest.mark.parametrize("method", ["", "  ", None, 7])
def test_method_names_the_metric(method: object) -> None:
    with pytest.raises(MeasureError, match="method must name"):
        _measure(method=method)


def test_cost_bases_keep_the_old_vocabulary_plus_none() -> None:
    assert set(COST_BASES) == OLD_COST_BASIS_VALUES | {"none"}


def test_label_status_words_are_the_labels_package_status_words() -> None:
    labels_models = pytest.importorskip("mailroom_labels.models")
    assert labels_models.STATUS_WORDS == LABEL_STATUS_WORDS
    assert set(LABEL_STATUSES) == {*LABEL_STATUS_WORDS, "mixed", "none", "unverified"}


@given(finite, finite)
def test_an_interval_has_ordered_bounds(first: float, second: float) -> None:
    assume(first != second)
    low, high = sorted((first, second))
    with pytest.raises(MeasureError, match="is above ci.high"):
        ConfidenceInterval(low=high, high=low, level=0.95, method="wilson")


@pytest.mark.parametrize("level", [0.0, 1.0, -0.5, 1.5, math.nan, True])
def test_an_interval_level_lies_strictly_between_zero_and_one(level: object) -> None:
    with pytest.raises(MeasureError, match="ci.level"):
        ConfidenceInterval(low=0.1, high=0.2, level=level, method="wilson")  # type: ignore[arg-type]


@pytest.mark.parametrize("bound", [math.nan, math.inf, None, "0.1"])
def test_interval_bounds_are_finite(bound: object) -> None:
    with pytest.raises(MeasureError, match="ci.low must be a finite number"):
        ConfidenceInterval(low=bound, high=0.2, level=0.95, method="wilson")  # type: ignore[arg-type]


def test_an_interval_names_its_method() -> None:
    with pytest.raises(MeasureError, match="ci.method"):
        ConfidenceInterval(low=0.1, high=0.2, level=0.95, method=" ")


@given(intervals())
def test_an_interval_needs_a_value(interval: ConfidenceInterval) -> None:
    with pytest.raises(MeasureError, match="a confidence interval needs a value"):
        _measure(value=None, reason="no_rows", ci=interval)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"subject": ("sorter", None, None)}, "subject must be a Subject"),
        ({"provenance": {"split": "test"}}, "provenance must be a Provenance"),
    ],
)
def test_subject_and_provenance_are_typed(fields: dict[str, object], expected: str) -> None:
    with pytest.raises(MeasureError, match=expected):
        _measure(**fields)


@pytest.mark.parametrize("value", ["", "  ", 3])
def test_subject_and_provenance_words_are_non_empty_text(value: object) -> None:
    with pytest.raises(MeasureError, match="subject.field must be None or a non-empty string"):
        Subject(field=value)  # type: ignore[arg-type]
    with pytest.raises(MeasureError, match="provenance.split must be None or a non-empty string"):
        Provenance(split=value)  # type: ignore[arg-type]


def test_ci_must_be_an_interval() -> None:
    with pytest.raises(MeasureError, match="ci must be a ConfidenceInterval"):
        _measure(ci=(0.1, 0.2))


def test_a_measure_is_immutable() -> None:
    measure = _measure()
    with pytest.raises(dataclasses.FrozenInstanceError):
        measure.value = 0.9  # type: ignore[misc]
    assert hash(measure) == hash(_measure())


@given(st.lists(st.sampled_from(LABEL_STATUSES), min_size=1, max_size=8))
def test_combined_label_status(statuses: list[str]) -> None:
    combined = combined_label_status(statuses)
    assert combined == combined_label_status(reversed(statuses))
    if len(set(statuses)) == 1:
        assert combined == statuses[0]
    elif "unverified" in statuses:
        assert combined == "unverified"
    else:
        assert combined == "mixed"


def test_combined_label_status_refuses_nothing_and_unknown_words() -> None:
    with pytest.raises(MeasureError, match="at least one"):
        combined_label_status([])
    with pytest.raises(MeasureError, match="unknown label status 'platinum'"):
        combined_label_status(["gold", "platinum"])


@given(valid_measures())
def test_a_measure_is_comparable_with_itself(measure: Measure) -> None:
    assert comparability_problems(measure, measure) == ()
    assert_comparable(measure, measure)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"method": "f1_macro"}, "method_mismatch"),
        ({"cost_basis": "token_price_estimate"}, "cost_basis_mismatch"),
        ({"label_status": "near_gold"}, "label_status_mismatch"),
        ({"value": None, "reason": "gt_missing"}, "right_unmeasured (gt_missing)"),
        ({"subject": Subject(field="effective_date")}, "subject_mismatch"),
        ({"subject": Subject(doc_class="contract")}, "subject_mismatch"),
        ({"subject": Subject(agent="judge")}, "subject_mismatch"),
        (
            {"provenance": dataclasses.replace(RUN, dataset_revision="0f00ba11")},
            "provenance_mismatch (dataset_revision)",
        ),
        (
            {"provenance": dataclasses.replace(RUN, prompt_id="deploy_v1/sorter@def456")},
            "provenance_mismatch (prompt_id)",
        ),
        (
            {"provenance": dataclasses.replace(RUN, scorer_version="0.2.0")},
            "provenance_mismatch (scorer_version)",
        ),
        ({"provenance": dataclasses.replace(RUN, model_id=None)}, "provenance_missing (model_id)"),
        ({"provenance": dataclasses.replace(RUN, draw_seed=None)}, "provenance_missing (draw_seed)"),
        ({"provenance": None}, "right_provenance_missing"),
    ],
)
def test_measures_that_differ_in_identity_are_not_compared(change: dict[str, object], problem: str) -> None:
    left = _measure()
    right = _measure(**change)
    problems = comparability_problems(left, right)
    assert len(problems) == 1 and problems[0].startswith(problem)
    with pytest.raises(IncomparableMeasuresError) as caught:
        assert_comparable(left, right)
    assert caught.value.problems == problems


def test_two_fields_of_one_class_are_never_compared() -> None:
    left = _measure(method="per_field_scores", subject=Subject(doc_class="contract", field="effective_date"))
    right = _measure(method="per_field_scores", subject=Subject(doc_class="contract", field="governing_law"))
    with pytest.raises(IncomparableMeasuresError, match="subject_mismatch"):
        assert_comparable(left, right)
    assert_comparable(left, dataclasses.replace(left, value=0.9))


def test_two_dataset_revisions_are_never_compared() -> None:
    left = _measure()
    right = _measure(provenance=dataclasses.replace(RUN, dataset_revision="0f00ba11"), value=0.7)
    assert comparability_problems(left, right) == ("provenance_mismatch (dataset_revision)",)


def test_both_sides_without_a_draw_seed_compare() -> None:
    full_split = dataclasses.replace(RUN, draw_seed=None)
    assert comparability_problems(_measure(provenance=full_split), _measure(provenance=full_split)) == ()


def test_vary_lets_the_compared_axis_differ_but_not_go_missing() -> None:
    local = _measure(provenance=dataclasses.replace(RUN, serving_kind="local", model_id="model-b"))
    api = _measure()
    assert comparability_problems(local, api) == ("provenance_mismatch (serving_kind, model_id)",)
    assert comparability_problems(local, api, vary=("serving_kind", "model_id")) == ()
    assert comparability_problems(local, api, vary="serving_kind") == ("provenance_mismatch (model_id)",)
    unset = _measure(provenance=dataclasses.replace(RUN, serving_kind=None))
    assert comparability_problems(unset, api, vary="serving_kind") == ("provenance_missing (serving_kind)",)
    assert_comparable(local, api, vary=("serving_kind", "model_id"))


@pytest.mark.parametrize("vary", [("metric_id",), ("cost_basis",), "split_name"])
def test_vary_names_only_provenance_keys(vary: object) -> None:
    with pytest.raises(MeasureError, match="not provenance keys"):
        comparability_problems(_measure(), _measure(), vary=vary)  # type: ignore[arg-type]


def test_provenance_keys_are_the_old_keys_less_metric_id() -> None:
    old = {
        "dataset_revision",
        "split",
        "draw_seed",
        "prompt_id",
        "metric_id",
        "scorer_version",
        "serving_kind",
        "model_id",
    }
    assert set(PROVENANCE_KEYS) == old - {"metric_id"}
    assert tuple(field.name for field in dataclasses.fields(Provenance)) == PROVENANCE_KEYS
