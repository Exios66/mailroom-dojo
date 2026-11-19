"""Importers: pure functions from explicit local source files to label snapshots.

``hub`` (the dataset's ``ground_truth`` config), ``interim`` and ``near_gold`` (the sandbox
overlays) and ``review`` (review-desk and audit decisions). None of them touches the network
(``scripts/fetch_hub.py`` downloads the Hub files); each is deterministic and records its
sources (repository, revision or commit, file paths and sha256) and builder in the manifest.
``check_snapshot`` re-derives a snapshot and compares it byte for byte with the committed one.
"""

from __future__ import annotations

from mailroom_labels.importers.common import CheckResult, ImportRefused, check_snapshot
from mailroom_labels.importers.hub import build_hub
from mailroom_labels.importers.interim import build_interim
from mailroom_labels.importers.near_gold import build_near_gold
from mailroom_labels.importers.review import build_review

__all__ = [
    "CheckResult",
    "ImportRefused",
    "build_hub",
    "build_interim",
    "build_near_gold",
    "build_review",
    "check_snapshot",
]
