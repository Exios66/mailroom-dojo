"""Every metric's direction, frozen in a reviewed fixture and re-derived by the table's rule.

The old registry stated no direction, so the table's are derived (owner to confirm) by
:func:`mailroom_dojo.measures.derive_direction`: the table's ``direction_overrides``
first, then the id and unit. ``fixtures/measures/metric_directions.json`` freezes every
direction and every override; a direction changes only with that file in the same
commit (regenerate it from the table and review the diff).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import pytest

from dojo_testkit import PACKAGE_ROOT
from mailroom_dojo.measures import MetricTable, derive_direction, load_metric_table
from mailroom_dojo.measures.table import DIRECTION_LOWER_WORDS, name_direction

FIXTURE_PATH = PACKAGE_ROOT / "tests" / "fixtures" / "measures" / "metric_directions.json"
FROZEN: Mapping[str, Any] = MappingProxyType(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def table() -> MetricTable:
    return load_metric_table()


def test_the_fixture_is_marked_derived() -> None:
    assert FROZEN["status"] == "derived, owner to confirm"


def test_every_direction_is_the_frozen_one(table: MetricTable) -> None:
    assert list(FROZEN["directions"]) == list(table.ids)
    changed = {row.id: (FROZEN["directions"][row.id], row.direction) for row in table.rows}
    assert {key: pair for key, pair in changed.items() if pair[0] != pair[1]} == {}


def test_the_frozen_overrides_are_the_table_overrides(table: MetricTable) -> None:
    table_overrides = {
        metric: {"direction": override.direction, "why": override.why}
        for metric, override in table.direction_overrides.items()
    }
    assert table_overrides == FROZEN["overrides"]


def test_the_frozen_directions_follow_the_rule(table: MetricTable) -> None:
    for row in table.rows:
        assert FROZEN["directions"][row.id] == derive_direction(row.id, row.unit, table.direction_overrides)


@pytest.mark.parametrize(
    ("metric", "unit", "expected"),
    [
        ("wer", "error_rate", "lower"),
        ("judge_error_rate", "float[0,1]", "lower"),
        ("reviewer_wrong_accept_rate", "float[0,1]", "lower"),
        ("reviewer_wrong_accept_upper_95", "float[0,1]", "lower"),
        ("false_positive_recall", "float[0,1]", "lower"),
        ("e2e_p95", "seconds", "lower"),
        ("reviewer_cost_per_accepted_usd", "usd", "lower"),
        ("f1_macro", "float[0,1]", "higher"),
        ("tokens_per_second", "tokens/s", "higher"),
        ("intake_prep_completeness", "float[0,1]", "higher"),
        ("audit_resolution_rate", "float[0,1]", "none"),
        ("prompt_tokens", "count", "none"),
    ],
)
def test_the_name_rule(metric: str, unit: str, expected: str) -> None:
    assert name_direction(metric, unit) == expected
    assert derive_direction(metric, unit, {}) == expected


def test_errors_wrong_answers_bounds_costs_and_latencies_are_lower(table: MetricTable) -> None:
    for row in table.rows:
        if (
            row.unit == "error_rate"
            or any(word in row.id for word in DIRECTION_LOWER_WORDS)
            or row.cost
            or row.unit in ("USD", "usd", "seconds", "milliseconds")
        ):
            assert row.direction == "lower", row.id
        if "error" in row.id or "wrong" in row.id or row.id.endswith("_upper_95"):
            assert row.direction == "lower", row.id


def test_a_keep_rule_metric_has_a_direction(table: MetricTable) -> None:
    rows = table.rows_for_reader("keep_rule")
    assert rows
    for row in rows:
        assert row.direction != "none", row.id
