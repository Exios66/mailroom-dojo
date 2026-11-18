"""Every metric's direction, frozen in a reviewed fixture, and a lint over the ids' words.

The ``direction`` column of ``measures/metrics.yaml`` is the source of truth: nothing derives
it. The old registry stated no direction, so every value is proposed for the owner to
confirm, and ``fixtures/measures/metric_directions.json`` freezes them all: a direction
changes only with that file in the same commit.

The lint compares each direction with what the id's words suggest (whole ``_`` or ``-``
separated words, never substrings: ``ece`` is not in ``unnecessary``). It decides nothing; it
lists the disagreements, and the list must equal the fixture's reviewed
``name_rule_exceptions``, so a new row whose direction disagrees with its name, or a stale
exception, shows up for review.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import pytest

from dojo_testkit import PACKAGE_ROOT
from mailroom_dojo.measures import MetricTable, load_metric_table

FIXTURE_PATH = PACKAGE_ROOT / "tests" / "fixtures" / "measures" / "metric_directions.json"
FROZEN: Mapping[str, Any] = MappingProxyType(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))

#: Words that suggest lower is better: errors, wrong answers, bounds, costs, latencies.
LOWER_WORDS = frozenset(
    {
        "error",
        "errors",
        "wrong",
        "wrongful",
        "false",
        "violation",
        "violations",
        "wasted",
        "upper",
        "ece",
        "brier",
        "latency",
        "seconds",
        "cost",
        "usd",
        "p50",
        "p95",
    }
)
#: Words that suggest higher is better: shares and scores of right answers.
HIGHER_WORDS = frozenset(
    {"accuracy", "f1", "f2", "precision", "recall", "completeness", "similarity", "coverage"}
)


def words(metric: str) -> tuple[str, ...]:
    """The whole words of a metric id."""
    return tuple(re.split(r"[_-]", metric))


def name_rule(metric: str, unit: str) -> str:
    """What the id and unit suggest: lower, higher or none (a lint, not a rule of the table)."""
    tokens = words(metric)
    if unit == "error_rate" or LOWER_WORDS.intersection(tokens):
        return "lower"
    if HIGHER_WORDS.intersection(tokens) or ("per", "second") in zip(tokens, tokens[1:], strict=False):
        return "higher"
    return "none"


def name_rule_disagreements(table: MetricTable) -> dict[str, tuple[str, str]]:
    """Each row whose direction differs from the name rule: id -> (direction, name rule)."""
    out: dict[str, tuple[str, str]] = {}
    for row in table.rows:
        suggested = name_rule(row.id, row.unit)
        if suggested != row.direction:
            out[row.id] = (row.direction, suggested)
    return out


@pytest.fixture(scope="module")
def table() -> MetricTable:
    return load_metric_table()


def test_the_fixture_is_marked_for_review() -> None:
    assert FROZEN["status"] == "proposed, owner to confirm"


def test_every_direction_is_the_frozen_one(table: MetricTable) -> None:
    assert list(FROZEN["directions"]) == list(table.ids)
    changed = {row.id: (FROZEN["directions"][row.id], row.direction) for row in table.rows}
    assert {key: pair for key, pair in changed.items() if pair[0] != pair[1]} == {}


def test_the_name_lint_reports_only_the_reviewed_exceptions(table: MetricTable) -> None:
    exceptions = FROZEN["name_rule_exceptions"]
    found = name_rule_disagreements(table)
    unreviewed = {metric: pair for metric, pair in found.items() if metric not in exceptions}
    stale = sorted(set(exceptions) - set(found))
    assert unreviewed == {}, f"directions that disagree with their names, not yet reviewed: {unreviewed}"
    assert stale == [], f"reviewed exceptions that no longer disagree: {stale}"
    for metric, entry in exceptions.items():
        assert (entry["direction"], entry["name_rule"]) == found[metric], metric
        assert entry["why"].strip(), metric


def test_the_name_lint_never_changes_the_table(table: MetricTable) -> None:
    """The lint is a report: a row that disagrees with its name still loads as written."""
    assert table.get("completeness_label").direction == "none"
    assert name_rule("completeness_label", "tag") == "higher"


@pytest.mark.parametrize(
    ("metric", "unit", "expected"),
    [
        ("wer", "error_rate", "lower"),
        ("judge_error_rate", "float[0,1]", "lower"),
        ("reviewer_wrong_accept_rate", "float[0,1]", "lower"),
        ("arbiter_wrongful_accept_rate", "float[0,1]", "lower"),
        ("reviewer_wrong_accept_upper_95", "float[0,1]", "lower"),
        ("false_positive_recall", "float[0,1]", "lower"),
        ("e2e_p95", "seconds", "lower"),
        ("reviewer_cost_per_accepted_usd", "USD", "lower"),
        ("f1_macro", "float[0,1]", "higher"),
        ("tokens_per_second", "tokens/s", "higher"),
        ("intake_prep_completeness", "float[0,1]", "higher"),
        ("audit_resolution_rate", "float[0,1]", "none"),
        ("prompt_tokens", "count", "none"),
        # whole words only: no lower-is-better word hides inside another word
        ("arbiter_unnecessary_escalation_rate", "float[0,1]", "none"),
        ("precisionless_rate", "float[0,1]", "none"),
        ("costume_count", "count", "none"),
    ],
)
def test_the_name_rule(metric: str, unit: str, expected: str) -> None:
    assert name_rule(metric, unit) == expected


def test_a_keep_rule_metric_has_a_direction(table: MetricTable) -> None:
    rows = table.rows_for_reader("keep_rule")
    assert rows
    for row in rows:
        assert row.direction != "none", row.id
