"""The metric table loader: the packaged table, its content hash, refusals and ``check``.

Fault injection: every rule of the loader is broken once on a copy of the real table
and must raise ``MetricTableError`` naming the YAML path; damaged files (missing,
empty, not UTF-8, not YAML, truncated at any byte) raise ``MetricTableError`` and
nothing else.
"""

from __future__ import annotations

import copy
import importlib
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from mailroom_dojo.measures import (
    READERS,
    ConfidenceInterval,
    Measure,
    MeasureError,
    MetricTableError,
    UnknownMetricError,
    load_metric_table,
    metric_table_canonical_form,
)
from mailroom_dojo.measures.table import _hash

PACKAGED_TEXT = resources.files("mailroom_dojo.measures").joinpath("metrics.yaml").read_text(encoding="utf-8")


PACKAGED_RAW = MappingProxyType(yaml.safe_load(PACKAGED_TEXT))
PACKAGED_ROW_COUNT = len(PACKAGED_RAW["metrics"])


def _raw() -> dict[str, Any]:
    """A fresh parsed copy of the packaged table."""
    return copy.deepcopy(dict(PACKAGED_RAW))


def _row_index(raw: dict[str, Any], metric: str) -> int:
    return next(index for index, row in enumerate(raw["metrics"]) if row["id"] == metric)


def test_the_packaged_table_loads() -> None:
    table = load_metric_table()
    assert len(table.rows) >= 184
    assert table.version == 1
    assert len(table.content_hash) == 64 and int(table.content_hash, 16) >= 0
    assert "f1_macro" in table and "not_a_metric" not in table


def test_the_hash_is_the_canonical_form_hash_and_is_stable(tmp_path: Path) -> None:
    table = load_metric_table()
    assert table.content_hash == _hash(metric_table_canonical_form(table))
    copy_path = tmp_path / "metrics.yaml"
    copy_path.write_text(PACKAGED_TEXT, encoding="utf-8")
    assert load_metric_table(copy_path).content_hash == table.content_hash
    assert load_metric_table(_raw()).content_hash == table.content_hash


def test_comments_and_key_order_do_not_change_the_hash() -> None:
    raw = _raw()
    reordered = {key: raw[key] for key in reversed(list(raw))}
    reordered["metrics"] = [{key: row[key] for key in reversed(list(row))} for row in raw["metrics"]]
    commented = "# another comment\n" + PACKAGED_TEXT.replace("\nmetrics:\n", "\n# rows\nmetrics:\n")
    expected = load_metric_table().content_hash
    assert load_metric_table(reordered).content_hash == expected
    assert load_metric_table(yaml.safe_load(commented)).content_hash == expected


def test_writing_a_default_explicitly_does_not_change_the_hash() -> None:
    raw = _raw()
    raw["metrics"][0].setdefault("noise_floor", None)
    raw["metrics"][0].setdefault("notes", "")
    assert load_metric_table(raw).content_hash == load_metric_table().content_hash


@pytest.mark.parametrize(
    "edit",
    [
        lambda raw: raw["metrics"][0].__setitem__("description", raw["metrics"][0]["description"] + "."),
        lambda raw: raw.__setitem__("version", 2),
        lambda raw: raw["metrics"].reverse(),
        lambda raw: raw["metrics"][0].__setitem__("noise_floor", 0.01),
        lambda raw: raw["default_views"]["sorter"].__setitem__("metric", "accuracy"),
        lambda raw: raw["default_views"]["sorter"].__setitem__("confirmed", False),
    ],
    ids=["description", "version", "row order", "noise floor", "default view", "view confirmation"],
)
def test_any_content_change_changes_the_hash(edit: Callable[[dict[str, Any]], None]) -> None:
    raw = _raw()
    edit(raw)
    assert load_metric_table(raw).content_hash != load_metric_table().content_hash


def _set(metric: str, key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def edit(raw: dict[str, Any]) -> None:
        raw["metrics"][_row_index(raw, metric)][key] = value

    return edit


def _drop(metric: str, key: str) -> Callable[[dict[str, Any]], None]:
    def edit(raw: dict[str, Any]) -> None:
        del raw["metrics"][_row_index(raw, metric)][key]

    return edit


def _view(agent: str, **changes: Any) -> Callable[[dict[str, Any]], None]:
    def edit(raw: dict[str, Any]) -> None:
        view = raw["default_views"][agent]
        for key, value in changes.items():
            if value is None and key == "reason":
                view.pop(key, None)
            else:
                view[key] = value

    return edit


#: (id, edit, the expected start of the path and a fragment of the message).
FAULTS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("unknown top key", lambda raw: raw.__setitem__("tiers", {}), "tiers: unknown key"),
    ("missing top key", lambda raw: raw.pop("agent_families"), "agent_families: missing"),
    ("table format", lambda raw: raw.__setitem__("table_format", 2), "table_format: this loader reads"),
    ("version zero", lambda raw: raw.__setitem__("version", 0), "version: must be a positive int"),
    ("version bool", lambda raw: raw.__setitem__("version", True), "version: must be a positive int"),
    ("no rows", lambda raw: raw.__setitem__("metrics", []), "metrics: must be a non-empty list"),
    (
        "row not a mapping",
        lambda raw: raw["metrics"].__setitem__(0, "f1_macro"),
        "metrics[0]: must be a mapping",
    ),
    ("unknown row key", _set("f1_macro", "tier", 0), "metrics[0].tier: unknown key"),
    ("missing direction", _drop("accuracy", "direction"), "metrics[1].direction: missing"),
    ("bad id", _set("f1_macro", "id", "F1 Macro"), "metrics[0].id: 'F1 Macro' is not a metric id"),
    ("bad direction", _set("f1_macro", "direction", "up"), "(f1_macro).direction: 'up' is not one of"),
    ("bad unit", _set("f1_macro", "unit", "percent"), "(f1_macro).unit: 'percent' is not one of"),
    ("bad aggregation", _set("f1_macro", "aggregation", "median"), "(f1_macro).aggregation"),
    ("bad ground truth", _set("f1_macro", "ground_truth", ""), "(f1_macro).ground_truth"),
    ("headline not bool", _set("f1_macro", "headline", "yes"), "(f1_macro).headline: must be true or false"),
    ("cost not bool", _set("f1_macro", "cost", 1), "(f1_macro).cost: must be true or false"),
    ("empty description", _set("f1_macro", "description", " "), "(f1_macro).description: must not be empty"),
    ("citation not text", _set("f1_macro", "citation", 3), "(f1_macro).citation: must be a string"),
    (
        "unknown family",
        _set("f1_macro", "agents", ["SPECIALIST"]),
        "(f1_macro).agents[0]: unknown agent family",
    ),
    ("bad agent token", _set("f1_macro", "agents", ["Sorter Agent"]), "(f1_macro).agents[0]"),
    ("agent twice", _set("f1_macro", "agents", ["sorter", "sorter"]), "(f1_macro).agents[1]"),
    ("no agents", _set("f1_macro", "agents", []), "(f1_macro).agents: must be a non-empty list"),
    ("bad layer", _set("f1_macro", "layer", "D9"), "(f1_macro).layer"),
    ("bad status", _set("f1_macro", "status", "done"), "(f1_macro).status"),
    (
        "not ported without reason",
        _drop("bert_elapsed_ms", "status_reason"),
        "(bert_elapsed_ms).status_reason",
    ),
    (
        "not ported with layer",
        _set("bert_elapsed_ms", "layer", "D5"),
        "(bert_elapsed_ms).layer: a not_ported",
    ),
    (
        "not ported with reader",
        _set("bert_elapsed_ms", "readers", ["run_report"]),
        "(bert_elapsed_ms).readers",
    ),
    ("planned without layer", _set("schema_valid", "layer", None), "(schema_valid).layer: a planned metric"),
    ("planned without reader", _drop("schema_valid", "readers"), "(schema_valid).readers: a planned metric"),
    ("planned with producer", _set("schema_valid", "producer", "x.y"), "(schema_valid).layer: a planned"),
    ("implemented without producer", _drop("f1_macro", "producer"), "(f1_macro).producer: an implemented"),
    ("implemented in a chunk", _set("f1_macro", "layer", "D3b"), "(f1_macro).producer: an implemented"),
    ("unknown reader", _set("f1_macro", "readers", ["dashboard"]), "(f1_macro).readers[0]: 'dashboard'"),
    ("reader twice", _set("schema_valid", "readers", ["run_report", "run_report"]), "lists a reader twice"),
    ("bad pairing", _set("f1_macro", "pairing", "paired_t"), "(f1_macro).pairing"),
    (
        "keep rule without direction",
        _set("extraction_overall_score", "direction", "none"),
        "(extraction_overall_score).direction: a metric the keep rule reads needs a direction",
    ),
    ("negative noise floor", _set("f1_macro", "noise_floor", -0.1), "(f1_macro).noise_floor"),
    ("nan noise floor", _set("f1_macro", "noise_floor", float("nan")), "(f1_macro).noise_floor"),
    ("bool noise floor", _set("f1_macro", "noise_floor", True), "(f1_macro).noise_floor"),
    ("foreign registry id", _set("f1", "metric_id", "registry:f2"), "(f1).metric_id: a registry id names"),
    (
        "headline agent without a view",
        lambda raw: raw["default_views"].pop("reporter"),
        "default_views: 'reporter' has headline metrics but no default view",
    ),
    (
        "view metric unknown",
        _view("sorter", metric="f9"),
        "default_views.sorter.metric: 'f9' is not a metric",
    ),
    ("view not ported", _view("intake", metric="bert_elapsed_ms"), "'bert_elapsed_ms' is not ported"),
    ("view not for agent", _view("sorter", metric="wer"), "'wer' does not apply to 'sorter'"),
    ("confirmed non-headline", _view("arbiter", confirmed=True), "is not a headline metric"),
    (
        "headline agent non-headline",
        _view("sorter_reviewer", metric="reviewer_confidence_ece"),
        "is not a headline",
    ),
    (
        "confirmed without direction",
        _view("sorter_reviewer", metric="reviewer_routed_to_human_review_rate", confirmed=True),
        "has no direction; only an unconfirmed proposal may",
    ),
    (
        "null without reason",
        _view("archivist", reason=None),
        "default_views.archivist.reason: an agent with no",
    ),
    (
        "null confirmed",
        _view("archivist", confirmed=True),
        "default_views.archivist.confirmed: an agent with no",
    ),
    ("reason with metric", _view("sorter", reason="because"), "default_views.sorter.reason: only an agent"),
    ("empty source", _view("sorter", source=" "), "default_views.sorter.source: must not be empty"),
    ("confirmed not bool", _view("sorter", confirmed="yes"), "default_views.sorter.confirmed: must be true"),
    ("unknown view key", _view("sorter", tier=0), "default_views.sorter.tier: unknown key"),
    (
        "missing view key",
        lambda raw: raw["default_views"]["sorter"].pop("source"),
        "default_views.sorter.source: missing",
    ),
    (
        "unknown agent",
        lambda raw: raw["default_views"].__setitem__("sortr", dict(raw["default_views"]["sorter"])),
        "'sortr' is not an agent of any metric row",
    ),
    ("bad secondary", _view("local_vs_api", secondary=["wer"]), "secondary[0]: 'wer' does not apply"),
    ("secondary twice", _view("local_vs_api", secondary=["ttft_seconds", "ttft_seconds"]), "listed twice"),
    ("secondary is metric", _view("local_vs_api", secondary=["tokens_per_second"]), "listed twice"),
    (
        "views not a mapping",
        lambda raw: raw.__setitem__("default_views", []),
        "default_views: must be a mapping",
    ),
    (
        "bad family name",
        lambda raw: raw["agent_families"].__setitem__("all", ["sorter"]),
        "agent_families.all",
    ),
    ("family agent", lambda raw: raw["agent_families"].__setitem__("X", ["Bad"]), "agent_families.X[0]"),
    (
        "source not text",
        lambda raw: raw["source"].__setitem__("old_dojo_commit", 7),
        "source.old_dojo_commit",
    ),
]


@pytest.mark.parametrize(
    ("edit", "expected"),
    [(edit, expected) for _, edit, expected in FAULTS],
    ids=[name for name, _, _ in FAULTS],
)
def test_each_broken_rule_is_refused_naming_the_path(
    edit: Callable[[dict[str, Any]], None], expected: str
) -> None:
    raw = _raw()
    edit(raw)
    with pytest.raises(MetricTableError) as caught:
        load_metric_table(raw)
    assert expected in str(caught.value)


def test_a_duplicate_id_is_refused() -> None:
    raw = _raw()
    raw["metrics"].append(copy.deepcopy(raw["metrics"][0]))
    with pytest.raises(MetricTableError, match=r"'f1_macro' repeats metrics\[0\]"):
        load_metric_table(raw)


def test_a_shared_comparison_id_is_refused() -> None:
    raw = _raw()
    raw["metrics"][_row_index(raw, "accuracy")]["metric_id"] = "pipeline.classification.f1_macro"
    with pytest.raises(MetricTableError, match="already the id of 'f1_macro'"):
        load_metric_table(raw)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"", "the metric table must be a mapping"),
        (b"metrics: [\n", "not valid YAML"),
        (b"\xff\xfe\x00bad", "cannot read the metric table"),
        (b"- 1\n- 2\n", "the metric table must be a mapping"),
    ],
    ids=["empty", "not yaml", "not utf-8", "a list"],
)
def test_damaged_files_are_refused(tmp_path: Path, content: bytes, expected: str) -> None:
    path = tmp_path / "metrics.yaml"
    path.write_bytes(content)
    with pytest.raises(MetricTableError, match=expected):
        load_metric_table(path)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MetricTableError, match="cannot read the metric table"):
        load_metric_table(tmp_path / "absent.yaml")


@settings(max_examples=25)
@given(st.integers(min_value=0, max_value=len(PACKAGED_TEXT.encode("utf-8")) - 1))
def test_a_truncated_table_loads_or_is_refused_never_crashes(
    tmp_path_factory: pytest.TempPathFactory, cut: int
) -> None:
    path = tmp_path_factory.mktemp("truncated") / "metrics.yaml"
    path.write_bytes(PACKAGED_TEXT.encode("utf-8")[:cut])
    try:
        table = load_metric_table(path)
    except MetricTableError:
        return
    # A cut that still loads ends on a row boundary: a valid table with fewer rows.
    assert len(table.rows) <= PACKAGED_ROW_COUNT


def test_every_implemented_metric_names_a_real_producer() -> None:
    implemented = [row for row in load_metric_table().rows if row.status == "implemented"]
    assert implemented
    for row in implemented:
        module, _, name = str(row.producer).rpartition(".")
        assert callable(getattr(importlib.import_module(module), name)), row.id


def test_queries_replace_tiers() -> None:
    table = load_metric_table()
    sorter_rows = {row.id for row in table.rows_for_agent("sorter")}
    assert {"f1_macro", "accuracy", "subclass_f1_macro"} <= sorter_rows
    assert "wer" not in sorter_rows
    assert {row.id for row in table.rows_for_agent("sorter", headline_only=True)} == {"f1_macro", "accuracy"}
    assert all("keep_rule" in row.readers for row in table.rows_for_reader("keep_rule"))
    with pytest.raises(UnknownMetricError, match="unknown reader"):
        table.rows_for_reader("dashboard")
    assert set(READERS) == {reader for row in table.rows for reader in row.readers}
    with pytest.raises(UnknownMetricError, match="unknown metric 'tier0'"):
        table.get("tier0")


def _ci(low: float, high: float) -> ConfidenceInterval:
    return ConfidenceInterval(low=low, high=high, level=0.95, method="wilson")


def _measure(method: str, **overrides: Any) -> Measure:
    fields: dict[str, Any] = {"value": 0.5, "n": 4, "cost_basis": "none", "label_status": "gold"}
    fields.update(overrides)
    return Measure(method=method, **fields)


@pytest.mark.parametrize(
    ("measure", "expected"),
    [
        (lambda: _measure("accuracy", value=1.2), r"value 1.2 is outside \[0, 1\]"),
        (lambda: _measure("prompt_tokens", value=10.5), "a count is a whole number"),
        (lambda: _measure("reviewer_accept_rule_violations", value=2.5), "a int is a whole number"),
        (lambda: _measure("accuracy", value=0.5, ci=_ci(0.6, 0.9)), "does not contain the value 0.5"),
        (lambda: _measure("accuracy", value=0.95, ci=_ci(0.9, 1.05)), r"ci.high 1.05 is outside \[0, 1\]"),
        (lambda: _measure("wer", value=0.1, ci=_ci(-0.2, 0.3)), "ci.low -0.2 is negative"),
        (lambda: _measure("accuracy", value=-0.1), r"outside \[0, 1\]"),
        (lambda: _measure("wer", value=-0.5), "negative"),
        (lambda: _measure("accuracy", label_status="none"), "needs labels"),
        (lambda: _measure("cost_per_document", value=0.02), "a cost needs a cost basis"),
        (lambda: _measure("accuracy", cost_basis="busy_window"), "not a cost"),
        (lambda: _measure("bert_elapsed_ms", value=3.0, label_status="none"), "not ported"),
        (lambda: _measure("judge_notes", value=1.0), "not ported"),
    ],
    ids=[
        "rate above 1",
        "fractional count",
        "fractional int",
        "interval misses value",
        "interval above 1",
        "interval below 0",
        "rate below 0",
        "negative error",
        "labels",
        "cost basis",
        "not a cost",
        "dropped",
        "text",
    ],
)
def test_check_refuses_a_measure_that_breaks_its_row(measure: Callable[[], Measure], expected: str) -> None:
    with pytest.raises(MeasureError, match=expected):
        load_metric_table().check(measure())


def test_check_refuses_a_non_numeric_unit() -> None:
    raw = _raw()
    row = raw["metrics"][_row_index(raw, "model")]
    row.update(status="planned", layer="D5", readers=["run_report"])
    del row["status_reason"]
    with pytest.raises(MeasureError, match="is not a number"):
        load_metric_table(raw).check(_measure("model", label_status="none"))


def test_check_accepts_measures_that_keep_their_rows() -> None:
    table = load_metric_table()
    assert table.check(_measure("accuracy")).id == "accuracy"
    assert table.check(_measure("wer", value=1.7)).unit == "error_rate"
    assert table.check(_measure("cost_per_document", value=0.02, cost_basis="token_price_estimate")).cost
    assert table.check(Measure.missing(method="accuracy", reason="gt_missing"))
    assert table.check(_measure("prompt_tokens", value=120, label_status="none")).unit == "count"
    assert table.check(_measure("prompt_tokens", value=120.0, label_status="none")).unit == "count"
    assert (
        table.check(_measure("intake_hyphen_unwraps", value=1.5, label_status="none")).aggregation == "mean"
    )
    assert table.check(_measure("accuracy", value=0.5, ci=_ci(0.5, 1.0))).id == "accuracy"
    assert table.check(_measure("wer", value=1.7, ci=_ci(1.2, 2.4))).id == "wer"
    with pytest.raises(UnknownMetricError):
        table.check(_measure("not_a_metric"))


#: The default views the registry names (owner decisions, 2026-11-18); every other is a proposal.
CONFIRMED_VIEWS = {
    "sorter": "f1_macro",
    "judge": "f1_macro",
    "contracts_specialist": "extraction_overall_score",
    "merger_agreement_specialist": "extraction_overall_score",
    "corporate_records_specialist": "extraction_overall_score",
    "due_diligence_specialist": "extraction_overall_score",
    "compliance_specialist": "extraction_overall_score",
    "court_opinions_specialist": "extraction_overall_score",
    "insurance_claims_specialist": "extraction_f2",
    "correspondence_specialist": "content_topic_f1_macro",
}


def test_each_agent_with_a_headline_has_one_default_view() -> None:
    table = load_metric_table()
    headline_agents = {
        agent
        for row in table.headline_rows()
        if row.status != "not_ported"
        for agent in row.expanded_agents
        if agent != "ALL"
    }
    assert headline_agents <= set(table.default_views)
    for agent, view in table.default_views.items():
        assert view.agent == agent and view.source.strip()
        if view.metric is None:
            assert view.reason and not view.confirmed
            continue
        row = table.get(view.metric)
        assert row.status != "not_ported" and row.applies_to(agent)
        if view.confirmed or agent in headline_agents:
            assert row.headline, agent
        if view.confirmed:
            assert row.direction != "none", agent


def test_only_the_registry_named_views_are_confirmed() -> None:
    table = load_metric_table()
    confirmed = {agent: view.metric for agent, view in table.default_views.items() if view.confirmed}
    assert confirmed == CONFIRMED_VIEWS
    assert table.default_view("archivist") is not None and table.default_view("archivist").metric is None  # type: ignore[union-attr]
    assert table.default_view("sorter_reviewer").secondary == ("reviewer_wrong_accept_upper_95",)  # type: ignore[union-attr]
    assert table.default_view("nobody") is None


def test_a_repeated_key_is_refused(tmp_path: Path) -> None:
    text = PACKAGED_TEXT.replace("default_views:\n", "default_views:\n  sorter:\n    metric: accuracy\n", 1)
    path = tmp_path / "metrics.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(MetricTableError, match="found duplicate key 'sorter'"):
        load_metric_table(path)
