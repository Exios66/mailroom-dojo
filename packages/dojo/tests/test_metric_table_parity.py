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

_INTERVAL = (
    "a bootstrap interval of its metric (lo, hi, half, n, seed, n_boot): a Measure's ci and n, not a metric"
)
_SIDES = (
    "the serving comparison's per-side copy (api, local, delta) of figures that are rows already "
    "(ttft_seconds, e2e_latency_seconds, cost_per_document, estimated_cost_usd, ...), plus run settings: "
    "a breakdown by serving side, D5"
)
_PER_FIELD = "a per-field breakdown of extraction rows (per_field_scores, the entity_list_* rows); D3b"
#: Every nested block of a golden payload (the nested-breakdown rule in
#: scripts/capture_old_metric_registry.py) and what it is. None of them adds a metric id.
BREAKDOWN_BLOCKS: Mapping[str, str] = MappingProxyType(
    {
        "chained_summary.extractor": "the chained run's extractor stage: leaves mapped one by one below",
        "chained_summary.sorter": "the chained run's sorter stage: leaves mapped one by one below",
        "chained_summary.weights": "the composite's weights: leaves mapped one by one below",
        "score.per_question": "MAUD per-question breakdown: leaves mapped one by one below",
        "score.api": _SIDES,
        "score.local": _SIDES,
        "score.cost": _SIDES,
        "score.quality": _SIDES,
        "score.scorecard": _SIDES,
        "score.binary": (
            "LegalBench's binary breakdown: accuracy, precision, recall, f1, f2 and counts of one task"
        ),
        "score.confusion": "the confusion matrix (row confusion_matrix); its cells are counts",
        "score.doc_type_accuracy_ci": _INTERVAL,
        "score.exact_match_ci": _INTERVAL,
        "score.subclass_accuracy_ci": _INTERVAL,
        "score.entity_list_audit": "per-field entity-list audit detail (field_scoring audits), not a metric",
        "score.entity_list_scores": _PER_FIELD,
        "score.extraction": "an auditor suite's nested extraction score: " + _PER_FIELD,
        "score.field_scores": _PER_FIELD,
        "score_document.extraction": _PER_FIELD,
        "score.per_category": "ContractEval per-category breakdown of the classification and Jaccard rows",
        "score.per_class": "per-class breakdown of the classification rows (R2: per class is standard)",
        "score.per_subclass": "per-subclass breakdown of the classification rows",
        "score.prep_invariants": "the per-invariant flags behind intake_prep_completeness",
        "score.trace": "per-document trace detail behind confidence_calibration_error",
        "score_document.detail": (
            "per-document detail under the names of rows amount_exactness and determination_consistency"
        ),
        "score_document.provenance": "the provenance stamp (a Measure's provenance), not a metric",
        "score_document.schema": "schema detail (the subtype index), not a metric",
        "score_document.schema_promotion_gate": (
            "schema_valid against its gate threshold (row schema_valid; the threshold is configuration)"
        ),
    }
)
#: The blocks whose leaves carry metric values under their own names: each leaf maps to
#: the row it breaks down (``row:<id>``) or to a reason it is not a metric.
BREAKDOWN_LEAVES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "chained_summary.sorter": {
            "exact_match": "row:exact_match",
            "subtype_accuracy": "row:subclass_accuracy",
        },
        "chained_summary.extractor": {
            "overall_extraction_score": "row:extraction_overall_score",
            "field_presence": "row:field_presence",
        },
        "chained_summary.weights": {
            "sorter": "configuration: chained_composite's sorter weight (default 0.25), not a measurement",
            "extractor": (
                "configuration: chained_composite's extractor weight (default 0.75), not a measurement"
            ),
        },
        "score.per_question": {
            "accuracy": "row:maud_question_accuracy",
            "category_accuracy": "row:maud_category_accuracy",
            "presence": "row:maud_clause_presence",
            "valid_class_rate": "row:maud_valid_class_rate",
            "n": _COUNT,
        },
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


#: The old units the table writes differently, with the same meaning: one spelling of a unit
#: (``usd`` is ``USD``, ``int`` is ``count``), and the rows whose value is a structure, which
#: the old registry labelled ``float[0,1]``.
UNIT_NORMALISED: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        ("reviewer_cost_per_accepted_usd", "usd"): "USD",
        ("reviewer_accept_rule_violations", "int"): "count",
        ("confusion_matrix", "float[0,1]"): "structure",
        ("per_class_stats", "float[0,1]"): "structure",
        ("bootstrap_ci", "float[0,1]"): "structure",
    }
)


def test_every_unit_normalisation_is_used(table: MetricTable) -> None:
    for (metric, old_unit), unit in UNIT_NORMALISED.items():
        assert OLD["metrics"][metric]["units"] == old_unit
        assert table.get(metric).unit == unit


@pytest.mark.parametrize("metric", _old_ids())
def test_each_old_metric_keeps_its_meaning(table: MetricTable, metric: str) -> None:
    old = OLD["metrics"][metric]
    row: MetricRow = table.get(metric)
    assert row.aggregation == old["aggregation"]
    assert list(row.expanded_agents) == old["applicable_agents"]
    assert row.ground_truth == (old["ground_truth"] or None)
    assert row.description == old["description"]
    assert row.unit == UNIT_NORMALISED.get((metric, old["units"]), old["units"])
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


def test_every_nested_block_is_a_breakdown_with_a_reason() -> None:
    blocks = OLD["golden"]["breakdowns"]
    assert set(blocks) == set(BREAKDOWN_BLOCKS), "a nested block is unlisted, or a listed one is stale"
    assert all(reason.strip() for reason in BREAKDOWN_BLOCKS.values())
    assert set(BREAKDOWN_LEAVES) <= set(BREAKDOWN_BLOCKS)


def test_every_leaf_of_a_metric_block_maps_to_a_kept_row_or_a_reason(table: MetricTable) -> None:
    for block, mapping in BREAKDOWN_LEAVES.items():
        assert set(OLD["golden"]["breakdowns"][block]["leaves"]) == set(mapping), block
        for leaf, target in mapping.items():
            if target.startswith("row:"):
                row = table.get(target.removeprefix("row:"))
                assert row.status != "not_ported", (block, leaf)
            else:
                assert target.strip(), (block, leaf)


def test_the_chained_composite_is_a_row(table: MetricTable) -> None:
    assert "chained_summary" in "".join(OLD["golden"]["names"]["composite"])
    row = table.get("composite")
    assert row.origin == "golden" and "golden_parity_gate" in row.readers


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
    for metric in sorted(OWNER_DROPPED_GOLDEN):
        row = table.get(metric)
        assert row.status == "not_ported" and "owner decision" in str(row.status_reason), metric
    kept = golden_rows - OWNER_DROPPED_GOLDEN
    for metric in kept:
        row = table.get(metric)
        assert row.status != "not_ported" and "golden_parity_gate" in row.readers, metric
    assert {row.id for row in table.rows_for_reader("golden_parity_gate")} == kept


#: Golden metrics the owner dropped (2026-11-18): intake's clerk diagnostics are not needed to
#: judge intake. The rows keep their meaning; D3b lists them as intended parity differences.
OWNER_DROPPED_GOLDEN = frozenset(
    {
        "intake_prep_completeness",
        "intake_changed_rate",
        "intake_messy_rate",
        "intake_hyphen_unwraps",
        "intake_collapsed_blanks",
    }
)


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
    assert len(table.rows) == 184 + 10
    assert len(table.headline_rows()) == 14
    assert {status: statuses.count(status) for status in set(statuses)} == {
        "implemented": 19,
        "planned": 89,
        "not_ported": 86,
    }
