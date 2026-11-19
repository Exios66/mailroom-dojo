"""Invariants of the classification metrics, checked on random label lists (hypothesis).

Each property states a relation the metrics must keep for any input: identities
between metrics, bounds, counts that sum, and invariance under reordering the
expected/predicted pairs.
"""

from __future__ import annotations

import random
from collections import Counter

from hypothesis import given
from hypothesis import strategies as st

from dojo_testkit import MAILROOM_PROFILE_PATH
from mailroom_dojo.classification import (
    ClassLabelSet,
    accuracy,
    binary_metrics,
    class_distribution,
    confusion_accuracy,
    confusion_matrix,
    fbeta,
    macro_accuracy,
    macro_prf,
    normalize_label,
    per_class_stats,
    top_confusions,
)
from mailroom_dojo.profile import load_scoring_profile

LABELS = ClassLabelSet.from_profile(load_scoring_profile(MAILROOM_PROFILE_PATH))

#: Sorter-like text: class names in many spellings, foreign labels, error rows and noise.
LABEL_TEXT = st.one_of(
    st.sampled_from(
        [
            "contract",
            "Contract",
            "merger agreement",
            "MERGER_AGREEMENT",
            "corporate record",
            "correspondence",
            "insurance claim",
            '{"doc_type": "Contract"}',
            "license",
            "other",
            "",
            " ",
            "ERROR: timeout",
            "court opinion",
            "a",
            "b",
            "correspondence about an insurance claim",
            "**contract**",
        ]
    ),
    st.text(max_size=30),
)


@st.composite
def paired_text(draw, minimum: int = 0):
    """Equal-length expected and predicted lists of label text."""
    size = draw(st.integers(minimum, 12))
    return draw(st.lists(LABEL_TEXT, min_size=size, max_size=size)), draw(
        st.lists(LABEL_TEXT, min_size=size, max_size=size)
    )


@st.composite
def paired_text_and_permutation(draw):
    """Paired lists plus a shuffled copy of the same pairs."""
    expected, predicted = draw(paired_text())
    order = draw(st.permutations(range(len(expected))))
    return expected, predicted, [expected[index] for index in order], [predicted[index] for index in order]


def _non_error_count(predicted: list[str]) -> int:
    """Rows whose prediction is not a failed row."""
    return sum(1 for value in predicted if not str(value).startswith("ERROR: "))


@given(paired_text())
def test_accuracy_is_trace_over_total_of_the_confusion_matrix(pairs):
    """Micro accuracy equals the confusion matrix's trace over its total, for any rows."""
    expected, predicted = pairs
    matrix, labels = confusion_matrix(expected, predicted, LABELS)
    assert accuracy(expected, predicted, LABELS) == confusion_accuracy(matrix)
    assert sum(sum(row) for row in matrix) == _non_error_count(predicted)
    assert len(matrix) == len(labels) == len(set(labels))
    assert all(len(row) == len(labels) for row in matrix)


@given(paired_text())
def test_per_class_counts_agree_with_the_confusion_matrix(pairs):
    """Per-class n is the row total and correct the diagonal cell (text predictions), and they sum to the rows
    kept."""
    expected, predicted = pairs
    stats = per_class_stats(expected, predicted, LABELS)
    matrix, labels = confusion_matrix(expected, predicted, LABELS)
    for class_key, entry in stats.items():
        row = labels.index(class_key)
        assert entry["n"] == sum(matrix[row])
        assert entry["correct"] == matrix[row][row]
        assert 0 <= entry["correct"] <= entry["n"]
        for name in ("accuracy", "precision", "recall", "f1", "f2"):
            assert 0.0 <= entry[name] <= 1.0
    assert sum(entry["n"] for entry in stats.values()) == _non_error_count(predicted)
    assert sum(entry["correct"] for entry in stats.values()) == sum(matrix[i][i] for i in range(len(matrix)))
    assert list(stats) == sorted(stats)


@given(paired_text())
def test_macro_values_are_bounded_means(pairs):
    """Macro accuracy and macro P/R/F1/F2 lie in [0, 1] and between the smallest and largest per-class
    value."""
    expected, predicted = pairs
    stats = per_class_stats(expected, predicted, LABELS)
    macro = macro_accuracy(expected, predicted, LABELS)
    assert 0.0 <= macro <= 1.0
    if stats:
        values = [entry["accuracy"] for entry in stats.values()]
        assert min(values) - 1e-4 <= macro <= max(values) + 1e-4
    result = macro_prf(expected, predicted, LABELS)
    for name in ("precision_macro", "recall_macro", "f1_macro", "f2_macro"):
        assert 0.0 <= result[name] <= 1.0
    assert result["precision"] == result["precision_macro"] and result["f2"] == result["f2_macro"]
    assert result["n"] == _non_error_count(predicted)
    assert result["n_classes"] == len(result["per_class"]) <= len(stats)


@given(paired_text(), st.sampled_from(["contract", "merger_agreement", "license", ""]))
def test_binary_counts_cover_every_row(pairs, positive):
    """tp + tn + fp + fn is every row (failed rows included), and every rate is in [0, 1]."""
    expected, predicted = pairs
    result = binary_metrics(expected, predicted, positive, label_set=LABELS)
    assert result["tp"] + result["tn"] + result["fp"] + result["fn"] == len(expected)
    for name in ("precision", "recall", "f1", "f2", "accuracy", "false_positive_rate", "false_negative_rate"):
        assert 0.0 <= result[name] <= 1.0
    assert result["tp"] + result["fn"] == sum(
        1 for value in expected if normalize_label(value, LABELS) == positive
    )


@given(paired_text_and_permutation())
def test_metrics_do_not_depend_on_row_order(case):
    """Shuffling the pairs changes no metric (dicts compare without order)."""
    expected, predicted, shuffled_expected, shuffled_predicted = case
    assert accuracy(expected, predicted, LABELS) == accuracy(shuffled_expected, shuffled_predicted, LABELS)
    assert per_class_stats(expected, predicted, LABELS) == per_class_stats(
        shuffled_expected, shuffled_predicted, LABELS
    )
    assert macro_accuracy(expected, predicted, LABELS) == macro_accuracy(
        shuffled_expected, shuffled_predicted, LABELS
    )
    assert macro_prf(expected, predicted, LABELS) == macro_prf(shuffled_expected, shuffled_predicted, LABELS)
    assert macro_prf(expected, predicted, normalize=False) == macro_prf(
        shuffled_expected, shuffled_predicted, normalize=False
    )
    assert binary_metrics(expected, predicted, "contract", label_set=LABELS) == binary_metrics(
        shuffled_expected, shuffled_predicted, "contract", label_set=LABELS
    )
    matrix, labels = confusion_matrix(expected, predicted, LABELS)
    assert (matrix, labels) == confusion_matrix(shuffled_expected, shuffled_predicted, LABELS)
    assert top_confusions(matrix, labels) == top_confusions(
        *confusion_matrix(shuffled_expected, shuffled_predicted, LABELS)
    )
    assert Counter(class_distribution(expected, LABELS)) == Counter(
        class_distribution(shuffled_expected, LABELS)
    )


@given(paired_text(minimum=1))
def test_perfect_predictions_score_one(pairs):
    """Predicting the expected labels themselves scores 1.0 on every non-empty, non-failed set."""
    expected, _ = pairs
    expected = [value for value in expected if not value.startswith("ERROR: ")] or ["contract"]
    assert accuracy(expected, expected, LABELS) == 1.0
    assert macro_accuracy(expected, expected, LABELS) == 1.0
    result = macro_prf(expected, expected, LABELS)
    if result["n_classes"]:
        assert result["f1_macro"] == result["precision_macro"] == result["recall_macro"] == 1.0


@given(st.lists(LABEL_TEXT, max_size=20))
def test_class_distribution_counts_every_value(values):
    """The distribution has one count per value, keyed by the normalized label."""
    distribution = class_distribution(values, LABELS)
    assert sum(distribution.values()) == len(values)
    assert set(distribution) == {normalize_label(value, LABELS) for value in values}


@given(st.one_of(LABEL_TEXT, st.none(), st.integers(), st.floats(), st.booleans()))
def test_a_recognised_class_is_a_fixed_point(value):
    """normalize_label always returns text, and a class key it returns reads as itself."""
    result = normalize_label(value, LABELS)
    assert isinstance(result, str)
    if result in LABELS.class_keys:
        assert normalize_label(result, LABELS) == result


@given(st.floats(0.0, 1.0), st.floats(0.0, 1.0), st.sampled_from([0.5, 1.0, 2.0]))
def test_fbeta_is_bounded_and_f1_is_symmetric(precision, recall, beta):
    """F-beta of rates in [0, 1] is in [0, 1], F1 is symmetric, and F-beta lies between P and R."""
    value = fbeta(precision, recall, beta=beta)
    assert 0.0 <= value <= 1.0
    assert fbeta(precision, recall) == fbeta(recall, precision)
    assert min(precision, recall) - 1e-4 <= value <= max(precision, recall) + 1e-4


@given(st.integers(0, 6), st.randoms(use_true_random=False))
def test_top_confusions_are_the_largest_off_diagonal_cells(side, rng: random.Random):
    """top_confusions returns off-diagonal positive cells in non-increasing count order, at most k of them."""
    matrix = [[rng.randint(0, 4) for _ in range(side)] for _ in range(side)]
    labels = [f"class_{index}" for index in range(side)]
    off_diagonal = sorted(
        (matrix[i][j] for i in range(side) for j in range(side) if i != j and matrix[i][j]), reverse=True
    )
    for k in (0, 1, 3, 100):
        cells = top_confusions(matrix, labels, k=k)
        assert [cell["count"] for cell in cells] == off_diagonal[:k]
        assert all(cell["expected"] != cell["predicted"] for cell in cells)
