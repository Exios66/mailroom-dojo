"""Import review decisions into a ``review`` snapshot (``mailroom-labels/review/v1``).

Two native shapes come in:

- **decision files** already in the review shape, one decision per line: ``{doc_id, field,
  verdict, reviewer: {model, role}, note, entry: {value, evidence?, absent?, basis?}}``
  (the Claude Opus audit decisions and the owner decisions exported from the review desk);
- **desk logs** (the sandbox ``human_reviews.jsonl``): ``{catalog, doc_id, filename, field,
  decision, value, label_before_review, claude_verdict, note, reviewed_at, source}``, the
  owner's own marks. ``decision`` maps to a verdict: ``keep`` -> ``accept``,
  ``use_suggestion`` and ``correct`` -> ``correct``. ``unsure`` is a normal desk outcome that
  decides nothing: it never becomes a label and is listed under ``source.undecided`` (with
  ``replaced``: whether a later dated human decision on the same label settled it). Any
  other decision word cannot be imported and is listed under ``source.not_imported`` (which
  refuses the import, see ``common.refuse_dropped``). The reviewer is ``{model: human,
  role: <source>}``: a human desk decision is ``gold``. No initials are invented.

Rules, applied per (document, field):

1. Among human decisions, the latest ``reviewed_at`` wins (the desk's own rule), compared
   as instants: ``reviewed_at`` must be an ISO 8601 string with a UTC offset (``Z``,
   ``+00:00``, ``+05:00``; fractional seconds allowed) or null, and any other value makes the
   line a rejected line. A dated decision with the same value as the winner is merged into
   it (its ``origin`` is added, and its ``evidence`` is kept when the winner has none); one
   with another value is listed under ``source.superseded``. Decisions that cannot be
   ordered against the winner are never resolved by a guess: the same instant written two
   ways is a tie, and an **undated** human decision cannot be ordered against a dated one.
   Such a decision is merged when its value equals the winner's and otherwise kept beside
   it, so the quality gate reports a ``conflicting_decision`` error.
2. An ``unsure`` mark is a mark like any other, so the latest one counts: when the latest
   ``unsure`` on a label is later than every human decision on it, those decisions are
   withdrawn (listed under ``source.superseded``, never gold) and the label stays
   undecided. A later human decision settles the label as usual. When an ``unsure`` mark
   cannot be ordered against a human decision on the same label (either is undated, or the
   latest mark and the latest decision are the same instant), nothing is guessed: the mark
   is listed under ``source.unordered_undecided``, which refuses the import like a rejected
   line (and, with ``--allow-rejected``, fails the quality gate).
3. A human decision beats a Claude decision on the same label: the Claude one is listed
   under ``source.superseded`` and is not truth.
4. Claude decisions with the same value are merged; different values are all kept (the
   gate reports a duplicate).

Lines that are not JSON objects, or lack a key the mapping needs, are listed under
``source.rejected_lines``. Output: one ``decisions.jsonl``, sorted by document, field and
reviewer, each line carrying its ``origin`` (``<file>:<line>`` of every source line merged
into it) and ``reviewed_at`` (null when the source has none).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailroom_labels.importers.common import (
    SourceLine,
    builder_name,
    parse_jsonl_lenient,
    read_source,
    refuse_dropped,
)
from mailroom_labels.layers import CURRENT_LAYER_SCHEMA
from mailroom_labels.models import SnapshotManifest
from mailroom_labels.snapshot import write_snapshot
from mailroom_record.hashing import canonical_json

__all__ = ["DESK_UNDECIDED", "DESK_VERDICTS", "HUMAN_MODEL", "SUPERSEDED_REASONS", "build_review"]

HUMAN_MODEL = "human"
#: Desk ``decision`` -> review verdict.
DESK_VERDICTS: dict[str, str] = {"keep": "accept", "use_suggestion": "correct", "correct": "correct"}
#: Desk decisions that record "no decision yet": listed under ``source.undecided``, never a label.
DESK_UNDECIDED = ("unsure",)
_EPOCH = datetime.min.replace(tzinfo=UTC)


def reviewed_instant(value: Any) -> datetime | None:
    """The instant a ``reviewed_at`` names: ``None`` for an undated decision, otherwise a
    timezone-aware datetime (``Z``, ``+00:00`` or any offset; fractional seconds allowed).
    Raises ``ValueError`` for a non-string, an unparseable string or one without an offset,
    since a time without a zone cannot be ordered against another."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"reviewed_at must be a string or null, not {type(value).__name__}")
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"reviewed_at {value!r} is not an ISO 8601 timestamp") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(f"reviewed_at {value!r} has no UTC offset")
    return instant


def _at(row: Mapping[str, Any]) -> tuple[bool, datetime]:
    """Sort key: every dated decision sorts after (is later than) every undated one."""
    instant = reviewed_instant(row["reviewed_at"])
    return (instant is not None, instant or _EPOCH)


_LATER = "a later reviewed_at decision on the same label"
_HUMAN = "a human decision on the same label"
_UNSURE = "a later unsure mark on the same label"
#: Every reason a decision may be superseded for; any other would be a builder bug and is refused.
SUPERSEDED_REASONS = (_LATER, _HUMAN, _UNSURE)
_DESK_REQUIRED = ("doc_id", "field", "decision", "value", "reviewed_at", "source")
_DECISION_REQUIRED = ("doc_id", "field", "verdict", "reviewer", "entry")


def _missing(data: Mapping[str, Any], keys: Sequence[str]) -> list[str]:
    return [k for k in keys if k not in data]


def _is_name(value: object) -> bool:
    """A non-empty string without surrounding blanks (what the review reader calls a name)."""
    return isinstance(value, str) and value != "" and value.strip() == value


def _from_decision(line: SourceLine, rejected: list) -> dict[str, Any] | None:
    d = line.data
    missing = _missing(d, _DECISION_REQUIRED)
    if missing:
        rejected.append({"file": line.file, "line": line.line, "reason": f"missing key(s) {missing}"})
        return None
    reviewer, entry = d["reviewer"], d["entry"]
    # the same reviewer shape the review layer reader requires, so nothing imported here
    # can fail to load later
    if not (
        isinstance(reviewer, dict)
        and set(reviewer) == {"model", "role"}
        and _is_name(reviewer["model"])
        and _is_name(reviewer["role"])
    ) or not (isinstance(entry, dict) and "value" in entry):
        rejected.append(
            {"file": line.file, "line": line.line, "reason": "reviewer or entry has the wrong shape"}
        )
        return None
    row = dict(d)
    row["origin"] = [line.origin]
    row.setdefault("reviewed_at", None)
    try:
        reviewed_instant(row["reviewed_at"])
    except ValueError as exc:
        rejected.append({"file": line.file, "line": line.line, "reason": str(exc)})
        return None
    return row


def _from_desk(
    line: SourceLine, rejected: list, not_imported: list, undecided: list
) -> dict[str, Any] | None:
    d = line.data
    missing = _missing(d, _DESK_REQUIRED)
    if missing:
        rejected.append({"file": line.file, "line": line.line, "reason": f"missing key(s) {missing}"})
        return None
    try:
        reviewed_instant(d["reviewed_at"])
    except ValueError as exc:
        rejected.append({"file": line.file, "line": line.line, "reason": str(exc)})
        return None
    decision = d["decision"]
    if not isinstance(decision, str):
        rejected.append(
            {
                "file": line.file,
                "line": line.line,
                "reason": f"decision must be a string, not {type(decision).__name__}",
            }
        )
        return None
    if not _is_name(d["source"]):
        rejected.append(
            {"file": line.file, "line": line.line, "reason": "source (the reviewer role) must be a name"}
        )
        return None
    if decision in DESK_UNDECIDED:
        undecided.append(
            {
                "doc_id": d["doc_id"],
                "field": d["field"],
                "decision": decision,
                "reviewed_at": d["reviewed_at"],
                "note": d.get("note"),
                "origin": [line.origin],
            }
        )
        return None
    if decision not in DESK_VERDICTS:
        not_imported.append(
            {
                "file": line.file,
                "line": line.line,
                "doc_id": d["doc_id"],
                "field": d["field"],
                "decision": decision,
                "reason": "the decision does not settle the label",
            }
        )
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


def _label(row: Mapping[str, Any]) -> tuple[str, str]:
    return (str(row["doc_id"]), str(row["field"]))


def _undecided(undecided: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each ``unsure`` mark with ``replaced``: true when a dated human decision on the same
    label is later than a dated mark (an undated mark cannot be ordered, so it is never
    ``replaced``; ``_withdraw_by_unsure`` lists it as unordered when the label has a decision)."""
    out = []
    for u in sorted(undecided, key=lambda u: (str(u["doc_id"]), str(u["field"]), u["origin"])):
        mark = reviewed_instant(u["reviewed_at"])
        later = [
            r
            for r in rows
            if _label(r) == _label(u)
            and _is_human(r)
            and (at := reviewed_instant(r["reviewed_at"])) is not None
            and mark is not None
            and at > mark
        ]
        out.append({**u, "replaced": bool(later)})
    return out


def _withdraw_by_unsure(
    undecided: list[dict[str, Any]], rows: list[dict[str, Any]], superseded: list
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the desk's "a later mark replaces an earlier one" to ``unsure`` marks. Returns
    the rows that stay and the marks that cannot be ordered against a human decision on
    their label (rule 2 of the module docstring); withdrawn decisions go to ``superseded``."""
    marks: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for u in undecided:
        marks[_label(u)].append(u)
    humans: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if _is_human(r):
            humans[_label(r)].append(r)
    withdrawn: set[int] = set()
    unordered: list[dict[str, Any]] = []
    for key in sorted(marks):
        decided = humans.get(key, [])
        if not decided:
            continue  # an unsure mark alone: no decision to withdraw or to order against
        mark_at = [reviewed_instant(u["reviewed_at"]) for u in marks[key]]
        decided_at = [reviewed_instant(r["reviewed_at"]) for r in decided]
        dated_marks = [m for m in mark_at if m is not None]
        dated_decisions = [d for d in decided_at if d is not None]
        if len(dated_marks) < len(mark_at) or len(dated_decisions) < len(decided_at):
            reason = "an undated unsure mark or human decision cannot be ordered against the other"
        elif max(dated_marks) == max(dated_decisions):
            reason = "the latest unsure mark and the latest human decision are the same instant"
        else:
            if max(dated_marks) > max(dated_decisions):
                for r in decided:
                    withdrawn.add(id(r))
                    superseded.append(_summary(r, _UNSURE))
            continue
        unordered.extend(
            {
                "doc_id": u["doc_id"],
                "field": u["field"],
                "reviewed_at": u["reviewed_at"],
                "origin": u["origin"],
                "reason": reason,
            }
            for u in sorted(marks[key], key=lambda u: u["origin"])
        )
    return [r for r in rows if id(r) not in withdrawn], unordered


def _is_human(row: Mapping[str, Any]) -> bool:
    return row["reviewer"].get("model") == HUMAN_MODEL


def _value(row: Mapping[str, Any]) -> bytes:
    return canonical_json(row["entry"]["value"])


def _summary(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "doc_id": row["doc_id"],
        "field": row["field"],
        "reviewer": row["reviewer"],
        "value": row["entry"]["value"],
        "origin": row["origin"],
        "reason": reason,
    }


def _merge_into(winner: dict[str, Any], other: Mapping[str, Any]) -> None:
    winner["origin"] = sorted(set(winner["origin"]) | set(other["origin"]))
    if "evidence" not in winner["entry"] and "evidence" in other["entry"]:
        winner["entry"] = {**winner["entry"], "evidence": other["entry"]["evidence"]}


def _collapse(
    group: list[dict[str, Any]], *, by_time: bool, superseded: list, merged: Counter
) -> list[dict[str, Any]]:
    """One decision per value; with ``by_time`` the latest reviewed_at instant decides
    between values (compared as instants, so ``+05:00`` and ``Z`` order correctly and the same
    instant written two ways is a tie)."""
    ordered = sorted(group, key=lambda r: (_at(r), r["origin"]), reverse=True)
    if by_time:
        latest = _at(ordered[0])
        top = [r for r in ordered if _at(r) == latest]
        if latest[0] and len({_value(r) for r in top}) == 1:
            winner = dict(top[0])
            undated: list[dict[str, Any]] = []
            for other in ordered[1:]:
                if _value(other) == _value(winner):
                    _merge_into(winner, other)
                    merged["human"] += 1
                elif other["reviewed_at"] is not None:
                    superseded.append(_summary(other, _LATER))
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
    allow_rejected: bool = False,
) -> SnapshotManifest:
    """Write the review snapshot. ``decisions`` and ``desk`` are ``(local path, name)`` pairs;
    the name (never the local path) is what the manifest records. ``provenance`` (where the
    files come from: repositories, commits, dates) is recorded as given. Two sources with the
    same name raise ``ValueError`` before any is read: the name is every row's ``origin``, so
    a shared one would merge two files' lines in the audit trail."""
    out_dir = Path(out_dir)
    names = [name for _, name in (*decisions, *desk)]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ValueError(f"two source files have the same name {repeated[0]!r}; give each its own name")
    rejected: list[dict[str, Any]] = []
    not_imported: list[dict[str, Any]] = []
    undecided: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    files = []
    for kind, items in (("decisions", decisions), ("desk", desk)):
        for path, name in items:
            data, digest = read_source(path)
            lines, bad = parse_jsonl_lenient(data, name)
            rejected.extend(bad)
            files.append({"path": name, "shape": kind, "sha256": digest, "rows": len(lines)})
            for line in lines:
                row = (
                    _from_decision(line, rejected)
                    if kind == "decisions"
                    else _from_desk(line, rejected, not_imported, undecided)
                )
                if row is not None:
                    rows.append(row)

    superseded: list[dict[str, Any]] = []
    marked = _undecided(undecided, rows)
    rows, unordered = _withdraw_by_unsure(undecided, rows, superseded)
    groups: dict[tuple[str, str], dict[bool, list]] = defaultdict(lambda: {True: [], False: []})
    for row in rows:
        groups[_label(row)][_is_human(row)].append(row)
    merged: Counter = Counter()
    out: list[dict[str, Any]] = []
    for key in sorted(groups):
        human, other = groups[key][True], groups[key][False]
        if human:
            out.extend(_collapse(human, by_time=True, superseded=superseded, merged=merged))
            superseded.extend(_summary(r, _HUMAN) for r in other)
        elif other:
            out.extend(_collapse(other, by_time=False, superseded=superseded, merged=merged))

    unexplained = [s for s in superseded if s["reason"] not in SUPERSEDED_REASONS]
    refuse_dropped(
        {
            "rejected_lines": rejected,
            "not_imported": not_imported,
            "unexplained_superseded": unexplained,
            "unordered_undecided": unordered,
        },
        allow=allow_rejected,
    )
    by_reviewer = Counter(f"{r['reviewer'].get('model')}:{r['reviewer'].get('role')}" for r in out)
    source = {
        **dict(provenance or {}),
        "files": files,
        "builder": {
            "name": "mailroom_labels.importers.review",
            "version": builder_name("import-review").split()[-1],
        },
        "decisions_by_reviewer": dict(sorted(by_reviewer.items())),
        "merged_duplicates": dict(sorted(merged.items())),
        "superseded": sorted(superseded, key=lambda s: (s["doc_id"], s["field"], s["origin"])),
        "not_imported": not_imported,
        "rejected_lines": rejected,
        **({"unexplained_superseded": unexplained} if unexplained else {}),
        **({"undecided": marked} if undecided else {}),
        **({"unordered_undecided": unordered} if unordered else {}),
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
