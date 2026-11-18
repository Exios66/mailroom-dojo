"""Measures: the ``Measure`` value every Dojo layer returns, and the flat metric table.

``Measure`` (``measure.py``) carries a value with its sample size, interval, cost basis,
label status and method, and refuses the combinations the old scorecard-honesty module
refused. The metric table (``metrics.yaml``, loaded by ``table.py``) has one row per
metric: the old registry's ids and meanings, a headline flag, the layer that produces
each metric, the readers that use it and its status.
"""

from .measure import (
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
from .table import (
    PAIRING_METHODS,
    READERS,
    DefaultView,
    MetricRow,
    MetricTable,
    MetricTableError,
    UnknownMetricError,
    load_metric_table,
    metric_table_canonical_form,
)

__all__ = [
    "COST_BASES",
    "LABEL_STATUSES",
    "LABEL_STATUS_WORDS",
    "PAIRING_METHODS",
    "PROVENANCE_KEYS",
    "READERS",
    "ConfidenceInterval",
    "DefaultView",
    "IncomparableMeasuresError",
    "Measure",
    "MeasureError",
    "MetricRow",
    "MetricTable",
    "MetricTableError",
    "Provenance",
    "Subject",
    "UnknownMetricError",
    "assert_comparable",
    "combined_label_status",
    "comparability_problems",
    "load_metric_table",
    "metric_table_canonical_form",
]
