"""Import the sandbox interim overlay (``data/labels/interim/``) as an ``interim`` snapshot.

Each label keeps its native ``status`` (``gap_fill``, ``derived``, ``correction`` or
``disputed``), ``basis``, ``support`` and ``use_as_truth``, and each record its ``source``
(gate run, extraction run, question set, verdict). See ``overlay.py`` for the checks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from mailroom_labels.importers.overlay import build_overlay
from mailroom_labels.models import SnapshotManifest

__all__ = ["build_interim"]


def build_interim(
    out_dir: Path | str,
    *,
    source_dir: Path | str,
    source_path: str,
    repository: str,
    commit: str,
    snapshot_id: str | None = None,
    known_warnings: Sequence[Mapping[str, str | None]] = (),
    allow_rejected: bool = False,
) -> SnapshotManifest:
    """Write the interim snapshot (see ``overlay.build_overlay``)."""
    return build_overlay(
        out_dir,
        kind="interim",
        source_dir=source_dir,
        source_path=source_path,
        repository=repository,
        commit=commit,
        command="import-interim",
        snapshot_id=snapshot_id,
        known_warnings=known_warnings,
        allow_rejected=allow_rejected,
    )
