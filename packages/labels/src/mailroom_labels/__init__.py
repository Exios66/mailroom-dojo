"""mailroom_labels: versioned, hash-checked label snapshots, their precedence and quality gate.

The public surface (plan decision 8) is re-exported here; the Dojo codes against these
names only.
"""

from __future__ import annotations

from mailroom_labels.labelset import LabelSet, QualityGateFailed, load_label_set
from mailroom_labels.layers import corpus_index, read_layer, read_layers
from mailroom_labels.models import CorpusEntry, DocumentLabels, FieldLabel, Finding, LayerFact
from mailroom_labels.profile import ClientProfile, load_profile
from mailroom_labels.resolve import resolve
from mailroom_labels.snapshot import (
    LabelSnapshot,
    label_snapshot_ids,
    load_snapshot,
    load_snapshot_set,
    snapshot_refs,
)

__version__ = "0.1.0"

__all__ = [
    "ClientProfile",
    "CorpusEntry",
    "DocumentLabels",
    "FieldLabel",
    "Finding",
    "LabelSet",
    "LabelSnapshot",
    "LayerFact",
    "QualityGateFailed",
    "corpus_index",
    "label_snapshot_ids",
    "load_label_set",
    "load_profile",
    "load_snapshot",
    "load_snapshot_set",
    "read_layer",
    "read_layers",
    "resolve",
    "snapshot_refs",
]
