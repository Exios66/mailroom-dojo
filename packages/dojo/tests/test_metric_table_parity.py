"""Parity of the metric table with the old Dojo's registry, metric ids and golden baseline.

The expected side is ``fixtures/measures/old_metric_registry.json``, captured once from
llm-dojo-scoring at 23f8234c by ``scripts/capture_old_metric_registry.py``; nothing here
reads an old checkout.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import pytest

from dojo_testkit import OLD_DOJO_COMMIT, PACKAGE_ROOT
from mailroom_dojo.measures import MetricRow, MetricTable, load_metric_table

FIXTURE_PATH = PACKAGE_ROOT / "tests" / "fixtures" / "measures" / "old_metric_registry.json"
OLD: Mapping[str, Any] = MappingProxyType(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))

#: Golden names that are another row's value under a second name, and why the two are one.
GOLDEN_ALIASES: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "overall_score": (
            "extraction_overall_score",
            "METRIC_IDS.md maps both names to pipeline.extraction.overall",
        ),
        "doc_type_accuracy": (
            "accuracy",
            "old tasks.score_task (docclass) sets accuracy and doc_type_accuracy to the same doc_acc",
        ),
        "jaccard_mean": (
            "jaccard_similarity",
            "old tasks.contracteval_metrics: the mean of tasks.get_jaccard, "
            "the registry row's mean aggregation",
        ),
    }
)

_COUNT = (
    "a sample size or a confusion count: it becomes a Measure's n (or the counts behind it), not a metric"
)
_INTAKE = (
    "a per-document check inside the intake task (old pipeline/intake.py score_intake) that feeds its "
    "accuracy; the task reports it as detail, not as a metric"
)
#: Golden names that are not metrics, and why.
GOLDEN_EXCEPTIONS: Mapping[str, str] = MappingProxyType(
    {
        **dict.fromkeys(
            (
                "n",
                "n_aligned",
                "n_ambiguous",
                "n_documents",
                "n_exact",
                "n_pairs",
                "n_positive",
                "n_pred_conflicts",
                "n_present",
                "n_questions",
                "n_subclass_scored",
                "n_subclass_unannotated",
                "tp",
                "tn",
                "fp",
                "fn",
                "false_no_related_denominator",
            ),
            _COUNT,
        ),
        **dict.fromkeys(
            (
                "intake_changed_match",
                "intake_messy_match",
                "intake_hyphen_unwraps_match",
                "intake_collapsed_blanks_match",
                "intake_method_valid",
            ),
            _INTAKE,
        ),
        **dict.fromkeys(
            ("raw_chars", "intake_chars", "cleaned_chars"),
            "a character count of one document's text in the intake task's detail, not a metric",
        ),
        "reason": "the reason an unscorable MAUD payload gives (null when scored); a Measure's reason",
        "ttft_delta": "a two-run comparison output (delta_significance): the pairing of ttft_seconds",
        "e2e_delta": "a two-run comparison output (delta_significance): the pairing of e2e_latency_seconds",
    }
)

#: The rows R2's keep rule reads for per-field, per-class extraction accuracy.
PER_FIELD_ROWS = ("per_field_scores",)


@pytest.fixture(scope="module")
def table() -> MetricTable:
    return load_metric_table()


def _old_ids() -> list[str]:
    return list(OLD["order"])


def test_the_fixture_is_from_the_old_commit() -> None:
    assert OLD["old_commit"] == OLD_DOJO_COMMIT
    assert len(OLD["metrics"]) == 184 == len(OLD["order"])
    assert sum(entry["tier"] == 0 for entry in OLD["metrics"].values()) == 14


def test_the_table_names_its_source(table: MetricTable) -> None:
    assert table.source["old_dojo_commit"] == OLD_DOJO_COMMIT
    assert table.source["old_dojo_version"] == OLD["old_version"]


def test_every_old_id_is_a_row_in_the_old_order(table: MetricTable) -> None:
    registry_rows = [row.id for row in table.rows if row.origin == "registry"]
    assert registry_rows == _old_ids()


@pytest.mark.parametrize("metric", _old_ids())
def test_each_old_metric_keeps_its_meaning(table: MetricTable, metric: str) -> None:
    old = OLD["metrics"][metric]
    row: MetricRow = table.get(metric)
    assert row.aggregation == old["aggregation"]
    assert list(row.expanded_agents) == old["applicable_agents"]
    assert row.ground_truth == (old["ground_truth"] or None)
    assert row.description == old["description"]
    assert row.unit == old["units"]
    assert row.citation == old["citation"]
    assert row.inclusion == old["inclusion"]
    assert row.notes == old["notes"]
    assert row.old_source == old["source"]
    assert row.headline == (old["tier"] == 0)


def test_the_agent_families_are_the_old_roster(table: MetricTable) -> None:
    assert {name: list(members) for name, members in table.agent_families.items()} == OLD["agent_families"]


def test_new_rows_come_from_the_golden_baseline(table: MetricTable) -> None:
    new = {row.id for row in table.rows if row.id not in OLD["metrics"]}
    golden = {row.id for row in table.rows if row.origin == "golden"}
    assert new == golden
    assert new <= set(OLD["golden"]["names"])


def test_the_old_metric_id_code_and_doc_agree() -> None:
    assert OLD["metric_ids"]["code"] == OLD["metric_ids"]["doc"]


def test_metric_ids_are_the_old_canonical_ids(table: MetricTable) -> None:
    canonical = OLD["metric_ids"]["code"]
    for row in table.rows:
        assert row.metric_id == canonical.get(row.id, f"registry:{row.id}"), row.id


def test_headline_ids_match_metric_ids_md(table: MetricTable) -> None:
    doc = OLD["metric_ids"]["doc"]
    headline = table.headline_rows()
    assert {row.id for row in headline} == {
        name for name, entry in OLD["metrics"].items() if entry["tier"] == 0
    }
    in_doc = [row for row in headline if row.id in doc]
    assert len(in_doc) == 11  # ttft_seconds, tokens_per_second and bert_elapsed_ms have no canonical id
    for row in in_doc:
        assert row.metric_id == doc[row.id]


def test_every_golden_name_maps_to_a_row_or_a_reasoned_exception(table: MetricTable) -> None:
    names = set(OLD["golden"]["names"])
    assert not set(GOLDEN_ALIASES) & set(GOLDEN_EXCEPTIONS)
    assert set(GOLDEN_ALIASES) | set(GOLDEN_EXCEPTIONS) <= names, "a stale alias or exception"
    unmapped = sorted(
        name
        for name in names
        if name not in table and name not in GOLDEN_ALIASES and name not in GOLDEN_EXCEPTIONS
    )
    assert unmapped == []
    for alias, (target, reason) in GOLDEN_ALIASES.items():
        assert alias not in table and target in table and reason, alias


def test_every_golden_metric_is_kept_for_the_parity_gate(table: MetricTable) -> None:
    names = set(OLD["golden"]["names"])
    golden_rows = {GOLDEN_ALIASES[name][0] if name in GOLDEN_ALIASES else name for name in names} - set(
        GOLDEN_EXCEPTIONS
    )
    for metric in golden_rows:
        row = table.get(metric)
        assert row.status != "not_ported" and "golden_parity_gate" in row.readers, metric
    assert {row.id for row in table.rows_for_reader("golden_parity_gate")} == golden_rows


def test_only_read_metrics_are_kept(table: MetricTable) -> None:
    for row in table.rows:
        if row.status == "not_ported":
            assert not row.readers and row.status_reason, row.id
        else:
            assert row.readers, row.id


def test_per_field_extraction_accuracy_is_kept_for_the_keep_rule(table: MetricTable) -> None:
    for metric in PER_FIELD_ROWS:
        row = table.get(metric)
        assert row.status != "not_ported"
        assert {"keep_rule", "specialist_report"} <= set(row.readers)
        assert row.aggregation == "none"  # per field, never averaged away


def test_counts(table: MetricTable) -> None:
    """The table's shape, so a change to it is a visible, reviewed change."""
    statuses = [row.status for row in table.rows]
    assert len(table.rows) == 184 + 9
    assert len(table.headline_rows()) == 14
    assert {status: statuses.count(status) for status in set(statuses)} == {
        "implemented": 19,
        "planned": 92,
        "not_ported": 82,
    }
