"""Derive a review snapshot whose notes name the reviewing role instead of a person's initials.

A committed snapshot is never edited in place, so this writes a **new** snapshot from a
committed review snapshot. Every row is copied unchanged except its ``note``: a note that
starts ``top-reply rule (<initials> <date>)`` (two to four capital letters) starts
``top-reply rule (review desk <date>)`` instead. Nothing else in any row changes, the rows
keep the importer's order (``review_row_order``), and the derivation refuses (exit 1,
nothing written) when a note would still hold an initials-and-date token afterwards.

The new manifest keeps the source manifest's ``source`` (the original provenance) as it is
and adds ``source.derived_from``: the source snapshot id, its ``manifest_hash``, its files
(path, sha256, rows), the change made and how many notes it changed. ``known_warnings``
come from ``--known-warnings`` under the new snapshot id, as for an import.

    python packages/labels/scripts/derive_review_snapshot.py \\
        --source <the gold-review-2026-11-18 snapshot directory> \\
        --out labels/gold-review-2026-11-18-r2 \\
        --known-warnings config/clients/mailroom-dataset/known-warnings.yaml   # --check: compare only

``--source`` is the snapshot directory itself (the one holding its ``manifest.json``);
neither directory may lie inside the other. An existing ``--out`` is replaced only when it
is an earlier derivation of the same source (a review snapshot of the derived id whose
``source.derived_from.snapshot_id`` is the source's id); anything else is refused untouched.
The source snapshot is not kept beside its derivation; for ``--check``, extract it from
commit ``4ca36109f8a2098b530bfdc52fd802fb620bd81b``, which holds it exactly as it was added
(reachable from ``refs/pull/5/head``; ``git fetch origin refs/pull/5/head`` when it is not
local):

    mkdir <scratch> && git archive 4ca36109f8a2098b530bfdc52fd802fb620bd81b \\
        labels/gold-review-2026-11-18 | tar -x -C <scratch>

and pass ``--source <scratch>/labels/gold-review-2026-11-18``. A different source changes
``derived_from`` and so the manifest.
Exit 0: written (or identical under ``--check``); 1: refused or different; 2: usage (a
missing or unreadable ``--known-warnings`` file, with a one-line message).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from mailroom_labels.errors import LabelsError
from mailroom_labels.importers.common import (
    BUILDER_VERSION,
    ImportRefused,
    check_snapshot,
    known_warnings_for,
)
from mailroom_labels.importers.review import review_row_order
from mailroom_labels.models import SnapshotManifest
from mailroom_labels.snapshot import MANIFEST_NAME, load_snapshot, write_snapshot

BUILDER = f"packages/labels/scripts/derive_review_snapshot.py {BUILDER_VERSION}"
ROLE = "review desk"
#: A leading ``top-reply rule (<initials> <date>)``; group 1 is the initials, group 2 the date.
LEADING_INITIALS = re.compile(r"^top-reply rule \(([A-Z]{2,4}) (\d{4}-\d{2}-\d{2})\)")
#: An initials-and-date token anywhere: none may be left after the derivation.
ANY_INITIALS = re.compile(r"\b[A-Z]{2,4} \d{4}-\d{2}-\d{2}\b")
CHANGE = (
    "note only: a leading 'top-reply rule (<initials> <date>)' now reads "
    f"'top-reply rule ({ROLE} <date>)'; every other key and value of every row is unchanged"
)


def derive_note(note: Any) -> tuple[Any, bool]:
    """The derived note and whether it changed. A non-string note is kept as it is."""
    if not isinstance(note, str):
        return note, False
    new, count = LEADING_INITIALS.subn(lambda m: f"top-reply rule ({ROLE} {m.group(2)})", note, count=1)
    return new, count == 1


def _derived_from(out_dir: Path) -> Any:
    """The ``source.derived_from.snapshot_id`` of the snapshot already at ``out_dir``, or
    None when it has none (or its manifest cannot be read as one)."""
    try:
        manifest = json.loads((out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    source = manifest.get("source") if isinstance(manifest, dict) else None
    derived = source.get("derived_from") if isinstance(source, dict) else None
    return derived.get("snapshot_id") if isinstance(derived, dict) else None


def derive(source_dir: Path, out_dir: Path, known_warnings: Path | None) -> SnapshotManifest:
    """Write the derived snapshot to ``out_dir`` (its name is the new snapshot id)."""
    src, out = source_dir.resolve(), out_dir.resolve()
    if out.is_relative_to(src) or src.is_relative_to(out):
        raise ImportRefused(f"{out_dir} and {source_dir}: one lies inside the other")
    source = load_snapshot(source_dir)
    if source.kind != "review":
        raise ImportRefused(f"{source_dir}: a {source.kind} snapshot, not a review snapshot")
    snapshot_id = out_dir.name
    if snapshot_id == source.snapshot_id:
        raise ImportRefused(f"{out_dir}: the derived snapshot needs its own id, not {snapshot_id!r}")
    if "derived_from" in source.manifest.source:
        raise ImportRefused(f"{source_dir}: source.derived_from is already set")
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        found = _derived_from(out_dir)
        if found != source.snapshot_id:
            # write_snapshot also refuses another id or kind; this refuses a snapshot of the
            # same id and kind that is not a derivation of this source.
            raise ImportRefused(
                f"{out_dir}: holds a snapshot whose source.derived_from is {found!r}, "
                f"not {source.snapshot_id!r}; refusing to replace it"
            )
    files: dict[str, list[dict[str, Any]]] = {entry.path: [] for entry in source.manifest.files}
    changed = 0
    for row in source.rows():
        data = dict(row.data)
        if "note" in data:
            data["note"], did = derive_note(data["note"])
            changed += did
        left = ANY_INITIALS.search(data["note"]) if isinstance(data.get("note"), str) else None
        if left:
            raise ImportRefused(f"{source_dir}/{row.file}:{row.line}: a note still holds an initials token")
        files[row.file].append(data)
    for rel, rows in files.items():  # the importer's order, so writing keeps every row in place
        if rows != sorted(rows, key=review_row_order):
            raise ImportRefused(f"{source_dir}/{rel}: rows are not in review_row_order")
    provenance = {
        **dict(source.manifest.source),
        "derived_from": {
            "snapshot_id": source.snapshot_id,
            "manifest_hash": source.manifest_hash,
            "files": [{"path": f.path, "sha256": f.sha256, "rows": f.rows} for f in source.manifest.files],
            "change": CHANGE,
            "notes_changed": changed,
        },
    }
    return write_snapshot(
        out_dir,
        files,
        sort_key=review_row_order,
        snapshot_id=snapshot_id,
        kind="review",
        layer_schema=source.manifest.layer_schema,
        source=provenance,
        builder=BUILDER,
        known_warnings=known_warnings_for(known_warnings, snapshot_id),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, required=True, help="the committed review snapshot")
    parser.add_argument("--out", type=Path, required=True, help="the derived snapshot (its name is the id)")
    parser.add_argument("--known-warnings", type=Path, help="known-warnings YAML, read under the new id")
    parser.add_argument("--check", action="store_true", help="compare a fresh derivation with --out")
    args = parser.parse_args(argv)
    if args.known_warnings is not None:
        try:
            args.known_warnings.read_bytes()
        except OSError as exc:
            print(f"--known-warnings {args.known_warnings}: cannot read: {exc.strerror}", file=sys.stderr)
            return 2
    try:
        if args.check:
            result = check_snapshot(args.out, lambda out: derive(args.source, out, args.known_warnings))
            for line in result.differences:
                print(line, file=sys.stderr)
            print("identical" if result.identical else f"{len(result.differences)} difference(s)")
            return 0 if result.identical else 1
        manifest = derive(args.source, args.out, args.known_warnings)
    except (LabelsError, FileExistsError, ValueError) as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 1
    notes = manifest.source["derived_from"]
    changed = notes.get("notes_changed") if isinstance(notes, dict) else None
    print(f"wrote {args.out} ({manifest.snapshot_id}@{manifest.manifest_hash}, {changed} note(s) changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
