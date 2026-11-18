"""Import a sandbox label overlay (``mailroom-interim-labels/v1``) into a snapshot.

The interim overlay (``data/labels/interim/``) and the near-gold overlay
(``data/labels/gold-overlay/``) share one native shape: one JSON object per document per
line, in one file per class, with a ``MANIFEST.json`` that lists every file's sha256. The
importer:

1. reads ``MANIFEST.json`` and refuses (``ImportRefused``, nothing written) when its
   ``schema`` is not the overlay schema, a listed file is missing or has another sha256,
   or a ``.jsonl`` file sits in the directory without being listed;
2. copies every record **unchanged** (native keys, statuses, ``basis``, ``support``,
   ``label_id``, ``labeler``, ``evidence``: the history of who labelled what) into a file of
   the same name, sorted by document id;
3. lists every line that is not one JSON object under ``source.rejected_lines`` instead of
   writing it.

Statuses are never rewritten here: an interim ``disputed`` label stays ``disputed`` with
``use_as_truth`` false, and the layer reader keeps it out of the truth.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mailroom_labels.importers.common import ImportRefused, builder_name, file_sha256, read_jsonl_lenient
from mailroom_labels.layers import CURRENT_LAYER_SCHEMA
from mailroom_labels.models import SnapshotManifest
from mailroom_labels.snapshot import write_snapshot

__all__ = ["OVERLAY_SCHEMA", "build_overlay"]

OVERLAY_SCHEMA = "mailroom-interim-labels/v1"
_MANIFEST = "MANIFEST.json"
#: Keys of the source MANIFEST.json copied into the snapshot manifest's ``source``.
_KEPT_KEYS = ("schema", "status", "built_at", "rederived_at", "builder", "gate_run",
              "catalog_manifest_sha256", "rules", "sources", "superseded_by")


def _status_key(label: Any) -> str:
    if not isinstance(label, dict):
        return "(not an object)"
    basis = label.get("basis")
    if isinstance(basis, str) and basis.startswith("near_gold:"):
        return basis
    return str(label.get("status"))


def build_overlay(
    out_dir: Path | str,
    *,
    kind: str,
    source_dir: Path | str,
    source_path: str,
    repository: str,
    commit: str,
    command: str,
    snapshot_id: str | None = None,
    known_warnings: Sequence[Mapping[str, str | None]] = (),
) -> SnapshotManifest:
    """Write an ``interim`` or ``near_gold`` snapshot from a sandbox overlay directory.

    ``source_path`` is how the directory is named in the manifest (its path inside
    ``repository`` at ``commit``), never a local path.
    """
    if kind not in ("interim", "near_gold"):
        raise ValueError(f"an overlay snapshot is interim or near_gold, not {kind!r}")
    out_dir, source_dir = Path(out_dir), Path(source_dir)
    man_path = source_dir / _MANIFEST
    if not man_path.is_file():
        raise ImportRefused(f"no {_MANIFEST} in {source_dir}")
    try:
        man = json.loads(man_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ImportRefused(f"{_MANIFEST} is not JSON: {exc}") from exc
    if not isinstance(man, dict) or man.get("schema") != OVERLAY_SCHEMA:
        raise ImportRefused(f"{_MANIFEST} schema is not {OVERLAY_SCHEMA!r}")
    listed = man.get("files")
    if not isinstance(listed, dict) or not listed:
        raise ImportRefused(f"{_MANIFEST} lists no files")
    unlisted = sorted(p.name for p in source_dir.glob("*.jsonl") if p.name not in listed)
    if unlisted:
        raise ImportRefused(f"unlisted overlay file(s): {', '.join(unlisted)}")

    files: dict[str, list[dict[str, Any]]] = {}
    file_facts = []
    rejected: list[dict[str, Any]] = []
    counts: dict[str, Counter] = defaultdict(Counter)
    for name in sorted(listed):
        path = source_dir / name
        if not (isinstance(name, str) and name.endswith(".jsonl") and "/" not in name and path.is_file()):
            raise ImportRefused(f"listed file {name!r} is missing or not a .jsonl file")
        actual = file_sha256(path)
        if actual != listed[name]:
            raise ImportRefused(f"{name}: sha256 {actual} differs from {_MANIFEST} {listed[name]}")
        rows, bad = read_jsonl_lenient(path, name)
        rejected.extend(bad)
        files[name] = [r.data for r in rows]
        file_facts.append({"path": f"{source_path}/{name}", "sha256": actual, "rows": len(rows)})
        for r in rows:
            labels = r.data.get("labels")
            for label in (labels.values() if isinstance(labels, dict) else ()):
                counts[name[: -len(".jsonl")]][_status_key(label)] += 1

    source = {
        "repository": repository,
        "commit": commit,
        "path": source_path,
        "source_manifest": {"path": f"{source_path}/{_MANIFEST}", "sha256": file_sha256(man_path)},
        "files": file_facts,
        "builder": {"name": f"mailroom_labels.importers.{kind}", "version": builder_name(command).split()[-1]},
        "native": {k: man[k] for k in _KEPT_KEYS if k in man},
        "label_counts": {k: dict(sorted(v.items())) for k, v in sorted(counts.items())},
        "rejected_lines": rejected,
    }
    return write_snapshot(
        out_dir,
        files,
        sort_key=lambda r: (str(r.get("doc_id")), json.dumps(r, sort_keys=True)),
        snapshot_id=snapshot_id or out_dir.name,
        kind=kind,
        layer_schema=CURRENT_LAYER_SCHEMA[kind],
        source=source,
        known_warnings=known_warnings,
        builder=builder_name(command),
    )
