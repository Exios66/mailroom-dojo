"""Coverage: what can be scored, per class and field, by label status and by layer rank.

The v1 analysis found 8 of 25 documents unscorable because only one field was labelled,
which hid every change in those specialists. This report makes that visible: for each
class it counts documents (all, verified, with at least one scorable field) and, for every
field the profile defines (a field with no labels shows zeros), the scorable labels by
effective status and by the rank that won. A class the profile does not know is listed
too (with no scorable field), so no labelled document is silently dropped.

``coverage`` returns plain JSON; ``render_markdown`` renders it. Keys and rows are sorted,
so two reports over the same input are byte-identical and diffs show real changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from mailroom_labels.models import RANKS, STATUS_WORDS, DocumentLabels
from mailroom_labels.profile import ClientProfile

__all__ = ["coverage", "render_markdown"]

_UNKNOWN = "(unknown)"


def _empty_field() -> dict[str, Any]:
    return {"scorable": 0, "by_status": {s: 0 for s in STATUS_WORDS}, "by_rank": {r: 0 for r in RANKS}}


def coverage(
    documents: Iterable[DocumentLabels],
    profile: ClientProfile,
    snapshots: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """The coverage report as a JSON-safe dict (see the module docstring)."""
    classes: dict[str, dict[str, Any]] = {}

    def cls(name: str) -> dict[str, Any]:
        if name not in classes:
            spec = profile.classes.get(name)
            classes[name] = {
                "in_profile": spec is not None,
                "documents": 0,
                "verified_documents": 0,
                "scorable_documents": 0,
                "fields": {f: _empty_field() for f in (spec.fields if spec else {})},
            }
        return classes[name]

    for name in profile.classes:
        cls(name)
    for doc in documents:
        row = cls(doc.doc_class if doc.doc_class is not None else _UNKNOWN)
        row["documents"] += 1
        row["verified_documents"] += int(doc.verified)
        row["scorable_documents"] += int(bool(doc.scorable_fields))
        for fname in doc.scorable_fields:
            label = doc.fields[fname]
            cell = row["fields"].setdefault(fname, _empty_field())
            cell["scorable"] += 1
            cell["by_status"][label.status] += 1
            cell["by_rank"][label.rank] += 1

    totals = {
        "documents": sum(c["documents"] for c in classes.values()),
        "verified_documents": sum(c["verified_documents"] for c in classes.values()),
        "scorable_documents": sum(c["scorable_documents"] for c in classes.values()),
        "scorable_labels": sum(f["scorable"] for c in classes.values() for f in c["fields"].values()),
    }
    return {
        "client": profile.client,
        "snapshots": [dict(s) for s in sorted(snapshots, key=lambda s: s["snapshot_id"])],
        "totals": totals,
        "classes": {
            name: {**c, "fields": dict(sorted(c["fields"].items()))} for name, c in sorted(classes.items())
        },
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    """A Markdown rendering of ``coverage(...)``: totals, then one table per class."""
    lines = [f"# Label coverage: {report['client']}", ""]
    if report["snapshots"]:
        lines += ["| Snapshot | Kind | Manifest hash |", "| --- | --- | --- |"]
        lines += [f"| {s['snapshot_id']} | {s['kind']} | `{s['manifest_hash']}` |" for s in report["snapshots"]]
        lines.append("")
    t = report["totals"]
    lines += [
        f"Documents: {t['documents']}; verified: {t['verified_documents']}; "
        f"with a scorable field: {t['scorable_documents']}; scorable labels: {t['scorable_labels']}.",
        "",
    ]
    for name, c in report["classes"].items():
        note = "" if c["in_profile"] else " (not in the profile)"
        lines += [
            f"## {name}{note}",
            "",
            f"Documents: {c['documents']}; verified: {c['verified_documents']}; "
            f"with a scorable field: {c['scorable_documents']}.",
            "",
            "| Field | Scorable | " + " | ".join(STATUS_WORDS) + " | " + " | ".join(RANKS) + " |",
            "| --- | ---: | " + " | ".join("---:" for _ in (*STATUS_WORDS, *RANKS)) + " |",
        ]
        for fname, cell in c["fields"].items():
            nums = [str(cell["by_status"][s]) for s in STATUS_WORDS] + [str(cell["by_rank"][r]) for r in RANKS]
            lines.append(f"| {fname} | {cell['scorable']} | " + " | ".join(nums) + " |")
        lines.append("")
    return "\n".join(lines)
