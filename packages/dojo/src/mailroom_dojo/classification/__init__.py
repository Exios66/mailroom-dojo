"""Classification scoring: reading classifier outputs as class keys, and the metrics over them.

Build a :class:`ClassLabelSet` once from a scoring profile and pass it to every
function that reads labels::

    label_set = ClassLabelSet.from_profile(profile)
    accuracy(expected, predicted, label_set)
"""

from .labels import (
    ERROR_PREFIX,
    LABEL_JSON_KEY,
    ClassLabelSet,
    class_label_pattern,
    exact_match,
    failure,
    normalize_label,
)
from .metrics import (
    accuracy,
    binary_metrics,
    class_distribution,
    confusion_accuracy,
    confusion_matrix,
    fbeta,
    macro_accuracy,
    macro_prf,
    per_class_stats,
    top_confusions,
)
from .paired import require_equal_lengths

__all__ = [
    "ERROR_PREFIX",
    "LABEL_JSON_KEY",
    "ClassLabelSet",
    "accuracy",
    "binary_metrics",
    "class_distribution",
    "class_label_pattern",
    "confusion_accuracy",
    "confusion_matrix",
    "exact_match",
    "failure",
    "fbeta",
    "macro_accuracy",
    "macro_prf",
    "normalize_label",
    "per_class_stats",
    "require_equal_lengths",
    "top_confusions",
]
