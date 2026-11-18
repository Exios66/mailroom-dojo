"""Import the Hub ``ground_truth`` config of the dataset into a ``hub`` snapshot.

Inputs (all local; ``scripts/fetch_hub.py`` downloads them):

- ``dataset_dir``: holds the dataset's ``manifest.txt`` and the ``ground_truth`` parquet
  shards at ``parquet/ground_truth/<split>/<split>-00000-of-00001.parquet``.
- ``default_dir`` (optional): holds the ``default`` shards (``doc_text``) under the same
  layout, for the text check.
- the client profile, which says which keys of ``gt_fields`` are fields of each class and
  which fields are lists.

Every parquet file is checked against the sha256 that ``manifest.txt`` publishes before a
row is read (``ImportRefused`` otherwise).

The mapping reproduces how the sandbox turns a Hub row into ``expected_fields`` and
presence codes (``corpus.normalize_rows``: ``expected_fields`` is the parsed ``gt_fields``;
``interim_labels._presence``: presence comes from the ``gt_presence`` JSON inside it):

- A key ``gt_presence`` lists becomes a field with that presence code and the raw
  ``gt_fields`` value. ``pending_annotation`` (the dataset's "not labelled yet") becomes
  ``unlabeled``, which yields no label (``hub_has`` treats it as unlabelled too).
- A ``not_applicable`` key that is **not a field of the document's class in the profile**
  (the dataset carries every class's keys on every row as placeholders) is omitted:
  omitted means not_applicable. A ``not_applicable`` key the class does define is kept.
- A key ``gt_presence`` does not list is row metadata (token estimates, confidences,
  evidence notes) unless the profile names it as a field of the class; then its presence is
  the sandbox's ``hub_has`` rule: ``populated`` when the value is non-empty, else
  ``unlabeled``.
- A kept ``not_applicable`` field whose value is an empty placeholder (``""``, ``"[]"``, ...)
  is stored with value null: the placeholder is not a label. A non-empty value is kept.
- A list field (profile type ``entity_list*``) stored as a JSON string is decoded to the
  list; a string that is not a JSON list is kept as it is (the quality gate then reports a
  ``type_mismatch``). No other value is changed.

Every omission, mapping and decoding is counted in the manifest ``source``. The text check
hashes every ``doc_text`` (UTF-8) and compares it with ``content_sha256``; documents that do
not verify (or have no text row) are listed in ``source.text_unverified_documents``.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

from mailroom_labels.importers.common import ImportRefused, builder_name, file_sha256, loads_source, refuse_dropped
from mailroom_labels.layers import CURRENT_LAYER_SCHEMA
from mailroom_labels.models import SnapshotManifest
from mailroom_labels.profile import ClientProfile
from mailroom_labels.snapshot import write_snapshot

__all__ = ["DEFAULT_SHARDS", "SPLITS", "build_hub", "load_hub_pins", "parse_dataset_manifest", "shard_path"]

SPLITS: tuple[str, ...] = ("train", "test")
PRESENCE_KEY = "gt_presence"
_MAPPED_PRESENCE = {"pending_annotation": "unlabeled"}
_EMPTY = (None, "", [], {}, "nan", "[]", "{}")


def shard_path(config: str, split: str) -> str:
    """The dataset-relative path of one parquet shard."""
    return f"parquet/{config}/{split}/{split}-00000-of-00001.parquet"


#: What ``scripts/fetch_hub.py`` downloads by default: the shards ``build_hub`` reads.
DEFAULT_SHARDS: tuple[str, ...] = tuple(shard_path(c, s) for c in ("ground_truth", "default") for s in SPLITS)


def parse_dataset_manifest(text: str) -> dict[str, str]:
    """``{path: sha256}`` from the ``sha256 :`` block of the dataset's ``manifest.txt``."""
    out: dict[str, str] = {}
    in_block = False
    for line in text.splitlines():
        if line.split(":")[0].strip() == "sha256" and line.rstrip().endswith(":"):
            in_block = True
            continue
        if not in_block:
            continue
        parts = line.split()
        if len(parts) == 2 and line[:1].isspace() and len(parts[1]) == 64:
            out[parts[0]] = parts[1]
        elif line.strip():
            in_block = False
    return out


_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def load_hub_pins(path: Path | str) -> dict[str, dict[str, str]]:
    """The pinned file hashes of each dataset revision from a YAML file
    (``{<40-hex revision>: {manifest.txt: <sha256>, <shard path>: <sha256>, ...}}``)."""
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ImportRefused(f"{path}: expected {{revision: {{path: sha256}}}}")
    out: dict[str, dict[str, str]] = {}
    for revision, files in raw.items():
        if not (isinstance(revision, str) and _HEX40.match(revision) and isinstance(files, dict) and files
                and all(isinstance(k, str) and isinstance(v, str) and _HEX64.match(v) for k, v in files.items())):
            raise ImportRefused(f"{path}: {revision!r} must be a 40-hex revision mapping paths to 64-hex sha256")
        out[revision] = dict(files)
    return out


def _pinned(pinned: Mapping[str, str], rel: str, actual: str, revision: str) -> None:
    if pinned.get(rel) != actual:
        raise ImportRefused(f"{rel}: sha256 {actual} is not the one pinned for revision {revision} "
                            f"({pinned.get(rel, 'not pinned')})")


def _verified_shard(root: Path, rel: str, listed: dict[str, str]) -> dict[str, str]:
    path = root / rel
    if not path.is_file():
        raise ImportRefused(f"missing shard {rel} under {root}")
    expected = listed.get(rel)
    if expected is None:
        raise ImportRefused(f"{rel} is not listed in manifest.txt")
    actual = file_sha256(path)
    if actual != expected:
        raise ImportRefused(f"{rel}: sha256 {actual} differs from manifest.txt {expected}")
    return {"path": rel, "sha256": actual}


def _read_parquet(path: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq  # optional dependency: the [import] extra

    return pq.read_table(path).to_pylist()


def _json_or_raw(raw: Any) -> Any:
    """A JSON string decoded (strictly: a repeated key or nesting too deep leaves it raw, so
    the caller reports it), anything else as it is."""
    if isinstance(raw, str):
        try:
            return loads_source(raw)
        except (ValueError, RecursionError):  # JSONDecodeError and a duplicate key are ValueErrors
            return raw
    return raw


def _name_or_none(value: Any) -> Any:
    return None if value in (None, "") else value


class _Tally:
    def __init__(self) -> None:
        self.omitted: dict[str, Counter] = defaultdict(Counter)
        self.metadata: Counter = Counter()
        self.mapped: Counter = Counter()
        self.decoded: Counter = Counter()
        self.undecoded: Counter = Counter()
        self.presence_without_value: Counter = Counter()
        self.not_applicable_nulled: Counter = Counter()
        self.metadata_fields: Counter = Counter()
        self.bad_gt_fields: list[dict[str, Any]] = []

    def source(self) -> dict[str, Any]:
        def nested(d: dict[str, Counter]) -> dict[str, dict[str, int]]:
            return {k: dict(sorted(v.items())) for k, v in sorted(d.items())}

        return {
            "omitted_not_applicable_placeholders": nested(self.omitted),
            "excluded_metadata_keys": dict(sorted(self.metadata.items())),
            "presence_mapped": dict(sorted(self.mapped.items())),
            "decoded_json_lists": dict(sorted(self.decoded.items())),
            "undecodable_list_values": dict(sorted(self.undecoded.items())),
            "presence_without_value": dict(sorted(self.presence_without_value.items())),
            "not_applicable_placeholders_nulled": dict(sorted(self.not_applicable_nulled.items())),
            "fields_without_presence": dict(sorted(self.metadata_fields.items())),
            "unparseable_gt_fields": self.bad_gt_fields,
        }


def _fields(row: dict[str, Any], profile: ClientProfile, tally: _Tally) -> dict[str, dict[str, Any]] | None:
    doc_class = row.get("expected")
    gt = _json_or_raw(row.get("gt_fields"))
    if gt in (None, ""):
        gt = {}
    if not isinstance(gt, dict):
        tally.bad_gt_fields.append({"document_id": row.get("document_id"), "reason": "gt_fields is not a JSON object"})
        return None
    presence = _json_or_raw(gt.get(PRESENCE_KEY)) or {}
    if not isinstance(presence, dict):
        tally.bad_gt_fields.append({"document_id": row.get("document_id"), "reason": "gt_presence is not a JSON object"})
        return None
    out: dict[str, dict[str, Any]] = {}
    for key in sorted((set(gt) | set(presence)) - {PRESENCE_KEY}):
        spec = profile.field_spec(doc_class, key)
        value = gt.get(key)
        if key in presence:
            code = presence[key]
            if code == "not_applicable" and spec is None:
                tally.omitted[str(doc_class)][key] += 1
                continue
            if code in _MAPPED_PRESENCE:
                tally.mapped[f"{code}->{_MAPPED_PRESENCE[code]}"] += 1
                code = _MAPPED_PRESENCE[code]
            if key not in gt:
                tally.presence_without_value[key] += 1
            if code == "not_applicable" and value in _EMPTY and value is not None:
                # The dataset fills a not-applicable field with an empty placeholder ("",
                # "[]", ...) of whatever type the column has; it carries no label. Stored as
                # null so the placeholder is never type- or vocabulary-checked as a value.
                tally.not_applicable_nulled[f"{key}={value!r}"] += 1
                value = None
        else:
            if spec is None:
                tally.metadata[key] += 1
                continue
            tally.metadata_fields[key] += 1
            code = "unlabeled" if value in _EMPTY else "populated"
        if spec is not None and spec.type.startswith("entity_list") and isinstance(value, str):
            decoded = _json_or_raw(value)
            if isinstance(decoded, list):
                value = decoded
                tally.decoded[key] += 1
            else:
                tally.undecoded[key] += 1
        out[key] = {"value": value, "presence": code}
    return out


def _text_check(default_dir: Path, listed: dict[str, str], hub_rows: Sequence[dict[str, Any]]) -> tuple[list, list, dict]:
    files = [_verified_shard(default_dir, shard_path("default", s), listed) for s in SPLITS]
    texts: dict[str, str] = {}
    duplicates = 0
    for f in files:
        for row in _read_parquet(default_dir / f["path"]):
            name = str(row.get("filename"))
            if name in texts:
                duplicates += 1
            texts.setdefault(name, row.get("doc_text") or "")
    unverified = []
    reasons: Counter = Counter()
    for row in hub_rows:
        text = texts.get(row["filename"])
        if text is None:
            reasons["no_text_row"] += 1
            unverified.append(row["document_id"])
        elif row["content_sha256"] is None:
            reasons["no_content_sha256"] += 1
            unverified.append(row["document_id"])
        elif sha256(text.encode("utf-8")).hexdigest() != row["content_sha256"]:
            reasons["hash_mismatch"] += 1
            unverified.append(row["document_id"])
    summary = {
        "checked": len(hub_rows),
        "verified": len(hub_rows) - len(unverified),
        "unverified": len(unverified),
        "unverified_by_reason": dict(sorted(reasons.items())),
        "duplicate_text_filenames": duplicates,
        "text_rows": len(texts),
    }
    return files, sorted(unverified), summary


def build_hub(
    out_dir: Path | str,
    *,
    dataset_dir: Path | str,
    profile: ClientProfile,
    profile_sha256: str,
    repository: str,
    revision: str,
    default_dir: Path | str | None = None,
    snapshot_id: str | None = None,
    known_warnings: Sequence[Mapping[str, str | None]] = (),
    allow_rejected: bool = False,
    pins: Mapping[str, Mapping[str, str]],
) -> SnapshotManifest:
    """Write the Hub snapshot to ``out_dir`` (see the module docstring); return its manifest.

    ``pins`` (``load_hub_pins``) records the sha256 of ``manifest.txt`` and of every shard at
    each known revision: an import whose files are not exactly those pinned for ``revision``
    is refused, so files from another revision can never be recorded under this one."""
    out_dir, dataset_dir = Path(out_dir), Path(dataset_dir)
    pinned = pins.get(revision)
    if not pinned:
        raise ImportRefused(f"revision {revision} has no pinned file hashes")
    manifest_txt = dataset_dir / "manifest.txt"
    if not manifest_txt.is_file():
        raise ImportRefused(f"no manifest.txt in {dataset_dir}")
    _pinned(pinned, "manifest.txt", file_sha256(manifest_txt), revision)
    listed = parse_dataset_manifest(manifest_txt.read_text(encoding="utf-8"))
    if not listed:
        raise ImportRefused("manifest.txt has no sha256 block")
    gt_files = [_verified_shard(dataset_dir, shard_path("ground_truth", s), listed) for s in SPLITS]
    for f in gt_files:
        _pinned(pinned, f["path"], f["sha256"], revision)

    tally = _Tally()
    rows: list[dict[str, Any]] = []
    for f, split in zip(gt_files, SPLITS):
        for raw in _read_parquet(dataset_dir / f["path"]):
            fields = _fields(raw, profile, tally)
            if fields is None:
                continue
            rows.append({
                "document_id": raw.get("document_id"),
                "doc_class": raw.get("expected"),
                "subclass": _name_or_none(raw.get("expected_subclass")),
                "split": _name_or_none(raw.get("split")) or split,
                "content_sha256": _name_or_none(raw.get("content_sha256")),
                "filename": _name_or_none(raw.get("filename")),
                "fields": fields,
            })

    refuse_dropped({"unparseable_gt_fields": tally.bad_gt_fields}, allow=allow_rejected)

    text_files: list = []
    unverified: list = []
    text_summary: dict[str, Any] = {"run": False}
    if default_dir is not None:
        text_files, unverified, summary = _text_check(Path(default_dir), listed, rows)
        for f in text_files:
            _pinned(pinned, f["path"], f["sha256"], revision)
        text_summary = {"run": True, **summary}

    sid = snapshot_id or out_dir.name
    source = {
        "repository": repository,
        "revision": revision,
        "dataset_manifest": {"path": "manifest.txt", "sha256": file_sha256(manifest_txt)},
        "files": gt_files + text_files,
        "profile": {"client": profile.client, "sha256": profile_sha256},
        "builder": {"name": "mailroom_labels.importers.hub", "version": builder_name("import-hub").split()[-1]},
        "rows": len(rows),
        "text_check": text_summary,
        "text_unverified_documents": unverified,
        "omitted_means": "not_applicable",
        **tally.source(),
    }
    return write_snapshot(
        out_dir,
        {"documents.jsonl": rows},
        sort_key=lambda r: (str(r["document_id"]), str(r["filename"])),
        snapshot_id=sid,
        kind="hub",
        layer_schema=CURRENT_LAYER_SCHEMA["hub"],
        source=source,
        known_warnings=known_warnings,
        builder=builder_name("import-hub"),
    )
