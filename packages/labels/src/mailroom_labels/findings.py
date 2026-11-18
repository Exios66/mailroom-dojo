"""Finding kinds and their severities, shared by the layer readers and the quality gate.

An **error** makes ``mailroom-labels verify`` fail: a committed snapshot set must have none.
A **warning** is reported and may be accepted by listing it in a snapshot manifest's
``known_warnings``. Warnings are things the resolver already handles safely by flagging
(a stale content hash makes the document unverified, so it is never scored) or that are
expected in real review data (a decision that adds a label no layer had).
"""

from __future__ import annotations

from mailroom_labels.models import Finding

__all__ = ["SEVERITY", "finding"]

SEVERITY: dict[str, str] = {
    # loading
    "manifest_missing": "error",
    "manifest_mismatch": "error",
    "unknown_schema": "error",
    "duplicate_snapshot": "error",
    "corrupt_line": "error",
    "malformed_record": "error",
    "missing_hub": "error",
    # the importer left source data out (rejected lines, unparseable rows, undecided
    # decisions): the snapshot is incomplete, whatever it holds is not the whole source
    "dropped_source_data": "error",
    # against the corpus index
    "unknown_document": "error",
    "duplicate_document": "error",
    "class_mismatch": "error",
    # A label whose subclass disagrees with the index is stale: the resolver never uses
    # it and flags the document unverified, exactly as for a stale content hash.
    "subclass_mismatch": "warning",
    "stale_content_hash": "warning",
    "text_unverified": "warning",
    # the Hub import did not run the text check, so no document's text was verified
    "text_unchecked": "warning",
    # within and across layers
    "duplicate_label": "error",
    "status_not_allowed": "error",
    "inconsistent_truth_flag": "error",
    "orphan_decision": "warning",
    "conflicting_decision": "error",
    # against the client profile
    "unknown_class": "error",
    "unknown_subclass": "error",
    "unknown_field": "error",
    "type_mismatch": "error",
    "vocabulary_violation": "error",
}


def finding(kind: str, **fields: object) -> Finding:
    """A ``Finding`` of ``kind`` with the severity this table assigns it."""
    return Finding(kind=kind, severity=SEVERITY[kind], **fields)  # type: ignore[arg-type]
