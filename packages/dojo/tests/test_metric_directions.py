"""Every metric's direction, frozen in a reviewed fixture and re-derived from one rule.

The old registry stated no direction, so the table's are derived (owner to confirm).
``fixtures/measures/metric_directions.json`` freezes all of them; a direction changes only
with that file in the same commit. The rule, in order:

1. an explicit override in the fixture (with its reason);
2. unit ``error_rate``, or a lower-is-better word in the id (:data:`LOWER_WORDS`): lower;
3. a higher-is-better word in the id (:data:`HIGHER_WORDS`): higher;
4. anything else: none.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import pytest

from dojo_testkit import PACKAGE_ROOT
from mailroom_dojo.measures import MetricRow, MetricTable, load_metric_table

FIXTURE_PATH = PACKAGE_ROOT / "tests" / "fixtures" / "measures" / "metric_directions.json"
FROZEN: Mapping[str, Any] = MappingProxyType(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))

LOWER_WORDS = (
    "error",
    "wrong",
    "false_",
    "violation",
    "wasted",
    "upper",
    "ece",
    "brier",
    "latency",
    "seconds",
    "cost",
    "usd",
    "_p50",
    "_p95",
)
HIGHER_WORDS = (
    "accuracy",
    "f1",
    "f2",
    "precision",
    "recall",
    "completeness",
    "similarity",
    "coverage",
    "per_second",
)


def _lower_by_name(row: MetricRow) -> bool:
    return row.unit == "error_rate" or any(word in row.id for word in LOWER_WORDS)


def name_rule(row: MetricRow) -> str:
    """Rules 2 to 4: the direction the id and unit alone give."""
    if _lower_by_name(row):
        return "lower"
    if any(word in row.id for word in HIGHER_WORDS):
        return "higher"
    return "none"


@pytest.fixture(scope="module")
def table() -> MetricTable:
    return load_metric_table()


def test_the_fixture_is_marked_derived() -> None:
    assert FROZEN["status"] == "derived, owner to confirm"


def test_every_direction_is_the_frozen_one(table: MetricTable) -> None:
    assert list(FROZEN["directions"]) == list(table.ids)
    changed = {row.id: (FROZEN["directions"][row.id], row.direction) for row in table.rows}
    assert {key: pair for key, pair in changed.items() if pair[0] != pair[1]} == {}


def test_the_frozen_directions_follow_the_rule(table: MetricTable) -> None:
    overrides = FROZEN["overrides"]
    assert set(overrides) <= set(table.ids), "an override names a row that is not in the table"
    for metric, override in overrides.items():
        assert override["direction"] in ("higher", "lower", "none") and override["why"].strip(), metric
        assert override["direction"] != name_rule(table.get(metric)) or override["direction"] == "none", (
            f"{metric}: an override that repeats the rule is not an override"
        )
    for row in table.rows:
        expected = overrides[row.id]["direction"] if row.id in overrides else name_rule(row)
        assert FROZEN["directions"][row.id] == expected, row.id


def test_errors_wrong_answers_bounds_costs_and_latencies_are_lower(table: MetricTable) -> None:
    for row in table.rows:
        if _lower_by_name(row) or row.cost or row.unit in ("USD", "usd", "seconds", "milliseconds"):
            assert row.direction == "lower", row.id
        if "error" in row.id or "wrong" in row.id or row.id.endswith("_upper_95"):
            assert row.direction == "lower", row.id


def test_a_keep_rule_metric_has_a_direction(table: MetricTable) -> None:
    rows = table.rows_for_reader("keep_rule")
    assert rows
    for row in rows:
        assert row.direction != "none", row.id
