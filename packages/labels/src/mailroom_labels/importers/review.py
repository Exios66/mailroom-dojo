"""Import review decisions into a ``review`` snapshot (``mailroom-labels/review/v1``).

Two native shapes come in:

- **decision files** already in the review shape, one decision per line: ``{doc_id, field,
  verdict, reviewer: {model, role}, note, entry: {value, evidence?, absent?, basis?}}``
  (the Claude Opus audit decisions and the owner decisions exported from the review desk);
- **desk logs** (the sandbox ``human_reviews.jsonl``): ``{catalog, doc_id, filename, field,
  decision, value, label_before_review, claude_verdict, note, reviewed_at, source}``, the
  owner's own marks. ``decision`` maps to a verdict: ``keep`` -> ``accept``,
  ``use_suggestion`` and ``correct`` -> ``correct``; ``unsure`` decides nothing (the label stays
  open), so it is listed under ``source.not_imported``. The reviewer is ``{model: human,
  role: <source>}``: a human desk decision is ``gold``. No initials are invented.

Rules, applied per (document, field):

1. Among human decisions, the latest ``reviewed_at`` wins (the desk's own rule). A decision
   with the same value as the winner is merged into it (its ``origin`` is added, and its
   ``evidence`` is kept when the winner has none); one with another value is listed under
   ``source.superseded``. Decisions that cannot be ordered (no timestamp, or the same one)
   with different values are all kept, so the quality gate reports the conflict instead of
   a guess.
2. A human decision beats a Claude decision on the same label: the Claude one is listed
   under ``source.superseded`` and is not truth.
3. Claude decisions with the same value are merged; different values are all kept (the
   gate reports a duplicate).

Lines that are not JSON objects, or lack a key the mapping needs, are listed under
``source.rejected_lines``. Output: one ``decisions.jsonl``, sorted by document, field and
reviewer, each line carrying its ``origin`` (``<file>:<line>`` of every source line merged
into it) and ``reviewed_at`` (null when the source has none).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mailroom_labels.importers.common import SourceLine, builder_name, file_sha256, read_jsonl_lenient
from mailroom_labels.layers import CURRENT_LAYER_SCHEMA
from mailroom_labels.models import SnapshotManifest
from mailroom_labels.snapshot import write_snapshot
from mailroom_record.hashing import canonical_json

__all__ = ["DESK_VERDICTS", "HUMAN_MODEL", "build_review"]

HUMAN_MODEL = "human"
#: Desk ``decision`` -> review verdict. A decision not listed here (``unsure``) decides nothing.
DESK_VERDICTS: dict[str, str] = {"keep": "accept", "use_suggestion": "correct", "correct": "correct"}
_DESK_REQUIRED = ("doc_id", "field", "decision", "value", "reviewed_at", "source")
_DECISION_REQUIRED = ("doc_id", "field", "verdict", "reviewer", "entry")


def _missing(data: Mapping[str, Any], keys: Sequence[str]) -> list[str]:
    return [k for k in keys if k not in data]


def _from_decision(line: SourceLine, rejected: list) -> dict[str, Any] | None:
    d = line.data
    missing = _missing(d, _DECISION_REQUIRED)
    if missing:
        rejected.append({"file": line.file, "line": line.line, "reason": f"missing key(s) {missing}"})
        return None
    reviewer, entry = d["reviewer"], d["entry"]
    if not (isinstance(reviewer, dict) and isinstance(reviewer.get("model"), str)) or not (
        isinstance(entry, dict) and "value" in entry
    ):
        rejected.append({"file": line.file, "line": line.line, "reason": "reviewer or entry has the wrong shape"})
        return None
    row = dict(d)
    row["origin"] = [line.origin]
    row.setdefault("reviewed_at", None)
    return row


def _from_desk(line: SourceLine, rejected: list, not_imported: list) -> dict[str, Any] | None:
    d = line.data
    missing = _missing(d, _DESK_REQUIRED)
    if missing:
        rejected.append({"file": line.file, "line": line.line, "reason": f"missing key(s) {missing}"})
        return None
    decision = d["decision"]
    if decision not in DESK_VERDICTS:
        not_imported.append({"file": line.file, "line": line.line, "doc_id": d["doc_id"], "field": d["field"],
                             "decision": decision, "reason": "the decision does not settle the label"})
        return None
    note = f"owner review desk decision ({decision})"
    if isinstance(d.get("note"), str) and d["note"].strip():
        note += f": {d['note']}"
    return {
        "doc_id": d["doc_id"],
        "field": d["field"],
        "verdict": DESK_VERDICTS[decision],
        "reviewer": {"model": HUMAN_MODEL, "role": d["source"]},
        "note": note,
        "entry": {"value": d["value"]},
        "origin": [line.origin],
        "reviewed_at": d["reviewed_at"],
    }


def _is_human(row: Mapping[str, Any]) -> bool:
    return row["reviewer"].get("model") == HUMAN_MODEL


def _value(row: Mapping[str, Any]) -> bytes:
    return canonical_json(row["entry"]["value"])


def _summary(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {"doc_id": row["doc_id"], "field": row["field"], "reviewer": row["reviewer"],
            "value": row["entry"]["value"], "origin": row["origin"], "reason": reason}


def _merge_into(winner: dict[str, Any], other: Mapping[str, Any]) -> None:
    winner["origin"] = sorted(set(winner["origin"]) | set(other["origin"]))
    if "evidence" not in winner["entry"] and "evidence" in other["entry"]:
        winner["entry"] = {**winner["entry"], "evidence": other["entry"]["evidence"]}


def _collapse(group: list[dict[str, Any]], *, by_time: bool, superseded: list, merged: Counter) -> list[dict[str, Any]]:
    """One decision per value; with ``by_time`` the latest reviewed_at decides between values."""
    ordered = sorted(group, key=lambda r: (r["reviewed_at"] or "", r["origin"]), reverse=True)
    if by_time:
        latest = ordered[0]["reviewed_at"]
        top = [r for r in ordered if r["reviewed_at"] == latest]
        if latest is not None and len({_value(r) for r in top}) == 1:
            winner = dict(top[0])
            undated: list[dict[str, Any]] = []
            for other in ordered[1:]:
                if _value(other) == _value(winner):
                    _merge_into(winner, other)
                    merged["human"] += 1
                elif other["reviewed_at"] is not None:
                    superseded.append(_summary(other, "a later reviewed_at decision on the same label"))
                else:
                    undated.append(dict(other))  # cannot be ordered: kept, so the gate reports it
            return [winner, *undated]
    kept: dict[bytes, dict[str, Any]] = {}
    for row in sorted(ordered, key=lambda r: r["origin"]):
        key = _value(row)
        if key in kept:
            _merge_into(kept[key], row)
            merged["human" if _is_human(row) else "other"] += 1
        else:
            kept[key] = dict(row)
    return list(kept.values())


def build_review(
    out_dir: Path | str,
    *,
    decisions: Sequence[tuple[Path | str, str]] = (),
    desk: Sequence[tuple[Path | str, str]] = (),
    provenance: Mapping[str, Any] | None = None,
    snapshot_id: str | None = None,
    known_warnings: Sequence[Mapping[str, str | None]] = (),
) -> SnapshotManifest:
    """Write the review snapshot. ``decisions`` and ``desk`` are ``(local path, name)`` pairs;
    the name (never the local path) is what the manifest records. ``provenance`` (where the
    files come from: repositories, commits, dates) is recorded as given."""
    out_dir = Path(out_dir)
    rejected: list[dict[str, Any]] = []
    not_imported: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    files = []
    for kind, items in (("decisions", decisions), ("desk", desk)):
        for path, name in items:
            lines, bad = read_jsonl_lenient(Path(path), name)
            rejected.extend(bad)
            files.append({"path": name, "shape": kind, "sha256": file_sha256(path), "rows": len(lines)})
            for line in lines:
                row = _from_decision(line, rejected) if kind == "decisions" else _from_desk(line, rejected, not_imported)
                if row is not None:
                    rows.append(row)

    groups: dict[tuple[str, str], dict[bool, list]] = defaultdict(lambda: {True: [], False: []})
    for row in rows:
        groups[(str(row["doc_id"]), str(row["field"]))][_is_human(row)].append(row)
    superseded: list[dict[str, Any]] = []
    merged: Counter = Counter()
    out: list[dict[str, Any]] = []
    for key in sorted(groups):
        human, other = groups[key][True], groups[key][False]
        if human:
            out.extend(_collapse(human, by_time=True, superseded=superseded, merged=merged))
            superseded.extend(_summary(r, "a human decision on the same label") for r in other)
        elif other:
            out.extend(_collapse(other, by_time=False, superseded=superseded, merged=merged))

    by_reviewer = Counter(f"{r['reviewer'].get('model')}:{r['reviewer'].get('role')}" for r in out)
    source = {
        **dict(provenance or {}),
        "files": files,
        "builder": {"name": "mailroom_labels.importers.review", "version": builder_name("import-review").split()[-1]},
        "decisions_by_reviewer": dict(sorted(by_reviewer.items())),
        "merged_duplicates": dict(sorted(merged.items())),
        "superseded": sorted(superseded, key=lambda s: (s["doc_id"], s["field"], s["origin"])),
        "not_imported": not_imported,
        "rejected_lines": rejected,
        "reviewer_initials": "not recorded: the reviewer field holds the role the source states",
    }
    return write_snapshot(
        out_dir,
        {"decisions.jsonl": out},
        sort_key=lambda r: (str(r["doc_id"]), str(r["field"]), canonical_json(r["reviewer"]), r["origin"]),
        snapshot_id=snapshot_id or out_dir.name,
        kind="review",
        layer_schema=CURRENT_LAYER_SCHEMA["review"],
        source=source,
        known_warnings=known_warnings,
        builder=builder_name("import-review"),
    )
