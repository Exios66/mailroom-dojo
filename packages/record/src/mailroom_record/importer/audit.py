"""What routing inputs a stored sandbox run folder carries (Task 10).

``audit_sandbox_run(folder)`` reads a folder (never writes) and reports, as ``present`` or
``absent``, each input the engine's ``legacy`` router replay will need (``ROUTING_INPUTS``),
plus how many finished rows each node has (``nodes_seen``) and how many decisions each
question has (``decisions_seen``). Those are keyed by the node or question name when it is
a short token, else by a short digest of it (``<`` twelve hex digits ``>``, ``_folder.shown``),
and the run id is the lock's only when it is a valid run id, else the folder's name (a
digest of it when that is not a valid run id either), so the
audit (and ``mailroom-record audit sandbox-run``) never prints free text. An input is present when at least one row carries it:

- ``classification_confidence`` / ``extraction_confidence``: a number in an item's ``pred``,
  or the ``p`` of a ``doc_type`` / ``extraction_confidence`` decision;
- ``judge verdict per pass``: an ``extraction_verdict`` decision on a node row;
- ``arbiter route per pass``: an ``arbiter_route`` decision;
- ``sorter reviewer verdict``: a ``sorter_review`` decision or an item's ``review_verdict``;
- ``retry counts``: finished node rows (each pass of a node is a row, so retries count);
- ``effective thresholds``: a non-empty ``effective_confidence`` in ``spec.lock.json``;
- ``decision slot outputs``: a decision written by a Decisions-API slot (``source`` is
  ``"decisions"``); today's agents write ``"agent"``;
- ``boss approval``: a ``conflict_ruling`` decision.

The question names are the sandbox format's own; the record core knows none of them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mailroom_record.importer._folder import SandboxFolderInvalid, read_json, read_jsonl, shown
from mailroom_record.importer.sandbox_run import _RUN_ID_RE

__all__ = ["ROUTING_INPUTS", "RoutingAudit", "audit_sandbox_run"]

ROUTING_INPUTS: tuple[str, ...] = (
    "classification_confidence",
    "extraction_confidence",
    "judge verdict per pass",
    "arbiter route per pass",
    "sorter reviewer verdict",
    "retry counts",
    "effective thresholds",
    "decision slot outputs",
    "boss approval",
)


@dataclass(frozen=True)
class RoutingAudit:
    run_id: str
    documents: int
    nodes_seen: dict[str, int]
    decisions_seen: dict[str, int]
    present: list[str]
    absent: list[str]


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def audit_sandbox_run(folder: Path) -> RoutingAudit:
    """Report which routing inputs ``folder`` carries (see the module doc)."""
    folder = Path(folder)
    if not folder.is_dir():
        raise SandboxFolderInvalid(f"{folder.name}: not a directory")
    warnings: list[str] = []
    dataset = read_jsonl(folder / "dataset.jsonl", warnings)
    nodes = read_jsonl(folder / "nodes.jsonl", warnings) or []
    items = read_jsonl(folder / "items.jsonl", warnings) or []
    lock = read_json(folder / "spec.lock.json") or {}

    nodes_seen: Counter[str] = Counter()
    decisions_seen: Counter[str] = Counter()
    found: set[str] = set()
    for _, row in nodes:
        node = row.get("node")
        if row.get("status") == "running" or not isinstance(node, str):
            continue
        nodes_seen[shown(node)] += 1
        found.add("retry counts")
        decisions = row.get("decisions")
        for d in decisions if isinstance(decisions, list) else []:
            if not isinstance(d, dict):
                continue
            q = d.get("question")
            if isinstance(q, str):
                decisions_seen[shown(q)] += 1
            if d.get("source") == "decisions":
                found.add("decision slot outputs")
            if q == "doc_type" and _number(d.get("p")):
                found.add("classification_confidence")
            elif q == "extraction_confidence" and _number(d.get("p")):
                found.add("extraction_confidence")
            elif q == "extraction_verdict":
                found.add("judge verdict per pass")
            elif q == "arbiter_route":
                found.add("arbiter route per pass")
            elif q == "sorter_review":
                found.add("sorter reviewer verdict")
            elif q == "conflict_ruling":
                found.add("boss approval")
    for _, row in items:
        pred = row.get("pred") if isinstance(row.get("pred"), dict) else {}
        if _number(pred.get("classification_confidence")):
            found.add("classification_confidence")
        if _number(pred.get("extraction_confidence")):
            found.add("extraction_confidence")
        if pred.get("review_verdict") is not None:
            found.add("sorter reviewer verdict")
    thresholds = lock.get("effective_confidence")
    if isinstance(thresholds, dict) and thresholds:
        found.add("effective thresholds")

    run_id = lock.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        run_id = folder.name if _RUN_ID_RE.fullmatch(folder.name) else shown(folder.name)  # as the importer does
    if dataset is not None:
        documents = len(dataset)
    else:
        ids = {r.get("doc") for _, r in nodes} | {r.get("item_id") for _, r in items}
        documents = len(ids - {None})
    return RoutingAudit(
        run_id=run_id,
        documents=documents,
        nodes_seen=dict(sorted(nodes_seen.items())),
        decisions_seen=dict(decisions_seen),
        present=[i for i in ROUTING_INPUTS if i in found],
        absent=[i for i in ROUTING_INPUTS if i not in found],
    )
