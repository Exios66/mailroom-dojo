"""The metric table: one flat, versioned row per metric, shipped as ``measures/metrics.yaml``.

:func:`load_metric_table` reads the table (the packaged ``metrics.yaml`` by default, or
a path or an already parsed mapping), checks every key and value, and returns an
immutable :class:`MetricTable`. A problem raises :class:`MetricTableError` naming the
YAML path, for example ``metrics[3].direction: 'up' is not one of higher, lower, none``.

The table replaces the old registry's tiers, bundles and profiles: the dashboard view
is a query (:meth:`MetricTable.headline_rows`, :meth:`MetricTable.rows_for_agent`).
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
    "AGGREGATIONS",
    "DIRECTIONS",
    "GROUND_TRUTH",
    "LAYERS",
    "NON_NUMERIC_UNITS",
    "ORIGINS",
    "PAIRINGS",
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

#: Units, as the old registry wrote them (``usd`` and ``USD`` both occur and are kept).
UNITS: tuple[str, ...] = (
    "float[0,1]",
    "count",
    "int",
    "boolean",
    "error_rate",
    "seconds",
    "milliseconds",
    "days",
    "USD",
    "usd",
    "GB",
    "tokens/s",
    "req/s",
    "docs/s",
    "tag",
    "text",
)
#: Units whose rows describe a label or text, not a number: no ``Measure`` carries them.
NON_NUMERIC_UNITS: tuple[str, ...] = ("tag", "text")
DIRECTIONS: tuple[str, ...] = ("higher", "lower", "none")
#: How per-document values roll up to the run (the old registry's three values).
AGGREGATIONS: tuple[str, ...] = ("mean", "sum", "none")
#: The old ``ALLOWED_GROUND_TRUTH`` without the empty string (written as null here).
GROUND_TRUTH: tuple[str, ...] = ("required", "optional", "structural", "none")
#: Existing Dojo modules, then the build chunks of the port design (section 6).
LAYERS: tuple[str, ...] = ("classification", "field_scoring", "D1", "D3b", "D4", "D5", "D6", "D7")
_EXISTING_LAYERS = frozenset({"classification", "field_scoring"})
STATUSES: tuple[str, ...] = ("implemented", "planned", "not_ported")
ORIGINS: tuple[str, ...] = ("registry", "golden")
#: Pairing rules for comparing two runs. ``two_sample_percentile_bootstrap`` is the old
#: ``analysis.bootstrap.delta_significance`` that ``compare_serving`` runs on latencies.
PAIRINGS: tuple[str, ...] = ("two_sample_percentile_bootstrap",)

#: The gates, reports and projections that read metrics, as the master design and the port
#: design name them (``metrics.yaml`` says where). A kept metric names at least one.
READERS: tuple[str, ...] = (
    "golden_parity_gate",
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
        "noise_floor": None,
        "old_source": None,
        "notes": "",
    }
)
_TOP_KEYS = frozenset({"table_format", "version", "source", "agent_families", "metrics"})


def _parse(text: str) -> Any:
    """Parse with libyaml's safe loader when PyYAML has it (same rules, faster), else the pure one."""
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    return yaml.load(text, Loader=loader)  # noqa: S506 - always a safe loader


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
class MetricTable:
    """The loaded, validated table. Immutable; identified by ``content_hash``."""

    version: int
    source: Mapping[str, str]
    agent_families: Mapping[str, tuple[str, ...]]
    rows: tuple[MetricRow, ...]
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

    def headline_rows(self) -> tuple[MetricRow, ...]:
        """The headline metrics, in table order."""
        return tuple(row for row in self.rows if row.headline)

    def rows_for_agent(self, agent: str, *, headline_only: bool = False) -> tuple[MetricRow, ...]:
        """The metrics that apply to ``agent`` (rows for ``ALL`` included)."""
        return tuple(
            row for row in self.rows if row.applies_to(agent) and (row.headline or not headline_only)
        )

    def rows_for_reader(self, reader: str) -> tuple[MetricRow, ...]:
        """The metrics a gate, report or projection reads (an unknown reader raises)."""
        if reader not in READERS:
            raise UnknownMetricError(f"unknown reader {reader!r}; known: {', '.join(READERS)}")
        return tuple(row for row in self.rows if reader in row.readers)

    def headlines_by_agent(self) -> Mapping[str, tuple[str, ...]]:
        """Each named agent and the headline ids that name it (``ALL`` rows under ``ALL``)."""
        out: dict[str, list[str]] = {}
        for row in self.headline_rows():
            for agent in row.expanded_agents:
                out.setdefault(agent, []).append(row.id)
        return MappingProxyType({agent: tuple(ids) for agent, ids in sorted(out.items())})

    def check(self, measure: Measure) -> MetricRow:
        """Check a measure against its metric's row and return the row.

        Raises :class:`UnknownMetricError` for an id outside the table and
        :class:`MeasureError` when the measure breaks the row: the metric is not ported
        or is not a number (a tag or text); the value is outside the unit's range
        (``float[0,1]`` and ``boolean`` in ``[0, 1]``, every other unit non-negative); a
        ``count`` or ``int`` value is not a whole number (unless the row's aggregation
        is ``mean``: a mean of counts need not be whole); a ``boolean`` is not 0 or 1;
        the interval leaves the unit's range or does not contain the value; a metric
        that needs labels claims ``label_status="none"``; or the cost basis does not
        match the row (a cost needs a basis; any other metric has ``none``).
        """
        row = self.get(measure.method)
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
_WHOLE_UNITS = frozenset({"count", "int"})
#: Units bounded by ``[0, 1]``; every other numeric unit is non-negative.
_UNIT_INTERVAL = frozenset({"float[0,1]", "boolean"})


def _check_in_unit(row: MetricRow, number: float, what: str) -> None:
    if row.unit in _UNIT_INTERVAL and not 0.0 <= number <= 1.0:
        raise MeasureError(f"{row.id}: {what} {number!r} is outside [0, 1] for unit {row.unit!r}")
    if number < 0:
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
        pairing=_choice(spec["pairing"], PAIRINGS, f"{path}.pairing", nullable=True),
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
        "metrics": [_row_canonical(row) for row in table.rows],
    }


def _hash(canonical: Mapping[str, Any]) -> str:
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    table = MetricTable(
        version=version,
        source=MappingProxyType(_source(data["source"])),
        agent_families=MappingProxyType(families),
        rows=rows,
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
