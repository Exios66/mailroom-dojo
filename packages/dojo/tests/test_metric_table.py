"""The metric table loader: the packaged table, its content hash, refusals and ``check``.

Fault injection: every rule of the loader is broken once on a copy of the real table
and must raise ``MetricTableError`` naming the YAML path; damaged files (missing,
empty, not UTF-8, not YAML, truncated at any byte) raise ``MetricTableError`` and
nothing else.
"""

from __future__ import annotations

import copy
import importlib
import re
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
    PAIRING_METHODS,
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
        lambda raw: raw["default_views"][CONFIRMED_AGENT].__setitem__("source", "another source"),
        lambda raw: raw["default_views"][CONFIRMED_AGENT].__setitem__("confirmed", False),
        lambda raw: raw["metrics"][0].__setitem__("direction", "lower"),
    ],
    ids=[
        "description",
        "version",
        "row order",
        "noise floor",
        "view source",
        "view confirmation",
        "direction",
    ],
)
def test_any_content_change_changes_the_hash(edit: Callable[[dict[str, Any]], None]) -> None:
    raw = _raw()
    edit(raw)
    assert load_metric_table(raw).content_hash != load_metric_table().content_hash


def _set(metric: str, key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def edit(raw: dict[str, Any]) -> None:
        raw["metrics"][_row_index(raw, metric)][key] = value

    return edit


def _sequence(*edits: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    def edit(raw: dict[str, Any]) -> None:
        for each in edits:
            each(raw)

    return edit


def _drop(metric: str, key: str) -> Callable[[dict[str, Any]], None]:
    def edit(raw: dict[str, Any]) -> None:
        del raw["metrics"][_row_index(raw, metric)][key]

    return edit


_DELETE = object()


def _views_by(predicate: Callable[[str, dict[str, Any]], bool]) -> list[str]:
    return [agent for agent, view in PACKAGED_RAW["default_views"].items() if predicate(agent, view)]


def _headline_agents() -> set[str]:
    families = PACKAGED_RAW["agent_families"]
    out: set[str] = set()
    for row in PACKAGED_RAW["metrics"]:
        if row["headline"] and row["status"] != "not_ported":
            for token in row["agents"]:
                out.update(families.get(token, [token]))
    out.discard("ALL")
    return out


def _rows_for(agent: str) -> list[dict[str, Any]]:
    families = PACKAGED_RAW["agent_families"]
    return [
        row
        for row in PACKAGED_RAW["metrics"]
        if row["status"] != "not_ported"
        and any(token == "ALL" or agent in families.get(token, [token]) for token in row["agents"])
    ]


def _roster(status: str) -> list[str]:
    return [agent for agent, entry in PACKAGED_RAW["agent_roster"].items() if entry["status"] == status]


# Agents and metrics picked from the packaged data by property, so the owner can change
# any view by editing the YAML without touching this file.
CONFIRMED_AGENT = _views_by(lambda _, view: view["confirmed"] and view["metric"] is not None)[0]
CONFIRMED_METRIC = PACKAGED_RAW["default_views"][CONFIRMED_AGENT]["metric"]
LIVE_AGENT = _roster("live")[0]
LEGACY_AGENT = _roster("legacy")[0]
RETIRED_AGENT = _roster("retired")[0]
GUARDED_AGENT = _views_by(lambda _, view: view.get("guard") is not None)[0]
UNGUARDED_AGENT = _views_by(lambda _, view: view["metric"] is not None and view.get("guard") is None)[0]
LABELLED_AGENT = _views_by(lambda _, view: bool(view.get("label_layers")))[0]
UNLABELLED_AGENT = _views_by(lambda _, view: view["metric"] is not None and view.get("label_layers") == [])[0]
NO_METRIC_AGENT = _views_by(lambda _, view: view["metric"] is None)[0]
FOREIGN_METRIC = next(
    row["id"]
    for row in PACKAGED_RAW["metrics"]
    if row not in _rows_for(CONFIRMED_AGENT) and row["status"] != "not_ported"
)
DIRECTIONLESS_METRIC = next(
    row["id"]
    for row in _rows_for(CONFIRMED_AGENT)
    if row["direction"] == "none" and row["unit"] != "structure"
)


def _view(agent: str, **changes: Any) -> Callable[[dict[str, Any]], None]:
    def edit(raw: dict[str, Any]) -> None:
        view = raw["default_views"][agent]
        for key, value in changes.items():
            if value is _DELETE or (value is None and key == "reason"):
                view.pop(key, None)
            else:
                view[key] = value

    return edit


def _drop_view(agent: str) -> Callable[[dict[str, Any]], None]:
    return lambda raw: raw["default_views"].pop(agent)


def _copy_view(agent: str, new_agent: str) -> Callable[[dict[str, Any]], None]:
    return lambda raw: raw["default_views"].__setitem__(new_agent, dict(raw["default_views"][agent]))


def _not_ported(metric: str) -> Callable[[dict[str, Any]], None]:
    def edit(raw: dict[str, Any]) -> None:
        row = raw["metrics"][_row_index(raw, metric)]
        row.update(status="not_ported", layer=None, readers=[], status_reason="dropped for the test")

    return edit


def _append(path: Callable[[dict[str, Any]], list[Any]], item: Any) -> Callable[[dict[str, Any]], None]:
    return lambda raw: path(raw).append(item)


#: One edit per column the canonical form carries that the test above does not touch.
COLUMN_EDITS: dict[str, Callable[[dict[str, Any]], None]] = {
    "same_as": _set("laziness_rate", "same_as", None),
    "alias_of": _set("contracteval_false_no_related", "alias_of", None),
    "pairing": _set("tokens_per_second", "pairing", "paired_percentile_bootstrap"),
    "pairing_note": _set("f1_macro", "pairing_note", "a whole-set statistic"),
    "readers": _append(lambda raw: raw["metrics"][_row_index(raw, "accuracy")]["readers"], "keep_rule"),
    "status": _not_ported("judge_error_rate"),
    "status_reason": _set("bert_elapsed_ms", "status_reason", "another reason"),
    "agents": _set("judge_error_rate", "agents", ["ALL"]),
    "ground_truth": _set("bert_elapsed_ms", "ground_truth", "optional"),
    "headline": _set("bert_elapsed_ms", "headline", False),
    "cost": _set("bert_elapsed_ms", "cost", True),
    "metric_id": _set("bert_elapsed_ms", "metric_id", "pipeline.intake.bert_elapsed_ms"),
    "view guard": _view("sorter", guard=_DELETE),
    "view guard_confirmed": _view(GUARDED_AGENT, guard_confirmed=True),
    "view label_layers": lambda raw: raw["default_views"]["sorter"]["label_layers"].pop(),
    "view metric": _view("sorter", metric="accuracy", guard="f1_macro"),
    "agent_families": _append(lambda raw: raw["agent_families"]["INTAKE"], "sorter"),
    "agent_roster": lambda raw: raw["agent_roster"]["sorter"].__setitem__("note", "a note"),
}


def test_the_canonical_form_writes_every_row_and_view_field() -> None:
    """A field left out of the canonical form could change without moving the hash."""
    import dataclasses

    from mailroom_dojo.measures import DefaultView, MetricRow

    canonical = metric_table_canonical_form(load_metric_table())
    row_fields = {field.name for field in dataclasses.fields(MetricRow)} - {"expanded_agents"}
    view_fields = {field.name for field in dataclasses.fields(DefaultView)} - {"agent"}
    assert set(canonical["metrics"][0]) == row_fields
    assert all(set(view) == view_fields for view in canonical["default_views"].values())
    assert set(canonical) == {
        "table_format",
        "version",
        "source",
        "agent_families",
        "agent_roster",
        "default_views",
        "metrics",
    }


@pytest.mark.parametrize("edit", list(COLUMN_EDITS.values()), ids=list(COLUMN_EDITS))
def test_every_column_is_part_of_the_hash(edit: Callable[[dict[str, Any]], None]) -> None:
    """Each column the canonical form writes, edited alone on a valid table, moves the hash."""
    raw = _raw()
    edit(raw)
    assert load_metric_table(raw).content_hash != load_metric_table().content_hash


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
        "alias to nothing",
        _set("contracteval_false_no_related", "alias_of", "nope"),
        "'nope' is not another metric",
    ),
    (
        "alias to itself",
        _set("contracteval_false_no_related", "alias_of", "contracteval_false_no_related"),
        "is not another",
    ),
    (
        "alias to dropped",
        _set("contracteval_false_no_related", "alias_of", "laziness_rate"),
        "an alias or not ported",
    ),
    ("alias other unit", _set("contracteval_false_no_related", "unit", "count"), "unit 'count' differs"),
    (
        "kept alias",
        _set("false_no_related_rate", "alias_of", "accuracy"),
        "an alias is reported under its target",
    ),
    (
        "headline without pairing",
        _set("accuracy", "pairing", None),
        "(accuracy).pairing: a kept headline or keep-rule metric names its pairing",
    ),
    (
        "keep rule without pairing",
        _set("per_field_scores", "pairing", None),
        "(per_field_scores).pairing: a kept headline or keep-rule metric",
    ),
    ("pairing and note", _set("accuracy", "pairing_note", "why"), "(accuracy).pairing_note: a note says why"),
    (
        "empty pairing note",
        _set("f1_macro", "pairing_note", " "),
        "(f1_macro).pairing_note: must not be empty",
    ),
    (
        "not ported with pairing",
        _set("bert_elapsed_ms", "pairing", "paired_percentile_bootstrap"),
        "(bert_elapsed_ms).pairing",
    ),
    (
        "keep rule without direction",
        _set("extraction_overall_score", "direction", "none"),
        "(extraction_overall_score).direction: a metric the keep rule reads needs a direction",
    ),
    ("negative noise floor", _set("f1_macro", "noise_floor", -0.1), "(f1_macro).noise_floor"),
    ("nan noise floor", _set("f1_macro", "noise_floor", float("nan")), "(f1_macro).noise_floor"),
    ("bool noise floor", _set("f1_macro", "noise_floor", True), "(f1_macro).noise_floor"),
    ("foreign registry id", _set("f1", "metric_id", "registry:f2"), "(f1).metric_id: a registry id names"),
    ("live agent without a view", _drop_view(LIVE_AGENT), f"{LIVE_AGENT!r} is a live agent with no entry"),
    (
        "legacy agent with a view",
        _copy_view(CONFIRMED_AGENT, LEGACY_AGENT),
        "is legacy; it has no default view",
    ),
    (
        "retired agent with a view",
        _copy_view(CONFIRMED_AGENT, RETIRED_AGENT),
        "is retired; it has no default view",
    ),
    (
        "unknown agent",
        _copy_view(CONFIRMED_AGENT, "nobody_agent"),
        "'nobody_agent' is not an agent of the roster",
    ),
    (
        "view not a metric",
        _view(CONFIRMED_AGENT, metric="f9"),
        f"default_views.{CONFIRMED_AGENT}.metric: 'f9' is not",
    ),
    ("view not ported", _view(CONFIRMED_AGENT, metric="bert_elapsed_ms"), "'bert_elapsed_ms' is not ported"),
    ("view not for agent", _view(CONFIRMED_AGENT, metric=FOREIGN_METRIC), "does not apply to"),
    (
        "view without direction",
        _view(CONFIRMED_AGENT, metric=DIRECTIONLESS_METRIC),
        "has no direction (none)",
    ),
    ("guard without direction", _view(GUARDED_AGENT, guard=DIRECTIONLESS_METRIC), ".guard: "),
    ("guard not for agent", _view(GUARDED_AGENT, guard=FOREIGN_METRIC), "does not apply to"),
    ("guard is the metric", _view(CONFIRMED_AGENT, guard=CONFIRMED_METRIC), "is the default metric itself"),
    ("guard confirmed alone", _view(UNGUARDED_AGENT, guard_confirmed=True), "there is no guard to confirm"),
    ("guard confirmed not bool", _view(GUARDED_AGENT, guard_confirmed="no"), "guard_confirmed: must be true"),
    ("layers missing", _view(LABELLED_AGENT, label_layers=_DELETE), "label_layers: missing"),
    ("layers empty", _view(LABELLED_AGENT, label_layers=[]), "needs labels; name the layers"),
    (
        "unknown layer",
        _view(LABELLED_AGENT, label_layers=["human"]),
        "label_layers[0]: 'human' is not one of",
    ),
    ("layer twice", _view(LABELLED_AGENT, label_layers=["hub", "hub"]), "lists a layer twice"),
    ("derived with others", _view(LABELLED_AGENT, label_layers=["derived_gold", "hub"]), "list it alone"),
    ("layers for no labels", _view(UNLABELLED_AGENT, label_layers=["hub"]), "needs no labels"),
    ("null without reason", _view(NO_METRIC_AGENT, reason=None), f"default_views.{NO_METRIC_AGENT}.reason"),
    (
        "null with guard",
        _view(NO_METRIC_AGENT, guard=CONFIRMED_METRIC),
        f"default_views.{NO_METRIC_AGENT}.guard",
    ),
    ("null with layers", _view(NO_METRIC_AGENT, label_layers=["hub"]), "reads no labels"),
    (
        "default without pairing",
        _set("judge_verdict_accuracy", "pairing", None),
        "default_views.judge.metric: 'judge_verdict_accuracy' names no pairing",
    ),
    (
        "guard without pairing",
        _set("boss_needless_review_rate", "pairing_note", None),
        "default_views.boss.guard: 'boss_needless_review_rate' names no pairing",
    ),
    ("reason with metric", _view(CONFIRMED_AGENT, reason="because"), "only an agent with no default metric"),
    ("empty source", _view(CONFIRMED_AGENT, source=" "), f"default_views.{CONFIRMED_AGENT}.source: must not"),
    ("confirmed not bool", _view(CONFIRMED_AGENT, confirmed="yes"), "confirmed: must be true or false"),
    (
        "unknown view key",
        _view(CONFIRMED_AGENT, tier=0),
        f"default_views.{CONFIRMED_AGENT}.tier: unknown key",
    ),
    (
        "missing view key",
        _view(CONFIRMED_AGENT, source=_DELETE),
        f"default_views.{CONFIRMED_AGENT}.source: missing",
    ),
    (
        "roster not a mapping",
        lambda raw: raw.__setitem__("agent_roster", []),
        "agent_roster: must be a mapping",
    ),
    (
        "agent missing from roster",
        lambda raw: raw["agent_roster"].pop(LEGACY_AGENT),
        f"agent_roster: {LEGACY_AGENT!r} is named by a metric row or family but not listed",
    ),
    (
        "bad roster status",
        lambda raw: raw["agent_roster"].__setitem__(LIVE_AGENT, {"status": "active"}),
        f"agent_roster.{LIVE_AGENT}.status: 'active' is not one of",
    ),
    (
        "roster extra key",
        lambda raw: raw["agent_roster"].__setitem__(LIVE_AGENT, {"status": "live", "tier": 0}),
        f"agent_roster.{LIVE_AGENT}: must be a mapping with status",
    ),
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


def test_label_layers_are_the_labels_package_layers_and_derived_gold() -> None:
    """The view layers are the labels package's layer kinds, plus gold derived from them.

    The Dojo keeps its own copy (the table loads without the labels package); this pins the
    copy to the source. ``derived_gold`` is no labels-package word: there ``derived`` is an
    interim truth status, which a decision agent's derived gold is not.
    """
    from mailroom_dojo.measures import LABEL_LAYERS

    labels_models = pytest.importorskip("mailroom_labels.models")
    labels_layers = pytest.importorskip("mailroom_labels.layers")
    assert (*labels_models.LAYERS, "derived_gold") == LABEL_LAYERS
    assert tuple(labels_layers.CURRENT_LAYER_SCHEMA) == labels_models.LAYERS
    vocabulary = {
        *labels_models.LAYERS,
        *labels_models.STATUS_WORDS,
        *labels_models.RANKS,
        *labels_layers.INTERIM_STATUSES,
    }
    assert "derived_gold" not in vocabulary
    used = {layer for view in load_metric_table().default_views.values() for layer in view.label_layers}
    assert used <= set(LABEL_LAYERS) and "derived_gold" in used


def test_an_unstated_ground_truth_is_unknown_not_label_free() -> None:
    """``ground_truth: null`` (the old registry left it unset) may need labels: never assume not."""
    raw = _raw()
    _set("f1_macro", "ground_truth", None)(raw)
    table = load_metric_table(raw)
    assert table.get("f1_macro").ground_truth is None
    assert table.default_view("sorter").label_layers  # a null metric may name its layers
    with pytest.raises(MeasureError, match="ground truth is not stated"):
        table.check(_measure("f1_macro", label_status="none"))
    assert table.check(_measure("f1_macro")).id == "f1_macro"
    _view("sorter", label_layers=[])(raw)
    assert load_metric_table(raw).default_view("sorter").label_layers == ()


def test_every_kept_number_states_its_ground_truth() -> None:
    """A kept numeric row says whether it needs labels; null stays only where no Measure is filed."""
    table = load_metric_table()
    unstated = [
        row.id
        for row in table.rows
        if row.status != "not_ported" and row.numeric and row.ground_truth is None
    ]
    assert unstated == []
    assert table.get("per_field_scores").ground_truth == "required"
    assert table.check(_measure("total_tokens", value=120, label_status="none")).id == "total_tokens"


def test_dashboard_queries_leave_out_dropped_rows_unless_asked() -> None:
    """A not_ported headline (bert_elapsed_ms, dropped with the BERT fast path) is no dashboard number."""
    table = load_metric_table()
    assert table.get("bert_elapsed_ms").headline and table.get("bert_elapsed_ms").status == "not_ported"
    assert all(row.status != "not_ported" for row in table.headline_rows())
    assert "bert_elapsed_ms" not in {row.id for row in table.rows_for_agent("intake")}
    assert "bert_elapsed_ms" not in table.headlines_by_agent().get("intake", ())
    assert all(
        table.get(metric).status != "not_ported"
        for ids in table.headlines_by_agent().values()
        for metric in ids
    )
    assert "bert_elapsed_ms" in {row.id for row in table.headline_rows(include_not_ported=True)}
    assert "bert_elapsed_ms" in {row.id for row in table.rows_for_agent("intake", include_not_ported=True)}
    assert table.headlines_by_agent(include_not_ported=True)["intake"] == ("bert_elapsed_ms",)


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
        (lambda: _measure("reviewer_accept_rule_violations", value=2.5), "a count is a whole number"),
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
        (
            lambda: _measure("log_chains_verified", value=0.5, label_status="none"),
            "a boolean value is 0 or 1",
        ),
    ],
    ids=[
        "rate above 1",
        "fractional count",
        "fractional former int",
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
        "boolean not 0 or 1",
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
    mean_of_counts = _raw()
    _set("prompt_tokens", "aggregation", "mean")(mean_of_counts)
    assert (
        load_metric_table(mean_of_counts).check(_measure("prompt_tokens", value=1.5, label_status="none")).id
        == "prompt_tokens"
    )
    assert table.check(_measure("accuracy", value=0.5, ci=_ci(0.5, 1.0))).id == "accuracy"
    assert table.check(_measure("wer", value=1.7, ci=_ci(1.2, 2.4))).id == "wer"
    for verdict in (0, 1, 0.0, 1.0):
        verified = _measure("log_chains_verified", value=verdict, label_status="none")
        assert table.check(verified).unit == "boolean"
    with pytest.raises(UnknownMetricError):
        table.check(_measure("not_a_metric"))


VIEWS_PATH = Path(__file__).parent / "fixtures" / "measures" / "default_views.json"


def test_every_live_agent_has_one_entry_and_no_legacy_or_retired_agent_has_one() -> None:
    table = load_metric_table()
    live = set(table.agents_with_status("live"))
    assert len(live) == 16
    assert live <= set(table.default_views)
    for status in ("legacy", "retired"):
        assert not set(table.agents_with_status(status)) & set(table.default_views), status


def test_no_default_or_guard_uses_a_directionless_or_dropped_metric() -> None:
    table = load_metric_table()
    for agent, view in table.default_views.items():
        assert view.agent == agent and view.source.strip()
        if view.metric is None:
            assert view.reason and view.guard is None and not view.label_layers
            continue
        for metric in (view.metric, view.guard):
            if metric is None:
                continue
            row = table.get(metric)
            assert row.status != "not_ported" and row.applies_to(agent), (agent, metric)
            assert row.direction != "none", (agent, metric)
        assert bool(view.label_layers) == (table.get(view.metric).ground_truth in ("required", "optional"))


def test_the_roster_names_every_agent_of_every_row() -> None:
    table = load_metric_table()
    named = {agent for row in table.rows for agent in row.expanded_agents} - {"ALL"}
    assert named <= set(table.agent_roster)
    with pytest.raises(UnknownMetricError, match="unknown agent status"):
        table.agents_with_status("active")


def test_the_default_views_are_the_reviewed_ones() -> None:
    """The views, frozen in a reviewed fixture: a change is a YAML edit plus a fixture refresh."""
    import json

    table = load_metric_table()
    frozen = json.loads(VIEWS_PATH.read_text(encoding="utf-8"))
    assert frozen["status"] == "confirmed only where the owner decided; the rest are proposals"
    canonical = metric_table_canonical_form(table)
    assert canonical["default_views"] == frozen["default_views"]
    assert canonical["agent_roster"] == frozen["agent_roster"]
    assert table.default_view("not_an_agent") is None


def test_a_repeated_key_is_refused(tmp_path: Path) -> None:
    text = PACKAGED_TEXT.replace(
        "default_views:\n", f"default_views:\n  {CONFIRMED_AGENT}:\n    metric: accuracy\n", 1
    )
    path = tmp_path / "metrics.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(MetricTableError, match=f"found duplicate key '{CONFIRMED_AGENT}'"):
        load_metric_table(path)


def test_every_compared_metric_names_its_pairing_or_why_not() -> None:
    table = load_metric_table()
    compared = [
        row
        for row in table.rows
        if row.status != "not_ported" and (row.headline or "keep_rule" in row.readers)
    ]
    assert compared
    for row in compared:
        assert (row.pairing is None) != (row.pairing_note is None), row.id
    assert {row.pairing for row in table.rows} - {None} <= set(PAIRING_METHODS)


def test_every_default_and_guard_metric_names_its_pairing_or_why_not() -> None:
    """A dashboard number is compared between runs, so it says how, or why it cannot be paired."""
    table = load_metric_table()
    shown = {
        metric for view in table.default_views.values() for metric in (view.metric, view.guard) if metric
    }
    assert shown
    for metric in sorted(shown):
        row = table.get(metric)
        assert (row.pairing is None) != (row.pairing_note is None), metric


@pytest.mark.parametrize("metric", ["f1_macro", "extraction_f1", "extraction_f2"])
def test_a_whole_set_statistic_has_no_per_document_pairing(metric: str) -> None:
    """Macro F1 and the pooled field-micro F1 and F2 are not means of per-document values."""
    row = load_metric_table().get(metric)
    assert row.pairing is None
    assert row.pairing_note is not None and "D7" in row.pairing_note


def test_the_pairings_are_the_tests_d1_implements() -> None:
    """Each pairing names a D1 function by the method string its result carries."""
    paired = pytest.importorskip("mailroom_dojo.statistics.paired", reason="D1 statistics not on this branch")
    bootstrap = pytest.importorskip("mailroom_dojo.statistics.bootstrap")
    import dataclasses

    def default_method(cls: type) -> object:
        return next(field.default for field in dataclasses.fields(cls) if field.name == "method")

    assert default_method(paired.PairedDelta) == PAIRING_METHODS["paired_percentile_bootstrap"]
    assert default_method(bootstrap.DeltaInterval) == PAIRING_METHODS["two_sample_percentile_bootstrap"]


def test_an_alias_resolves_to_its_target_and_is_not_reported_itself() -> None:
    table = load_metric_table()
    alias = table.get("contracteval_false_no_related")
    assert alias.alias_of == "false_no_related_rate" and alias.status == "not_ported"
    assert table.resolve("contracteval_false_no_related").id == "false_no_related_rate"
    assert table.resolve("accuracy").id == "accuracy"
    with pytest.raises(MeasureError, match="an alias of 'false_no_related_rate'"):
        table.check(_measure("contracteval_false_no_related", value=0.1))
    assert table.check(_measure("false_no_related_rate", value=0.1)).id == "false_no_related_rate"


def test_the_unit_vocabulary_is_closed() -> None:
    """One spelling per unit: a new unit is a reviewed change to this list and table.UNITS."""
    from mailroom_dojo.measures.table import NON_NUMERIC_UNITS, UNITS

    assert set(UNITS) == {
        "float[0,1]",
        "count",
        "boolean",
        "error_rate",
        "seconds",
        "milliseconds",
        "days",
        "USD",
        "GB",
        "tokens/s",
        "req/s",
        "docs/s",
        "tag",
        "text",
        "structure",
        "per_1000",
        "docs/min",
        "ratio",
        "utility",
    }
    assert set(NON_NUMERIC_UNITS) == {"tag", "text", "structure"}
    assert {row.unit for row in load_metric_table().rows} <= set(UNITS)
    for spelling in ("usd", "int"):
        with pytest.raises(MetricTableError, match=f"'{spelling}' is not one of"):
            raw = _raw()
            _set("prompt_tokens", "unit", spelling)(raw)
            load_metric_table(raw)


def test_the_duplicate_groups_name_their_canonical_row() -> None:
    table = load_metric_table()
    pointers = {row.id: row.alias_of or row.same_as for row in table.rows if row.alias_of or row.same_as}
    assert pointers == {
        "contracteval_false_no_related": "false_no_related_rate",
        "laziness_rate": "false_no_related_rate",
        "schema_adherence": "schema_valid",
        "expected_field_presence": "field_presence",
    }
    assert "no_related_rate is wrong" not in str(table.get("contracteval_false_no_related").status_reason)
    assert "divides by all pairs" in str(table.get("contracteval_false_no_related").status_reason)
    assert "1 - audit_resolution_rate" in str(table.get("audit_disagreement_rate").status_reason)


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (
            _set("schema_adherence", "same_as", "nothing_here"),
            "(schema_adherence).same_as: 'nothing_here' is not",
        ),
        (_set("schema_adherence", "same_as", "schema_adherence"), "is not another metric"),
        (_set("schema_valid", "same_as", "laziness_rate"), "is itself a duplicate"),
        (_set("laziness_rate", "same_as", "f1_macro"), "direction 'lower' differs from"),
        (_set("expected_field_presence", "same_as", "schema_valid"), "an alias already names its target"),
        (
            _set("schema_adherence", "same_as", "ml_intake_doc_type_accuracy"),
            "(schema_adherence).same_as: 'ml_intake_doc_type_accuracy' is not ported",
        ),
        (
            _sequence(
                _set("contracteval_false_no_related", "direction", "higher"),
                _set("contracteval_false_no_related", "alias_of", "schema_adherence"),
            ),
            "(contracteval_false_no_related).alias_of: 'schema_adherence' is itself a duplicate",
        ),
    ],
    ids=[
        "unknown",
        "itself",
        "chain",
        "different direction or unit",
        "alias and same_as",
        "same_as a dropped row",
        "alias of a duplicate",
    ],
)
def test_a_broken_same_as_is_refused(edit: Callable[[dict[str, Any]], None], expected: str) -> None:
    raw = _raw()
    edit(raw)
    with pytest.raises(MetricTableError, match=re.escape(expected)):
        load_metric_table(raw)


def test_the_archive_gate_reads_judge_error_rate() -> None:
    """The sandbox gate fails a slice with any judge_error document, so the row has a reader."""
    row = load_metric_table().get("judge_error_rate")
    assert row.status == "planned" and row.layer == "D6"
    assert "archive_gate" in row.readers and row.direction == "lower"


def test_utility_may_be_negative_and_nothing_else_may() -> None:
    table = load_metric_table()
    assert table.check(_measure("document_utility", value=-0.101)).unit == "utility"
    with pytest.raises(MeasureError, match="is negative"):
        table.check(_measure("cost_estimate_to_billed_ratio", value=-0.5, label_status="none"))


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (_set("review_rate", "metric_id", "registry:review_rate"), "(review_rate).metric_id: a design row"),
        (_set("f1_macro", "metric_id", "dojo:f1_macro"), "(f1_macro).metric_id: a design row"),
        (_set("f1_macro", "metric_id", "dojo:other"), "(f1_macro).metric_id: a design row"),
    ],
    ids=["design row with a registry id", "registry row with a dojo id", "foreign dojo id"],
)
def test_a_design_id_is_its_own_and_only_a_design_rows(
    edit: Callable[[dict[str, Any]], None], expected: str
) -> None:
    raw = _raw()
    edit(raw)
    with pytest.raises(MetricTableError, match=re.escape(expected)):
        load_metric_table(raw)
