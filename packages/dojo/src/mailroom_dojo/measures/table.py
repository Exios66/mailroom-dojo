"""The metric table: one flat, versioned row per metric, shipped as ``measures/metrics.yaml``.

:func:`load_metric_table` reads the table (the packaged ``metrics.yaml`` by default, or
a path or an already parsed mapping), checks every key and value, and returns an
immutable :class:`MetricTable`. A problem raises :class:`MetricTableError` naming the
YAML path, for example ``metrics[3].direction: 'up' is not one of higher, lower, none``.

The table replaces the old registry's tiers, bundles and profiles: the dashboard view
is a query (:meth:`MetricTable.headline_rows`, :meth:`MetricTable.rows_for_agent`), over
kept rows unless ``include_not_ported=True``.
:attr:`MetricTable.content_hash` is the SHA-256 of the table's canonical JSON form
(sorted keys, no whitespace, defaults filled in), so every scorecard can say which
table defined its numbers. Mapping key order and YAML comments never change it; row
order and list order do.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .measure import Measure, MeasureError

__all__ = [
    "AGENT_STATUSES",
    "AGGREGATIONS",
    "AgentEntry",
    "DefaultView",
    "DIRECTIONS",
    "GROUND_TRUTH",
    "LABEL_LAYERS",
    "LAYERS",
    "NON_NUMERIC_UNITS",
    "ORIGINS",
    "PAIRINGS",
    "PAIRING_METHODS",
    "READERS",
    "STATUSES",
    "TABLE_FORMAT",
    "UNITS",
    "MetricRow",
    "MetricTable",
    "MetricTableError",
    "UnknownMetricError",
    "load_metric_table",
    "metric_table_canonical_form",
]

#: The ``table_format`` this loader reads.
TABLE_FORMAT = 1

#: Units: a closed vocabulary. The old registry's units, normalised: ``usd`` is written
#: ``USD`` and ``int`` is ``count``; ``structure`` marks a row whose value is a structure
#: (a matrix, a per-class table, an interval), not one number. ``per_1000`` is a count per
#: 1,000 documents, ``ratio`` a non-negative quotient, ``utility`` a signed per-document
#: utility (the only unit that may be negative).
UNITS: tuple[str, ...] = (
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
)
#: Units whose rows are not one number (a label, text or a structure): no ``Measure`` carries them.
NON_NUMERIC_UNITS: tuple[str, ...] = ("tag", "text", "structure")
DIRECTIONS: tuple[str, ...] = ("higher", "lower", "none")
#: How per-document values roll up to the run (the old registry's three values).
AGGREGATIONS: tuple[str, ...] = ("mean", "sum", "none")
#: The old ``ALLOWED_GROUND_TRUTH`` without the empty string (written as null here).
GROUND_TRUTH: tuple[str, ...] = ("required", "optional", "structural", "none")
#: Existing Dojo modules, then the build chunks of the port design (section 6).
LAYERS: tuple[str, ...] = ("classification", "field_scoring", "D1", "D3b", "D4", "D5", "D6", "D7")
_EXISTING_LAYERS = frozenset({"classification", "field_scoring"})
STATUSES: tuple[str, ...] = ("implemented", "planned", "not_ported")
#: Where an agent stands. ``live`` is an agent of the running pipeline (llm-mailroom
#: ``src/config/taxonomy.yaml``, ``agents:``); ``legacy`` and ``retired`` agents keep their
#: rows for golden parity and history but get no default view; the rest name what the
#: entry is when it is not a model agent.
AGENT_STATUSES: tuple[str, ...] = (
    "live",
    "legacy",
    "retired",
    "procedural",
    "human",
    "comparison",
    "graph_node",
)
#: The label layers a default view reads its gold from: the labels package's layer kinds
#: (``mailroom_labels.models.LAYERS``: the dataset ground truth ``hub``, the generated
#: ``interim`` and ``near_gold`` labels, and the review desk's ``review`` gold), then
#: ``derived_gold``, computed deterministically from those layers' class and field labels
#: for the decision agents (the row's ``status_reason`` gives the rule). A copy, so the
#: table loads without the labels package; a test pins it to the source. Not ``derived``:
#: the labels package uses that word for an interim truth status.
LABEL_LAYERS: tuple[str, ...] = ("hub", "interim", "near_gold", "review", "derived_gold")
#: The layer computed from the others, listed alone.
_DERIVED_LAYER = "derived_gold"
#: Where a row comes from: the old registry, the old golden baseline, or a need the master
#: design names that no old metric meets (``design``, metric id ``dojo:<id>``).
ORIGINS: tuple[str, ...] = ("registry", "golden", "design")
#: How two runs' values of a metric are compared, each a test D1 implements
#: (``mailroom_dojo.statistics``), with the ``method`` string D1 stamps on its result:
#: ``paired_percentile_bootstrap`` is ``paired_bootstrap_delta`` (per-document values of
#: two runs over the same documents, paired by document id; the design's "Comparing
#: runs"); ``two_sample_percentile_bootstrap`` is ``delta_significance`` (two unpaired
#: samples; the old ``compare_serving`` on latencies).
PAIRING_METHODS: Mapping[str, str] = MappingProxyType(
    {
        "paired_percentile_bootstrap": "paired-percentile-bootstrap",
        "two_sample_percentile_bootstrap": "two-sample-percentile-bootstrap",
    }
)
PAIRINGS: tuple[str, ...] = tuple(PAIRING_METHODS)

#: The gates, reports and projections that read metrics, as the master design and the port
#: design name them (``metrics.yaml`` says where). A kept metric names at least one.
READERS: tuple[str, ...] = (
    "golden_parity_gate",
    "archive_gate",
    "headline_view",
    "keep_rule",
    "specialist_report",
    "classification_report",
    "merger_report",
    "behaviour_report",
    "confidence_report",
    "run_report",
    "cost_truth_report",
    "run_comparison",
    "cost_projection",
    "set_a_report",
    "set_c_report",
    "set_e_report",
    "log_health_report",
    "gold_generation",
)

_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
_AGENT = re.compile(r"^[a-z][a-z0-9_]*$")
_FAMILY = re.compile(r"^[A-Z][A-Z_]*$")
_ALL_AGENTS = "ALL"

_REQUIRED_ROW_KEYS = (
    "id",
    "description",
    "unit",
    "direction",
    "aggregation",
    "agents",
    "ground_truth",
    "headline",
    "metric_id",
    "layer",
    "status",
    "origin",
    "citation",
    "inclusion",
)
_ROW_DEFAULTS: Mapping[str, Any] = MappingProxyType(
    {
        "readers": (),
        "producer": None,
        "status_reason": None,
        "cost": False,
        "pairing": None,
        "pairing_note": None,
        "alias_of": None,
        "same_as": None,
        "noise_floor": None,
        "old_source": None,
        "notes": "",
    }
)
_TOP_KEYS = frozenset(
    {"table_format", "version", "source", "agent_families", "agent_roster", "default_views", "metrics"}
)
_VIEW_KEYS = frozenset(
    {"metric", "confirmed", "source", "reason", "guard", "guard_confirmed", "label_layers"}
)
_VIEW_REQUIRED = ("metric", "confirmed", "source")


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a mapping with the same key twice (PyYAML keeps the last)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:  # an unhashable key: the base constructor reports it
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)  # type: ignore[no-any-return]


def _parse(text: str) -> Any:
    """Parse with PyYAML's safe loader, refusing duplicate keys."""
    return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - always a safe loader


class MetricTableError(ValueError):
    """A metric table that does not load; the message names the YAML path."""


class UnknownMetricError(KeyError):
    """A metric id that is not in the table."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


@dataclass(frozen=True)
class MetricRow:
    """One metric. ``agents`` is as written; ``expanded_agents`` has the families expanded."""

    id: str
    description: str
    unit: str
    direction: str
    aggregation: str
    agents: tuple[str, ...]
    expanded_agents: tuple[str, ...]
    ground_truth: str | None
    headline: bool
    metric_id: str
    layer: str | None
    status: str
    readers: tuple[str, ...]
    origin: str
    citation: str
    inclusion: str
    producer: str | None
    status_reason: str | None
    cost: bool
    pairing: str | None
    pairing_note: str | None
    alias_of: str | None
    same_as: str | None
    noise_floor: float | None
    old_source: str | None
    notes: str

    def applies_to(self, agent: str) -> bool:
        """Whether the metric targets ``agent`` (``ALL`` targets every agent)."""
        return _ALL_AGENTS in self.expanded_agents or agent in self.expanded_agents

    @property
    def numeric(self) -> bool:
        """Whether a value of this metric is a number (tags and text are not)."""
        return self.unit not in NON_NUMERIC_UNITS


@dataclass(frozen=True)
class AgentEntry:
    """An agent the table names, its status (:data:`AGENT_STATUSES`) and an optional note."""

    agent: str
    status: str
    note: str | None


@dataclass(frozen=True)
class DefaultView:
    """The one number an agent's dashboard shows by default (the old tier 0, "ONE number per agent").

    ``metric`` is ``None`` for an agent with no default number, and ``reason`` says why.
    ``confirmed`` is true only where the owner fixed the choice; ``source`` says where the
    choice comes from. ``guard`` is the metric shown beside it so the default cannot be
    gamed alone, with its own ``guard_confirmed``. ``label_layers`` names the label layers
    (:data:`LABEL_LAYERS`) the gold comes from; it is empty only for a metric that needs no
    labels.
    """

    agent: str
    metric: str | None
    confirmed: bool
    source: str
    reason: str | None
    guard: str | None
    guard_confirmed: bool
    label_layers: tuple[str, ...]


@dataclass(frozen=True)
class MetricTable:
    """The loaded, validated table. Immutable; identified by ``content_hash``."""

    version: int
    source: Mapping[str, str]
    agent_families: Mapping[str, tuple[str, ...]]
    agent_roster: Mapping[str, AgentEntry]
    rows: tuple[MetricRow, ...]
    default_views: Mapping[str, DefaultView]
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_id", MappingProxyType({row.id: row for row in self.rows}))

    @property
    def ids(self) -> tuple[str, ...]:
        """Every metric id, in table order."""
        return tuple(row.id for row in self.rows)

    def __contains__(self, metric: object) -> bool:
        return metric in self._by_id  # type: ignore[attr-defined]

    def get(self, metric: str) -> MetricRow:
        """The row for ``metric``; an unknown id raises :class:`UnknownMetricError`."""
        try:
            row: MetricRow = self._by_id[metric]  # type: ignore[attr-defined]
        except KeyError:
            raise UnknownMetricError(f"unknown metric {metric!r}; it is not in the metric table") from None
        return row

    def default_view(self, agent: str) -> DefaultView | None:
        """The agent's default view, or ``None`` when the table names none."""
        return self.default_views.get(agent)

    def agents_with_status(self, status: str) -> tuple[str, ...]:
        """The roster's agents with ``status`` (an unknown status raises), in roster order."""
        if status not in AGENT_STATUSES:
            raise UnknownMetricError(f"unknown agent status {status!r}; known: {', '.join(AGENT_STATUSES)}")
        return tuple(name for name, entry in self.agent_roster.items() if entry.status == status)

    def resolve(self, metric: str) -> MetricRow:
        """The row a metric id is reported under: the target of an alias, else its own row."""
        row = self.get(metric)
        return row if row.alias_of is None else self.get(row.alias_of)

    def headline_rows(self, *, include_not_ported: bool = False) -> tuple[MetricRow, ...]:
        """The headline metrics, in table order; ``not_ported`` rows only when asked for."""
        return tuple(
            row for row in self.rows if row.headline and (include_not_ported or row.status != "not_ported")
        )

    def rows_for_agent(
        self, agent: str, *, headline_only: bool = False, include_not_ported: bool = False
    ) -> tuple[MetricRow, ...]:
        """The metrics that apply to ``agent`` (rows for ``ALL`` included), kept rows unless asked."""
        return tuple(
            row
            for row in self.rows
            if row.applies_to(agent)
            and (row.headline or not headline_only)
            and (include_not_ported or row.status != "not_ported")
        )

    def rows_for_reader(self, reader: str) -> tuple[MetricRow, ...]:
        """The metrics a gate, report or projection reads (an unknown reader raises)."""
        if reader not in READERS:
            raise UnknownMetricError(f"unknown reader {reader!r}; known: {', '.join(READERS)}")
        return tuple(row for row in self.rows if reader in row.readers)

    def headlines_by_agent(self, *, include_not_ported: bool = False) -> Mapping[str, tuple[str, ...]]:
        """Each named agent and the headline ids that name it (``ALL`` rows under ``ALL``).

        Kept rows only, unless ``include_not_ported``: a dropped headline is no dashboard number.
        """
        out: dict[str, list[str]] = {}
        for row in self.headline_rows(include_not_ported=include_not_ported):
            for agent in row.expanded_agents:
                out.setdefault(agent, []).append(row.id)
        return MappingProxyType({agent: tuple(ids) for agent, ids in sorted(out.items())})

    def check(self, measure: Measure) -> MetricRow:
        """Check a measure against its metric's row and return the row.

        Raises :class:`UnknownMetricError` for an id outside the table and
        :class:`MeasureError` when the measure breaks the row: the metric is not ported
        or is not a number (a tag, text or structure); the value is outside the unit's
        range (``float[0,1]`` and ``boolean`` in ``[0, 1]``, ``utility`` any finite number,
        every other unit non-negative); a
        ``count`` value is not a whole number (unless the row's aggregation
        is ``mean``: a mean of counts need not be whole); a ``boolean`` is not 0 or 1;
        the interval leaves the unit's range or does not contain the value; a metric
        that needs labels claims ``label_status="none"``; or the cost basis does not
        match the row (a cost needs a basis; any other metric has ``none``).
        """
        row = self.get(measure.method)
        if row.alias_of is not None:
            raise MeasureError(f"{row.id}: an alias of {row.alias_of!r}; report it under that id")
        if row.status == "not_ported":
            raise MeasureError(f"{row.id}: not ported ({row.status_reason})")
        if not row.numeric:
            raise MeasureError(
                f"{row.id}: unit {row.unit!r} is not a number; it is not reported as a Measure"
            )
        value = measure.value
        if value is not None:
            _check_in_unit(row, value, "value")
            if row.unit == "boolean" and value not in (0, 1):
                raise MeasureError(f"{row.id}: a boolean value is 0 or 1, got {value!r}")
            if row.unit in _WHOLE_UNITS and row.aggregation != "mean" and not float(value).is_integer():
                raise MeasureError(f"{row.id}: a {row.unit} is a whole number, got {value!r}")
        if measure.ci is not None and value is not None:
            _check_in_unit(row, measure.ci.low, "ci.low")
            _check_in_unit(row, measure.ci.high, "ci.high")
            if not measure.ci.low <= value <= measure.ci.high:
                raise MeasureError(
                    f"{row.id}: the interval [{measure.ci.low!r}, {measure.ci.high!r}] "
                    f"does not contain the value {value!r}"
                )
        if row.ground_truth == "required" and measure.label_status == "none":
            raise MeasureError(f"{row.id}: needs labels, so label_status cannot be 'none'")
        if row.cost and measure.cost_basis == "none":
            raise MeasureError(f"{row.id}: a cost needs a cost basis")
        if not row.cost and measure.cost_basis != "none":
            raise MeasureError(
                f"{row.id}: not a cost, so cost_basis must be 'none' (got {measure.cost_basis!r})"
            )
        return row


#: Units whose values are whole numbers.
_WHOLE_UNITS = frozenset({"count"})
#: Units whose values may be negative.
_SIGNED_UNITS = frozenset({"utility"})
#: Units bounded by ``[0, 1]``; every other numeric unit is non-negative.
_UNIT_INTERVAL = frozenset({"float[0,1]", "boolean"})


def _check_in_unit(row: MetricRow, number: float, what: str) -> None:
    if row.unit in _UNIT_INTERVAL and not 0.0 <= number <= 1.0:
        raise MeasureError(f"{row.id}: {what} {number!r} is outside [0, 1] for unit {row.unit!r}")
    if number < 0 and row.unit not in _SIGNED_UNITS:
        raise MeasureError(f"{row.id}: {what} {number!r} is negative for unit {row.unit!r}")


def _fail(path: str, message: str) -> MetricTableError:
    return MetricTableError(f"{path}: {message}")


def _string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail(path, f"must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise _fail(path, "must not be empty")
    return value


def _choice(value: Any, choices: tuple[str, ...], path: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in choices:
        allowed = ", ".join(choices) + (" or null" if nullable else "")
        raise _fail(path, f"{value!r} is not one of {allowed}")
    return value


def _families(raw: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, Mapping):
        raise _fail("agent_families", "must be a mapping of family name to agent list")
    out: dict[str, tuple[str, ...]] = {}
    for name, members in raw.items():
        path = f"agent_families.{name}"
        if not isinstance(name, str) or not _FAMILY.match(name) or name == _ALL_AGENTS:
            raise _fail(path, "a family name is upper case letters and underscores, and not ALL")
        if not isinstance(members, list) or not members:
            raise _fail(path, "must be a non-empty list of agent names")
        for index, member in enumerate(members):
            if not isinstance(member, str) or not _AGENT.match(member):
                raise _fail(f"{path}[{index}]", f"{member!r} is not an agent name")
        if len(set(members)) != len(members):
            raise _fail(path, "lists an agent twice")
        out[name] = tuple(members)
    return out


def _agents(
    raw: Any, families: Mapping[str, tuple[str, ...]], path: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(raw, list) or not raw:
        raise _fail(path, "must be a non-empty list of agents, families or ALL")
    written: list[str] = []
    expanded: list[str] = []
    for index, token in enumerate(raw):
        item_path = f"{path}[{index}]"
        if not isinstance(token, str):
            raise _fail(item_path, f"must be a string, got {type(token).__name__}")
        if token == _ALL_AGENTS:
            members: tuple[str, ...] = (token,)
        elif _FAMILY.match(token):
            if token not in families:
                raise _fail(item_path, f"unknown agent family {token!r}")
            members = families[token]
        elif _AGENT.match(token):
            members = (token,)
        else:
            raise _fail(item_path, f"{token!r} is not an agent name, a family or ALL")
        if token in written:
            raise _fail(item_path, f"{token!r} is listed twice")
        written.append(token)
        expanded.extend(member for member in members if member not in expanded)
    return tuple(written), tuple(expanded)


def _readers(raw: Any, path: str) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        raise _fail(path, "must be a list of readers")
    for index, reader in enumerate(raw):
        _choice(reader, READERS, f"{path}[{index}]")
    if len(set(raw)) != len(raw):
        raise _fail(path, "lists a reader twice")
    return tuple(raw)


def _row(raw: Any, index: int, families: Mapping[str, tuple[str, ...]]) -> MetricRow:
    path = f"metrics[{index}]"
    if not isinstance(raw, Mapping):
        raise _fail(path, "must be a mapping")
    unknown = sorted(set(raw) - set(_REQUIRED_ROW_KEYS) - set(_ROW_DEFAULTS))
    if unknown:
        raise _fail(f"{path}.{unknown[0]}", "unknown key")
    missing = [key for key in _REQUIRED_ROW_KEYS if key not in raw]
    if missing:
        raise _fail(f"{path}.{missing[0]}", "missing")
    spec = {**_ROW_DEFAULTS, **raw}
    metric = _string(spec["id"], f"{path}.id")
    if not _ID.match(metric):
        raise _fail(f"{path}.id", f"{metric!r} is not a metric id (lower case, digits, '_' and '-')")
    path = f"metrics[{index}] ({metric})"
    agents, expanded = _agents(spec["agents"], families, f"{path}.agents")
    if not isinstance(spec["headline"], bool):
        raise _fail(f"{path}.headline", "must be true or false")
    if not isinstance(spec["cost"], bool):
        raise _fail(f"{path}.cost", "must be true or false")
    layer = _choice(spec["layer"], LAYERS, f"{path}.layer", nullable=True)
    status = _choice(spec["status"], STATUSES, f"{path}.status")
    producer = None if spec["producer"] is None else _string(spec["producer"], f"{path}.producer")
    reason = (
        None if spec["status_reason"] is None else _string(spec["status_reason"], f"{path}.status_reason")
    )
    if status == "not_ported":
        if reason is None:
            raise _fail(f"{path}.status_reason", "a not_ported metric needs the reason")
        if layer is not None or producer is not None:
            raise _fail(f"{path}.layer", "a not_ported metric has no layer and no producer")
    elif layer is None:
        raise _fail(f"{path}.layer", f"a {status} metric needs a layer")
    if status == "implemented" and (producer is None or layer not in _EXISTING_LAYERS):
        raise _fail(f"{path}.producer", "an implemented metric names its producer in an existing module")
    if status == "planned" and (producer is not None or layer in _EXISTING_LAYERS):
        raise _fail(
            f"{path}.layer", "a planned metric names the build chunk that will produce it, and no producer"
        )
    readers = _readers(spec["readers"], f"{path}.readers")
    if status == "not_ported" and readers:
        raise _fail(f"{path}.readers", "a not_ported metric has no reader")
    if status != "not_ported" and not readers:
        raise _fail(
            f"{path}.readers", f"a {status} metric names the gate, report or projection that reads it"
        )
    direction = str(_choice(spec["direction"], DIRECTIONS, f"{path}.direction"))
    if "keep_rule" in readers and direction == "none":
        raise _fail(f"{path}.direction", "a metric the keep rule reads needs a direction (higher or lower)")
    pairing = _choice(spec["pairing"], PAIRINGS, f"{path}.pairing", nullable=True)
    pairing_note = (
        None if spec["pairing_note"] is None else _string(spec["pairing_note"], f"{path}.pairing_note")
    )
    if status == "not_ported" and (pairing is not None or pairing_note is not None):
        raise _fail(f"{path}.pairing", "a not_ported metric has no pairing")
    if pairing is not None and pairing_note is not None:
        raise _fail(f"{path}.pairing_note", "a note says why there is no pairing; this row has one")
    compared = status != "not_ported" and (spec["headline"] or "keep_rule" in readers)
    if compared and pairing is None and pairing_note is None:
        raise _fail(
            f"{path}.pairing",
            "a kept headline or keep-rule metric names its pairing, or a pairing_note says why it has none",
        )
    alias_of = None if spec["alias_of"] is None else _string(spec["alias_of"], f"{path}.alias_of")
    if alias_of is not None and status != "not_ported":
        raise _fail(f"{path}.alias_of", "an alias is reported under its target's id, so it is not_ported")
    same_as = None if spec["same_as"] is None else _string(spec["same_as"], f"{path}.same_as")
    if same_as is not None and alias_of is not None:
        raise _fail(f"{path}.same_as", "an alias already names its target; same_as is for a second row")
    noise_floor = spec["noise_floor"]
    if noise_floor is not None and (
        isinstance(noise_floor, bool)
        or not isinstance(noise_floor, int | float)
        or not math.isfinite(noise_floor)
        or noise_floor < 0
    ):
        raise _fail(f"{path}.noise_floor", "must be null or a non-negative number")
    metric_id = _string(spec["metric_id"], f"{path}.metric_id")
    if metric_id.startswith("registry:") and metric_id != f"registry:{metric}":
        raise _fail(f"{path}.metric_id", f"a registry id names its own row: registry:{metric}")
    if (spec["origin"] == "design") != (metric_id == f"dojo:{metric}") or (
        metric_id.startswith("dojo:") and metric_id != f"dojo:{metric}"
    ):
        raise _fail(f"{path}.metric_id", f"a design row, and only a design row, has the id dojo:{metric}")
    return MetricRow(
        id=metric,
        description=_string(spec["description"], f"{path}.description"),
        unit=str(_choice(spec["unit"], UNITS, f"{path}.unit")),
        direction=direction,
        aggregation=str(_choice(spec["aggregation"], AGGREGATIONS, f"{path}.aggregation")),
        agents=agents,
        expanded_agents=expanded,
        ground_truth=_choice(spec["ground_truth"], GROUND_TRUTH, f"{path}.ground_truth", nullable=True),
        headline=spec["headline"],
        metric_id=metric_id,
        layer=layer,
        status=str(status),
        readers=readers,
        origin=str(_choice(spec["origin"], ORIGINS, f"{path}.origin")),
        citation=_string(spec["citation"], f"{path}.citation", allow_empty=True),
        inclusion=_string(spec["inclusion"], f"{path}.inclusion", allow_empty=True),
        producer=producer,
        status_reason=reason,
        cost=spec["cost"],
        pairing=pairing,
        pairing_note=pairing_note,
        alias_of=alias_of,
        same_as=same_as,
        noise_floor=None if noise_floor is None else float(noise_floor),
        old_source=None if spec["old_source"] is None else _string(spec["old_source"], f"{path}.old_source"),
        notes=_string(spec["notes"], f"{path}.notes", allow_empty=True),
    )


def _source(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise _fail("source", "must be a mapping of strings")
    return {str(key): _string(value, f"source.{key}") for key, value in raw.items()}


def _row_canonical(row: MetricRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "description": row.description,
        "unit": row.unit,
        "direction": row.direction,
        "aggregation": row.aggregation,
        "agents": list(row.agents),
        "ground_truth": row.ground_truth,
        "headline": row.headline,
        "metric_id": row.metric_id,
        "layer": row.layer,
        "status": row.status,
        "readers": list(row.readers),
        "origin": row.origin,
        "citation": row.citation,
        "inclusion": row.inclusion,
        "producer": row.producer,
        "status_reason": row.status_reason,
        "cost": row.cost,
        "pairing": row.pairing,
        "pairing_note": row.pairing_note,
        "alias_of": row.alias_of,
        "same_as": row.same_as,
        "noise_floor": row.noise_floor,
        "old_source": row.old_source,
        "notes": row.notes,
    }


def metric_table_canonical_form(table: MetricTable) -> dict[str, Any]:
    """The table as plain JSON data (defaults filled in), the input of its content hash."""
    return {
        "table_format": TABLE_FORMAT,
        "version": table.version,
        "source": dict(sorted(table.source.items())),
        "agent_families": {name: list(members) for name, members in sorted(table.agent_families.items())},
        "agent_roster": {
            name: {"status": entry.status, "note": entry.note}
            for name, entry in sorted(table.agent_roster.items())
        },
        "default_views": {
            agent: {
                "metric": view.metric,
                "confirmed": view.confirmed,
                "source": view.source,
                "reason": view.reason,
                "guard": view.guard,
                "guard_confirmed": view.guard_confirmed,
                "label_layers": list(view.label_layers),
            }
            for agent, view in sorted(table.default_views.items())
        },
        "metrics": [_row_canonical(row) for row in table.rows],
    }


def _hash(canonical: Mapping[str, Any]) -> str:
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _view_metric(metric: Any, agent: str, by_id: Mapping[str, MetricRow], path: str) -> MetricRow:
    if not isinstance(metric, str) or metric not in by_id:
        raise _fail(path, f"{metric!r} is not a metric in the table")
    row = by_id[metric]
    if row.status == "not_ported":
        raise _fail(path, f"{metric!r} is not ported")
    if not row.applies_to(agent):
        raise _fail(path, f"{metric!r} does not apply to {agent!r}")
    return row


def _check_aliases(rows: tuple[MetricRow, ...]) -> None:
    """An alias names a kept row that is no alias, with the same unit, direction and aggregation.

    ``same_as`` marks a duplicate that keeps its own row (it may be computed at another
    level): it names another row that is itself canonical (no alias, no ``same_as``), with
    the same unit and direction. Both point at a kept row: never at a ``not_ported`` row,
    and never at a row that is itself a duplicate.
    """
    by_id = {row.id: row for row in rows}
    for index, row in enumerate(rows):
        if row.same_as is None:
            continue
        path = f"metrics[{index}] ({row.id}).same_as"
        target = by_id.get(row.same_as)
        if target is None or target.id == row.id:
            raise _fail(path, f"{row.same_as!r} is not another metric in the table")
        if target.alias_of is not None or target.same_as is not None:
            raise _fail(path, f"{target.id!r} is itself a duplicate; name the canonical row")
        if target.status == "not_ported":
            raise _fail(path, f"{target.id!r} is not ported; a duplicate names a kept canonical row")
        for key in ("unit", "direction"):
            if getattr(row, key) != getattr(target, key):
                raise _fail(
                    path, f"{key} {getattr(row, key)!r} differs from {target.id!r}'s {getattr(target, key)!r}"
                )
    for index, row in enumerate(rows):
        if row.alias_of is None:
            continue
        path = f"metrics[{index}] ({row.id}).alias_of"
        target = by_id.get(row.alias_of)
        if target is None or target.id == row.id:
            raise _fail(path, f"{row.alias_of!r} is not another metric in the table")
        if target.alias_of is not None or target.status == "not_ported":
            raise _fail(path, f"{target.id!r} is an alias or not ported; an alias names a kept metric")
        if target.same_as is not None:
            raise _fail(path, f"{target.id!r} is itself a duplicate of {target.same_as!r}; name that row")
        for key in ("unit", "direction", "aggregation"):
            if getattr(row, key) != getattr(target, key):
                raise _fail(
                    path, f"{key} {getattr(row, key)!r} differs from {target.id!r}'s {getattr(target, key)!r}"
                )


def _roster(
    raw: Any, rows: tuple[MetricRow, ...], families: Mapping[str, tuple[str, ...]]
) -> dict[str, AgentEntry]:
    """Validate ``agent_roster``: every agent a row or a family names is listed, with a status."""
    if not isinstance(raw, Mapping):
        raise _fail("agent_roster", "must be a mapping of agent to {status, note?}")
    out: dict[str, AgentEntry] = {}
    for agent, spec in raw.items():
        path = f"agent_roster.{agent}"
        if not isinstance(agent, str) or not _AGENT.match(agent):
            raise _fail(path, f"{agent!r} is not an agent name")
        if not isinstance(spec, Mapping) or not set(spec) <= {"status", "note"} or "status" not in spec:
            raise _fail(path, "must be a mapping with status and an optional note")
        status = str(_choice(spec["status"], AGENT_STATUSES, f"{path}.status"))
        note = None if spec.get("note") is None else _string(spec["note"], f"{path}.note")
        out[agent] = AgentEntry(agent, status, note)
    named = {agent for row in rows for agent in row.expanded_agents} | {
        agent for members in families.values() for agent in members
    }
    named.discard(_ALL_AGENTS)
    absent = sorted(named - set(out))
    if absent:
        raise _fail("agent_roster", f"{absent[0]!r} is named by a metric row or family but not listed")
    return out


def _view_guarded_metric(metric: Any, agent: str, by_id: Mapping[str, MetricRow], path: str) -> MetricRow:
    row = _view_metric(metric, agent, by_id, path)
    if row.direction == "none":
        raise _fail(path, f"{metric!r} has no direction (none); a default view or guard needs one")
    return row


def _default_views(
    raw: Any, rows: tuple[MetricRow, ...], roster: Mapping[str, AgentEntry]
) -> dict[str, DefaultView]:
    """Validate ``default_views``: one entry per live agent, none for a legacy or retired one."""
    if not isinstance(raw, Mapping):
        raise _fail("default_views", "must be a mapping of agent to its default view")
    by_id = {row.id: row for row in rows}
    views: dict[str, DefaultView] = {}
    for agent, spec in raw.items():
        path = f"default_views.{agent}"
        if not isinstance(agent, str) or agent not in roster:
            raise _fail(path, f"{agent!r} is not an agent of the roster")
        if roster[agent].status in ("legacy", "retired"):
            raise _fail(path, f"{agent!r} is {roster[agent].status}; it has no default view")
        if not isinstance(spec, Mapping):
            raise _fail(path, "must be a mapping")
        unknown = sorted(set(spec) - _VIEW_KEYS)
        if unknown:
            raise _fail(f"{path}.{unknown[0]}", "unknown key")
        missing = [key for key in _VIEW_REQUIRED if key not in spec]
        if missing:
            raise _fail(f"{path}.{missing[0]}", "missing")
        confirmed = spec["confirmed"]
        if not isinstance(confirmed, bool):
            raise _fail(f"{path}.confirmed", "must be true or false")
        guard_confirmed = spec.get("guard_confirmed", False)
        if not isinstance(guard_confirmed, bool):
            raise _fail(f"{path}.guard_confirmed", "must be true or false")
        source = _string(spec["source"], f"{path}.source")
        reason = spec.get("reason")
        guard = spec.get("guard")
        layers_raw = spec.get("label_layers")
        metric = spec["metric"]
        if metric is None:
            if reason is None:
                raise _fail(f"{path}.reason", "an agent with no default metric needs the reason")
            reason = _string(reason, f"{path}.reason")
            if guard is not None or "guard_confirmed" in spec:
                raise _fail(f"{path}.guard", "an agent with no default metric has no guard")
            if layers_raw is not None:
                raise _fail(f"{path}.label_layers", "an agent with no default metric reads no labels")
            views[agent] = DefaultView(agent, None, confirmed, source, reason, None, False, ())
            continue
        if reason is not None:
            raise _fail(f"{path}.reason", "only an agent with no default metric gives a reason")
        row = _view_guarded_metric(metric, agent, by_id, f"{path}.metric")
        if guard is None:
            if "guard_confirmed" in spec:
                raise _fail(f"{path}.guard_confirmed", "there is no guard to confirm")
        else:
            _view_guarded_metric(guard, agent, by_id, f"{path}.guard")
            if guard == metric:
                raise _fail(f"{path}.guard", f"{guard!r} is the default metric itself")
        if layers_raw is None:
            raise _fail(f"{path}.label_layers", "missing: name the label layers the gold comes from")
        if not isinstance(layers_raw, list):
            raise _fail(f"{path}.label_layers", "must be a list of label layers")
        for index, layer in enumerate(layers_raw):
            _choice(layer, LABEL_LAYERS, f"{path}.label_layers[{index}]")
        if len(set(layers_raw)) != len(layers_raw):
            raise _fail(f"{path}.label_layers", "lists a layer twice")
        needs_labels = row.ground_truth in ("required", "optional")
        if needs_labels and not layers_raw:
            raise _fail(f"{path}.label_layers", f"{metric!r} needs labels; name the layers they come from")
        if not needs_labels and layers_raw:
            raise _fail(f"{path}.label_layers", f"{metric!r} needs no labels, so it reads no label layer")
        if _DERIVED_LAYER in layers_raw and len(layers_raw) > 1:
            raise _fail(
                f"{path}.label_layers", "derived gold is computed from the other layers; list it alone"
            )
        views[agent] = DefaultView(
            agent, metric, confirmed, source, None, guard, guard_confirmed, tuple(layers_raw)
        )
    absent = [name for name, entry in roster.items() if entry.status == "live" and name not in views]
    if absent:
        raise _fail(
            "default_views", f"{absent[0]!r} is a live agent with no entry (a metric, or null with a reason)"
        )
    return views


def _build(data: Any) -> MetricTable:
    if not isinstance(data, Mapping):
        raise _fail("<root>", "the metric table must be a mapping")
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise _fail(str(unknown[0]), "unknown key")
    for key in sorted(_TOP_KEYS):
        if key not in data:
            raise _fail(key, "missing")
    if data["table_format"] != TABLE_FORMAT or isinstance(data["table_format"], bool):
        raise _fail(
            "table_format", f"this loader reads table_format {TABLE_FORMAT}, got {data['table_format']!r}"
        )
    version = data["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _fail("version", f"must be a positive int, got {version!r}")
    families = _families(data["agent_families"])
    raw_rows = data["metrics"]
    if not isinstance(raw_rows, list) or not raw_rows:
        raise _fail("metrics", "must be a non-empty list of rows")
    rows = tuple(_row(raw, index, families) for index, raw in enumerate(raw_rows))
    seen_ids: dict[str, int] = {}
    seen_metric_ids: dict[str, str] = {}
    for index, row in enumerate(rows):
        if row.id in seen_ids:
            raise _fail(f"metrics[{index}].id", f"{row.id!r} repeats metrics[{seen_ids[row.id]}]")
        seen_ids[row.id] = index
        if row.metric_id in seen_metric_ids:
            raise _fail(
                f"metrics[{index}] ({row.id}).metric_id",
                f"{row.metric_id!r} is already the id of {seen_metric_ids[row.metric_id]!r}",
            )
        seen_metric_ids[row.metric_id] = row.id
    _check_aliases(rows)
    roster = _roster(data["agent_roster"], rows, families)
    views = _default_views(data["default_views"], rows, roster)
    table = MetricTable(
        version=version,
        source=MappingProxyType(_source(data["source"])),
        agent_families=MappingProxyType(families),
        agent_roster=MappingProxyType(roster),
        rows=rows,
        default_views=MappingProxyType(views),
        content_hash="",
    )
    object.__setattr__(table, "content_hash", _hash(metric_table_canonical_form(table)))
    return table


def load_metric_table(source: str | Path | Mapping[str, Any] | None = None) -> MetricTable:
    """Read and validate a metric table: the packaged ``metrics.yaml`` when ``source`` is None."""
    if source is None:
        text = resources.files(__package__).joinpath("metrics.yaml").read_text(encoding="utf-8")
        return _build(_parse(text))
    if isinstance(source, Mapping):
        return _build(source)
    path = Path(source)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise MetricTableError(f"{path}: cannot read the metric table ({error})") from error
    try:
        data = _parse(text)
    except yaml.YAMLError as error:
        raise MetricTableError(f"{path}: not valid YAML ({error})") from error
    return _build(data)
