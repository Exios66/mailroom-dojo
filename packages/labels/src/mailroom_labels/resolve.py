"""The resolver: one effective label per (document, field), by the profile's precedence.

Rules (plan decision 1 and 4, ``docs/plans/2026-11-18-labels-plan.md``):

- The label whose rank comes first in ``profile.precedence`` wins. With the mailroom
  profile that is: a gold review decision, then an interim ``correction`` (it replaces the
  Hub value it corrects), then a populated Hub value (``hub``), then near_gold review
  decisions, then interim ``gap_fill`` and ``derived``, then a Hub documented absence or
  ``not_applicable`` (``hub:absent``: the sandbox lets interim fills replace it, but not
  near_gold labels), then near_gold labels.
- A rank missing from the precedence is never truth; ``interim:disputed`` can never be in
  it. Such labels stay visible in ``layers_seen`` or, when nothing else labels the field, in
  ``DocumentLabels.untrusted``.
- A label is **stale** when it disagrees with the corpus index: its content hash does not
  name the indexed text, or its class or subclass differs. A stale label is never truth
  (the sandbox's "a hash mismatch leaves the document untouched") and the document becomes
  unverified. An unverified document (also: not in the index, or Hub text not verified)
  keeps its resolved labels but has no ``scorable_fields``: it is flagged, never silently
  scored, and it never blocks another document.
- ``scorable_fields``: fields of the document's class in the profile whose effective
  status is in ``profile.scorable_statuses`` and whose presence is not ``not_applicable``.

Pure and deterministic: no I/O, output sorted by document id and field name, and the
result does not depend on the order of ``records``. Ties inside one rank (a duplicate the
quality gate reports as an error) break on snapshot id, file, line and value.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

from mailroom_labels.layers import hash_matches
from mailroom_labels.models import (
    RANK_STATUS,
    CorpusEntry,
    DocumentLabels,
    FieldLabel,
    LayerRecord,
)
from mailroom_labels.profile import ClientProfile
from mailroom_record.hashing import canonical_json

__all__ = ["is_stale", "resolve", "resolve_document"]


def is_stale(record: LayerRecord, entry: CorpusEntry | None) -> bool:
    """True when ``record`` disagrees with the corpus index entry of its document."""
    if entry is None:
        return False
    if not hash_matches(record.content_sha256, entry.content_sha256):
        return True
    if record.doc_class is not None and record.doc_class != entry.doc_class:
        return True
    return record.subclass is not None and record.subclass != entry.subclass


def _origin(record: LayerRecord) -> tuple:
    return (record.snapshot_id, record.file, record.line, canonical_json(record.value))


def resolve_document(
    document_id: str,
    records: Iterable[LayerRecord],
    profile: ClientProfile,
    entry: CorpusEntry | None,
) -> DocumentLabels:
    """Resolve one document's labels (``records`` must all belong to ``document_id``)."""
    recs = sorted(records, key=_origin)
    reasons: list[str] = []
    if entry is None:
        reasons.append("not_in_corpus_index")
    elif not entry.text_verified:
        reasons.append("hub_text_unverified")
    stale = {id(r): is_stale(r, entry) for r in recs}
    if any(stale.values()):
        reasons.append("stale_label")

    if entry is not None:
        doc_class, subclass, content = entry.doc_class, entry.subclass, entry.content_sha256
    else:
        doc_class = next((r.doc_class for r in recs if r.doc_class is not None), None)
        subclass = next((r.subclass for r in recs if r.subclass is not None), None)
        content = None

    never = len(profile.precedence)
    by_field: dict[str, list[LayerRecord]] = defaultdict(list)
    for r in recs:
        by_field[r.field].append(r)

    fields: dict[str, FieldLabel] = {}
    untrusted = {}
    for name in sorted(by_field):
        group = by_field[name]

        def order(r: LayerRecord) -> tuple:
            idx = profile.rank_index(r.rank)
            return (never if idx is None else idx, r.rank, *_origin(r))

        facts = tuple(r.fact(stale=stale[id(r)]) for r in sorted(group, key=order))
        candidates = [r for r in group if not stale[id(r)] and profile.rank_index(r.rank) is not None]
        if not candidates:
            untrusted[name] = facts
            continue
        win = min(candidates, key=order)
        fields[name] = FieldLabel(
            value=win.value,
            status=RANK_STATUS[win.rank],
            presence=win.presence,
            source=win.source,
            reviewer=win.reviewer,
            rank=win.rank,
            layers_seen=facts,
        )

    verified = not reasons
    scorable = frozenset(
        name
        for name, label in fields.items()
        if verified
        and profile.field_spec(doc_class, name) is not None
        and label.status in profile.scorable_statuses
        and label.presence != "not_applicable"
    )
    return DocumentLabels(
        document_id=document_id,
        doc_class=doc_class,
        subclass=subclass,
        content_sha256=content,
        verified=verified,
        unverified_reasons=tuple(reasons),
        fields=fields,
        untrusted=untrusted,
        scorable_fields=scorable,
    )


def resolve(
    records: Iterable[LayerRecord],
    profile: ClientProfile,
    corpus_index: Mapping[str, CorpusEntry],
) -> tuple[DocumentLabels, ...]:
    """Resolve every document in the corpus index or in ``records``, sorted by id."""
    by_doc: dict[str, list[LayerRecord]] = defaultdict(list)
    for r in records:
        by_doc[r.document_id].append(r)
    return tuple(
        resolve_document(doc, by_doc.get(doc, ()), profile, corpus_index.get(doc))
        for doc in sorted(set(corpus_index) | set(by_doc))
    )
