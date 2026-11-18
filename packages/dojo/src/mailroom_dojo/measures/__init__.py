"""Measures: the ``Measure`` value every Dojo layer returns.

``Measure`` (``measure.py``) carries a value with its sample size, interval, cost basis,
label status and method, and refuses the combinations the old scorecard-honesty module
refused.
"""

from .measure import (
    COST_BASES,
    LABEL_STATUS_WORDS,
    LABEL_STATUSES,
    ConfidenceInterval,
    IncomparableMeasuresError,
    Measure,
    MeasureError,
    assert_comparable,
    combined_label_status,
    comparability_problems,
)

__all__ = [
    "COST_BASES",
    "LABEL_STATUSES",
    "LABEL_STATUS_WORDS",
    "ConfidenceInterval",
    "IncomparableMeasuresError",
    "Measure",
    "MeasureError",
    "assert_comparable",
    "combined_label_status",
    "comparability_problems",
]
