"""Classification metrics over paired expected and predicted labels.

Every function is deterministic and pure. Labels are read with a
:class:`~mailroom_dojo.classification.labels.ClassLabelSet` (built from the scoring
profile), so two profiles in one process never interfere. A prediction starting with
:data:`~mailroom_dojo.classification.labels.ERROR_PREFIX` is a failed row and is left
out of the metrics that say so. Rates are rounded to four decimals.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from .labels import ERROR_PREFIX, ClassLabelSet
from .paired import require_equal_lengths

__all__ = [
    "accuracy",
    "binary_metrics",
    "class_distribution",
    "confusion_accuracy",
    "confusion_matrix",
    "fbeta",
    "macro_accuracy",
    "macro_prf",
    "per_class_stats",
    "top_confusions",
]

#: Text values a raw binary flag reads as positive (stripped, lowercased).
_POSITIVE_FLAG_TEXTS = ("yes", "y", "true", "1", "1.0")


def accuracy(expected: Sequence[Any], predicted: Sequence[Any], label_set: ClassLabelSet) -> float:
    """Share of non-failed rows whose prediction normalizes to the expected class.

    0.0 when there are no rows or every row failed. Raises ``ValueError`` when the
    lengths differ.
    """
    require_equal_lengths(expected, predicted)
    if not expected:
        return 0.0
    pairs = [
        (item, prediction)
        for item, prediction in zip(expected, predicted, strict=False)
        if not str(prediction).startswith(ERROR_PREFIX)
    ]
    if not pairs:
        return 0.0
    hits = sum(
        1 for item, prediction in pairs if label_set.normalize(prediction) == label_set.normalize(item)
    )
    return round(hits / len(pairs), 4)


def fbeta(precision: float, recall: float, *, beta: float = 1.0) -> float:
    """The van Rijsbergen F-beta of a precision and a recall (``beta=1`` is F1, ``beta=2`` weights recall).

    0.0 when both are zero or less, or when the weighted denominator is zero.
    """
    if precision <= 0.0 and recall <= 0.0:
        return 0.0
    beta_squared = beta * beta
    denominator = beta_squared * precision + recall
    if denominator == 0.0:
        return 0.0
    return round((1.0 + beta_squared) * precision * recall / denominator, 4)


def _one_versus_rest(pairs: list[tuple[str, str]], class_key: str) -> dict[str, Any]:
    """Counts, accuracy and one-vs-rest precision, recall, F1 and F2 of one class over normalized pairs."""
    count = sum(1 for item, _ in pairs if item == class_key)
    correct = sum(1 for item, prediction in pairs if item == class_key and prediction == class_key)
    false_positives = sum(1 for item, prediction in pairs if item != class_key and prediction == class_key)
    false_negatives = sum(1 for item, prediction in pairs if item == class_key and prediction != class_key)
    precision = round(correct / (correct + false_positives), 4) if correct + false_positives else 0.0
    recall = round(correct / (correct + false_negatives), 4) if correct + false_negatives else 0.0
    return {
        "n": count,
        "correct": correct,
        "accuracy": round(correct / count, 4) if count else 0.0,
        "precision": precision,
        "recall": recall,
        "f1": fbeta(precision, recall, beta=1.0),
        "f2": fbeta(precision, recall, beta=2.0),
    }


def per_class_stats(
    expected: Sequence[Any], predicted: Sequence[Any], label_set: ClassLabelSet
) -> dict[str, dict[str, Any]]:
    """Accuracy and one-vs-rest precision, recall, F1 and F2 for each expected class.

    Returns ``{class: {n, correct, accuracy, precision, recall, f1, f2}}`` for every
    class that occurs as an expected label, in sorted order. Failed rows are left out
    entirely. A prediction is read as text before it is normalized, so ``None``
    becomes the label ``"none"``. Raises ``ValueError`` when the lengths differ.
    """
    require_equal_lengths(expected, predicted)
    pairs: list[tuple[str, str]] = []
    for item, prediction in zip(expected, predicted, strict=False):
        output = str(prediction)
        if output.startswith(ERROR_PREFIX):
            continue
        pairs.append((label_set.normalize(item), label_set.normalize(output)))
    return {
        class_key: _one_versus_rest(pairs, class_key) for class_key in sorted({item for item, _ in pairs})
    }


def macro_accuracy(expected: Sequence[Any], predicted: Sequence[Any], label_set: ClassLabelSet) -> float:
    """Unweighted mean of the per-class accuracies (0.0 when no class is expected)."""
    stats = per_class_stats(expected, predicted, label_set)
    if not stats:
        return 0.0
    return round(sum(entry["accuracy"] for entry in stats.values()) / len(stats), 4)


def confusion_matrix(
    expected: Sequence[Any],
    predicted: Sequence[Any],
    label_set: ClassLabelSet,
    labels: Sequence[str] | None = None,
) -> tuple[list[list[int]], list[str]]:
    """Counts of expected (rows) against predicted (columns) normalized labels.

    The label order is ``labels`` followed by every other observed label sorted, or
    the sorted observed labels when ``labels`` is None. Failed rows are dropped.
    Returns ``(matrix, labels)``. Raises ``ValueError`` when the lengths differ.
    """
    require_equal_lengths(expected, predicted)
    pairs = [
        (label_set.normalize(item), label_set.normalize(prediction))
        for item, prediction in zip(expected, predicted, strict=False)
        if not str(prediction).startswith(ERROR_PREFIX)
    ]
    observed = {item for item, _ in pairs} | {prediction for _, prediction in pairs}
    if labels is None:
        order = sorted(observed)
    else:
        order = list(labels)
        order.extend(sorted(observed - set(order)))
    index = {label: position for position, label in enumerate(order)}
    matrix = [[0] * len(order) for _ in order]
    for item, prediction in pairs:
        matrix[index[item]][index[prediction]] += 1
    return matrix, order


def binary_metrics(
    expected: Sequence[Any],
    predicted: Sequence[Any],
    positive: str,
    *,
    label_set: ClassLabelSet | None = None,
    normalize: bool = True,
) -> dict[str, Any]:
    """Confusion counts and rates for a one-vs-rest binary view of the labels.

    With ``normalize=True`` a value is positive when it normalizes to ``positive``
    (``label_set`` is then required). With ``normalize=False`` values are raw flags:
    a string is positive when it is ``yes``, ``y``, ``true``, ``1`` or ``1.0``
    (stripped, case-blind), anything else by its truth value. Failed rows are not
    dropped. Returns ``tp``, ``tn``, ``fp``, ``fn``, ``precision``, ``recall``,
    ``f1``, ``f2``, ``accuracy``, ``false_positive_rate`` and
    ``false_negative_rate``. Raises ``TypeError`` when normalizing without a label
    set and ``ValueError`` when the lengths differ.
    """
    if normalize and label_set is None:
        raise TypeError("binary_metrics with normalize=True needs a label_set")

    def is_positive(value: Any) -> bool:
        """Read one value as positive or negative."""
        if not normalize:
            if isinstance(value, str):
                return value.strip().lower() in _POSITIVE_FLAG_TEXTS
            return bool(value)
        return label_set.normalize(value) == positive

    require_equal_lengths(expected, predicted)
    true_positives = true_negatives = false_positives = false_negatives = 0
    for item, prediction in zip(expected, predicted, strict=False):
        actual = is_positive(item)
        guessed = is_positive(prediction)
        if actual and guessed:
            true_positives += 1
        elif actual and not guessed:
            false_negatives += 1
        elif not actual and guessed:
            false_positives += 1
        else:
            true_negatives += 1
    precision = (
        round(true_positives / (true_positives + false_positives), 4)
        if true_positives + false_positives
        else 0.0
    )
    recall = (
        round(true_positives / (true_positives + false_negatives), 4)
        if true_positives + false_negatives
        else 0.0
    )
    total = true_positives + true_negatives + false_positives + false_negatives
    negatives = false_positives + true_negatives
    positives = false_negatives + true_positives
    return {
        "tp": true_positives,
        "tn": true_negatives,
        "fp": false_positives,
        "fn": false_negatives,
        "precision": precision,
        "recall": recall,
        "f1": fbeta(precision, recall, beta=1.0),
        "f2": fbeta(precision, recall, beta=2.0),
        "accuracy": round((true_positives + true_negatives) / total, 4) if total else 0.0,
        "false_positive_rate": round(false_positives / negatives, 4) if negatives else 0.0,
        "false_negative_rate": round(false_negatives / positives, 4) if positives else 0.0,
    }


def macro_prf(
    expected: Sequence[Any],
    predicted: Sequence[Any],
    label_set: ClassLabelSet | None = None,
    *,
    normalize: bool = True,
) -> dict[str, Any]:
    """Unweighted means of one-vs-rest precision, recall, F1 and F2 over the expected classes.

    Failed rows are left out. With ``normalize=True`` labels are normalized with
    ``label_set`` (required); with ``normalize=False`` they are compared as text.
    An empty expected label is not a class. Returns ``precision_macro``,
    ``recall_macro``, ``f1_macro``, ``f2_macro``, the aliases ``precision``,
    ``recall`` and ``f2``, ``per_class``, ``n`` (rows kept) and ``n_classes``; all
    rates are 0.0 when no class is expected. Raises ``TypeError`` when normalizing
    without a label set and ``ValueError`` when the lengths differ.
    """
    if normalize and label_set is None:
        raise TypeError("macro_prf with normalize=True needs a label_set")
    require_equal_lengths(expected, predicted)
    pairs: list[tuple[str, str]] = []
    for item, prediction in zip(expected, predicted, strict=False):
        if str(prediction).startswith(ERROR_PREFIX):
            continue
        if normalize:
            pairs.append((label_set.normalize(item), label_set.normalize(prediction)))
        else:
            pairs.append((str(item), str(prediction)))
    classes = sorted({item for item, _ in pairs if item})
    empty: dict[str, Any] = {
        "precision_macro": 0.0,
        "recall_macro": 0.0,
        "f1_macro": 0.0,
        "f2_macro": 0.0,
        "precision": 0.0,
        "recall": 0.0,
        "f2": 0.0,
        "per_class": {},
        "n": len(pairs),
        "n_classes": 0,
    }
    if not classes:
        return empty
    if normalize:
        stats = per_class_stats(
            [item for item, _ in pairs], [prediction for _, prediction in pairs], label_set
        )
        per_class = {key: value for key, value in stats.items() if key in set(classes)}
    else:
        per_class = {class_key: _one_versus_rest(pairs, class_key) for class_key in classes}
    class_count = len(per_class)
    if not class_count:
        return empty
    precision_macro = round(sum(entry["precision"] for entry in per_class.values()) / class_count, 4)
    recall_macro = round(sum(entry["recall"] for entry in per_class.values()) / class_count, 4)
    f1_macro = round(sum(entry["f1"] for entry in per_class.values()) / class_count, 4)
    f2_macro = round(sum(entry["f2"] for entry in per_class.values()) / class_count, 4)
    return {
        "precision_macro": precision_macro,
        "recall_macro": recall_macro,
        "f1_macro": f1_macro,
        "f2_macro": f2_macro,
        "precision": precision_macro,
        "recall": recall_macro,
        "f2": f2_macro,
        "per_class": per_class,
        "n": len(pairs),
        "n_classes": class_count,
    }


def confusion_accuracy(matrix: Sequence[Sequence[int]]) -> float:
    """Trace over total of a square confusion matrix (0.0 for an empty or all-zero matrix)."""
    total = sum(sum(row) for row in matrix)
    if not total:
        return 0.0
    trace = sum(matrix[position][position] for position in range(len(matrix)))
    return round(trace / total, 4)


def top_confusions(
    matrix: Sequence[Sequence[int]], labels: Sequence[str], k: int = 5
) -> list[dict[str, Any]]:
    """The ``k`` largest off-diagonal cells as ``{expected, predicted, count}``.

    Cells are read row by row; the sort by count is stable, so equal counts keep
    that order.
    """
    cells = []
    for row_index, row in enumerate(matrix):
        for column_index, count in enumerate(row):
            if row_index != column_index and count > 0:
                cells.append(
                    {"expected": labels[row_index], "predicted": labels[column_index], "count": count}
                )
    cells.sort(key=lambda cell: cell["count"], reverse=True)
    return cells[:k]


def class_distribution(values: Sequence[Any], label_set: ClassLabelSet) -> dict[str, int]:
    """Count of each normalized label, in order of first occurrence."""
    return dict(Counter(label_set.normalize(value) for value in values))
