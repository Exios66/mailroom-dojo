"""Import the sandbox near-gold overlay (``data/labels/gold-overlay/``) as a ``near_gold``
snapshot.

Each label keeps ``label_id``, ``labeler`` (model, role, batch), ``evidence`` spans (quotes
from the public corpus documents, as the source carries them), ``absent_reason``,
``presence`` and ``basis`` (``near_gold:two_pass_agree`` or ``near_gold:opus_reviewed_*``).
See ``overlay.py`` for the checks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from mailroom_labels.importers.overlay import build_overlay
from mailroom_labels.models import SnapshotManifest

__all__ = ["build_near_gold"]


def build_near_gold(out_dir: Path | str, *, source_dir: Path | str, source_path: str, repository: str,
                    commit: str, snapshot_id: str | None = None,
                    known_warnings: Sequence[Mapping[str, str | None]] = (),
                    allow_rejected: bool = False) -> SnapshotManifest:
    """Write the near-gold snapshot (see ``overlay.build_overlay``)."""
    return build_overlay(out_dir, kind="near_gold", source_dir=source_dir, source_path=source_path,
                         repository=repository, commit=commit, command="import-near-gold",
                         snapshot_id=snapshot_id, known_warnings=known_warnings,
                         allow_rejected=allow_rejected)
