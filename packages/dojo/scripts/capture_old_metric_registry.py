"""Freeze the old Dojo's metric registry, its canonical metric ids and the golden metric names.

Writes ``packages/dojo/tests/fixtures/measures/old_metric_registry.json``, the expected
side of the metric table's parity test (``tests/test_metric_table_parity.py``). The
tests read only that file, never an old checkout. It holds:

``metrics``
    Every entry of the old built-in registry (``metrics/registry.py``,
    ``DEFAULT_METRICS_YAML``), loaded the way the old ``Registry.from_dict`` loads it
    (family tokens expanded, ``metric_meta.METRIC_META`` merged in), with its tier,
    units, description, applicable agents, aggregation, source, notes, citation,
    inclusion and ground-truth requirement. ``order`` keeps the old YAML order.
``agent_families``
    The old roster families (``SPECIALISTS``, ``AUDITORS``, ...) and their members.
``metric_ids``
    The old canonical comparison ids (``scorecard_honesty._METRIC_ID_BY_NAME``) and the
    "Canonical metric_ids" table of ``docs/contracts/METRIC_IDS.md``, parsed separately
    so the test can show the two agree.
``golden``
    Every metric name the old golden baseline (``tests/fixtures/golden/scores.json``)
    emits: the numeric or null top-level keys of each ``score``, ``score_document`` and
    ``chained_summary`` payload, the ``metric_names`` of emit-only suites and the keys
    of the serving comparison's ``metrics`` block, each with the places it appears
    (``names``); every nested block of those payloads (``breakdowns``); and the sha256
    of that ``scores.json``.

The nested-breakdown rule: a numeric or null value below the top level of a payload is
a breakdown of a metric (per class, per question, per field, per serving side, per
pipeline stage), an interval, a count block or configuration, never a new metric id.
So the capture records, for each nested block ``<payload>.<key>`` (a mapping value of a
top-level key; the serving ``metrics`` block is read as names instead), the sorted
names of its numeric or null leaves at any depth (the last path component) and the
places it appears. The parity test maps every block to a reason, and maps each leaf of
the blocks that carry metric values under their own names (the chained summary's
stages and the MAUD ``per_question`` block) to a row or a reasoned exception.

The registry is read from the YAML constant, never through ``load_registry``, so the
``LLM_DOJO_SCORING_REGISTRY`` environment variable cannot change the capture. Run it
only with an interpreter that can import the old Dojo (PyYAML is its one need here),
never in CI::

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python packages/dojo/scripts/capture_old_metric_registry.py \\
        --old-dojo <llm-dojo-scoring checkout at 23f8234c> \\
        --output packages/dojo/tests/fixtures/measures/old_metric_registry.json

It writes nothing inside the old checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

EXPECTED_OLD_COMMIT = "23f8234c3c548bdc203033388bb316e52e588a2c"

#: The heading of the METRIC_IDS.md table that lists every canonical id.
_CANONICAL_HEADING = "## Canonical metric_ids"
_TABLE_ROW = re.compile(r"^\|\s*(?P<names>[^|]+?)\s*\|\s*`(?P<metric_id>[^`]+)`\s*\|\s*$")


def _old_commit(old_dojo: Path) -> str:
    """The old checkout's HEAD commit."""
    return subprocess.run(
        ["git", "-C", str(old_dojo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _registry(old_dojo: Path) -> tuple[dict[str, Any], list[str], dict[str, list[str]], str]:
    """The old registry's entries, their YAML order, the agent families and the old version."""
    sys.path.insert(0, str(old_dojo))
    import llm_dojo_scoring
    import yaml
    from llm_dojo_scoring.metrics import registry

    raw = yaml.safe_load(registry.DEFAULT_METRICS_YAML)["metrics"]
    loaded = registry.Registry.from_dict({"metrics": raw})
    metrics = {
        name: {
            "tier": int(entry.tier),
            "units": entry.units,
            "description": entry.description,
            "applicable_agents": list(entry.applicable_agents),
            "aggregation": entry.aggregation,
            "source": entry.source,
            "notes": entry.notes,
            "citation": entry.citation,
            "inclusion": entry.inclusion,
            "ground_truth": entry.ground_truth,
        }
        for name, entry in loaded.metrics.items()
    }
    families = {name: list(members) for name, members in registry._AGENT_FAMILIES.items()}
    return metrics, list(raw), families, llm_dojo_scoring.__version__


def _metric_ids_from_code() -> dict[str, str]:
    """``scorecard_honesty._METRIC_ID_BY_NAME``: registry name to canonical id."""
    from llm_dojo_scoring.metrics import scorecard_honesty

    return dict(scorecard_honesty._METRIC_ID_BY_NAME)


def _metric_ids_from_doc(old_dojo: Path) -> dict[str, str]:
    """The "Canonical metric_ids" table of METRIC_IDS.md (a row may name two aliases)."""
    text = (old_dojo / "docs" / "contracts" / "METRIC_IDS.md").read_text(encoding="utf-8")
    section = text.split(_CANONICAL_HEADING, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
    out: dict[str, str] = {}
    for line in section.splitlines():
        match = _TABLE_ROW.match(line)
        if not match:
            continue
        for name in re.findall(r"`([^`]+)`", match.group("names")):
            out[name] = match.group("metric_id")
    if not out:
        raise SystemExit("no canonical metric_id rows found in METRIC_IDS.md")
    return out


def _is_metric_value(value: Any) -> bool:
    """A number or null: what a metric key holds (bools, strings and containers are not)."""
    return value is None or (isinstance(value, int | float) and not isinstance(value, bool))


#: The payloads of a golden case whose top-level numeric keys are metric names.
_PAYLOADS = ("score", "score_document", "chained_summary")


def _leaf_names(value: Any, out: set[str], name: str = "") -> None:
    """The last path component of every numeric or null leaf under ``value`` (lists are not entered)."""
    if isinstance(value, dict):
        for key, inner in value.items():
            _leaf_names(inner, out, str(key))
    elif not isinstance(value, list) and _is_metric_value(value):
        out.add(name)


def _golden_breakdowns(scores: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """Every nested block of a payload: its numeric leaf names and the places it appears."""
    blocks: dict[str, tuple[set[str], set[str]]] = {}
    for section, cases in scores.items():
        for case, body in cases.items():
            for key in _PAYLOADS:
                payload = body.get(key)
                if not isinstance(payload, dict):
                    continue
                for name, value in payload.items():
                    if not isinstance(value, dict) or name == "metrics":
                        continue
                    leaves, places = blocks.setdefault(f"{key}.{name}", (set(), set()))
                    _leaf_names(value, leaves)
                    places.add(f"{section}/{case}")
    return {
        block: {"leaves": sorted(leaves), "places": sorted(places)}
        for block, (leaves, places) in sorted(blocks.items())
    }


def _golden_names(scores: dict[str, Any]) -> dict[str, list[str]]:
    """Every metric name the golden baseline emits, with the sorted places it appears."""
    names: dict[str, set[str]] = {}
    for section, cases in scores.items():
        for case, body in cases.items():
            for name in body.get("metric_names", []):
                names.setdefault(name, set()).add(f"{section}/{case}:metric_names")
            for key in _PAYLOADS:
                payload = body.get(key)
                if not isinstance(payload, dict):
                    continue
                for name, value in payload.items():
                    if _is_metric_value(value):
                        names.setdefault(name, set()).add(f"{section}/{case}:{key}")
                block = payload.get("metrics")
                if isinstance(block, dict):
                    for name in block:
                        names.setdefault(name, set()).add(f"{section}/{case}:{key}.metrics")
    return {name: sorted(places) for name, places in sorted(names.items())}


def build(old_dojo: Path) -> dict[str, Any]:
    """The fixture document."""
    commit = _old_commit(old_dojo)
    if commit != EXPECTED_OLD_COMMIT:
        print(f"warning: old Dojo is at {commit}, expected {EXPECTED_OLD_COMMIT}", file=sys.stderr)
    metrics, order, families, version = _registry(old_dojo)
    scores_path = old_dojo / "tests" / "fixtures" / "golden" / "scores.json"
    scores_bytes = scores_path.read_bytes()
    scores = json.loads(scores_bytes)
    return {
        "old_commit": commit,
        "old_version": version,
        "source": "llm_dojo_scoring/metrics/registry.py DEFAULT_METRICS_YAML via Registry.from_dict",
        "order": order,
        "metrics": metrics,
        "agent_families": families,
        "metric_ids": {
            "code": _metric_ids_from_code(),
            "doc": _metric_ids_from_doc(old_dojo),
        },
        "golden": {
            "scores_path": "tests/fixtures/golden/scores.json",
            "scores_sha256": hashlib.sha256(scores_bytes).hexdigest(),
            "names": _golden_names(scores),
            "breakdowns": _golden_breakdowns(scores),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--old-dojo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    document = build(args.old_dojo.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, sort_keys=True, ensure_ascii=True, indent=1) + "\n")
    print(
        f"old commit {document['old_commit']}, version {document['old_version']}: "
        f"{len(document['metrics'])} metrics, {len(document['golden']['names'])} golden names, "
        f"{len(document['golden']['breakdowns'])} nested blocks"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
