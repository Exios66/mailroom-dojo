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
  A key ``gt_fields`` lacks gets the value null (counted in ``presence_without_value``),
  except that a ``populated`` code with no value (missing or null) makes the row
  unreadable: it is listed in ``unparseable_gt_fields`` and the import is refused.
  So does any code that, after that mapping, is not a presence the Hub reader accepts
  (``layers.HUB_PRESENCES``: an unknown string, a number, a null, a list, ...), and a row
  the snapshot reader would refuse as a corrupt line (``snapshot.json_problem``: a value
  nested deeper than ``MAX_JSON_DEPTH``, or a string holding a lone surrogate).
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

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

from mailroom_labels.importers.common import (
    ImportRefused,
    builder_name,
    loads_source,
    read_source,
    refuse_dropped,
)
from mailroom_labels.layers import CURRENT_LAYER_SCHEMA, HUB_PRESENCES
from mailroom_labels.models import SnapshotManifest
from mailroom_labels.profile import ClientProfile, load_unique_yaml
from mailroom_labels.snapshot import json_problem, write_snapshot

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


_HEX40 = re.compile(r"^[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")  # \Z: "$" also matches before a final newline


def load_hub_pins(path: Path | str) -> dict[str, dict[str, str]]:
    """The pinned file hashes of each dataset revision from a YAML file
    (``{<40-hex revision>: {manifest.txt: <sha256>, <shard path>: <sha256>, ...}}``). A key
    repeated in the file is refused, so a second pin can never silently replace the first."""
    import yaml

    try:
        raw = load_unique_yaml(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ImportRefused(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise ImportRefused(f"{path}: expected {{revision: {{path: sha256}}}}")
    out: dict[str, dict[str, str]] = {}
    for revision, files in raw.items():
        if not (
            isinstance(revision, str)
            and _HEX40.match(revision)
            and isinstance(files, dict)
            and files
            and all(isinstance(k, str) and isinstance(v, str) and _HEX64.match(v) for k, v in files.items())
        ):
            raise ImportRefused(
                f"{path}: {revision!r} must be a 40-hex revision mapping paths to 64-hex sha256"
            )
        out[revision] = dict(files)
    return out


def _pinned(pinned: Mapping[str, str], rel: str, actual: str, revision: str) -> None:
    if pinned.get(rel) != actual:
        raise ImportRefused(
            f"{rel}: sha256 {actual} is not the one pinned for revision {revision} "
            f"({pinned.get(rel, 'not pinned')})"
        )


def _verified_shard(root: Path, rel: str, listed: dict[str, str]) -> tuple[dict[str, str], bytes]:
    """A shard's ``{path, sha256}`` and the bytes that hash was taken of (parse those)."""
    path = root / rel
    if not path.is_file():
        raise ImportRefused(f"missing shard {rel} under {root}")
    expected = listed.get(rel)
    if expected is None:
        raise ImportRefused(f"{rel} is not listed in manifest.txt")
    data, actual = read_source(path)
    if actual != expected:
        raise ImportRefused(f"{rel}: sha256 {actual} differs from manifest.txt {expected}")
    return {"path": rel, "sha256": actual}, data


def _read_parquet(data: bytes) -> list[dict[str, Any]]:
    import pyarrow as pa  # optional dependency: the [import] extra
    import pyarrow.parquet as pq

    return pq.read_table(pa.BufferReader(data)).to_pylist()


def _json_or_raw(raw: Any) -> Any:
    """A JSON string decoded (strictly: a repeated key, a non-finite number such as `NaN` or
    nesting too deep leaves it raw, so the caller reports it), anything else as it is."""
    if isinstance(raw, str):
        try:
            return loads_source(raw)
        except (ValueError, RecursionError):  # JSONDecodeError, a duplicate key and NaN are ValueErrors
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

    def add(self, row: _Tally) -> None:
        """Merge the counts of one kept row (its rejections are recorded directly)."""
        for cls, counts in row.omitted.items():
            self.omitted[cls].update(counts)
        for name in (
            "metadata",
            "mapped",
            "decoded",
            "undecoded",
            "presence_without_value",
            "not_applicable_nulled",
            "metadata_fields",
        ):
            getattr(self, name).update(getattr(row, name))

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


def _fields(
    row: dict[str, Any], profile: ClientProfile, tally: _Tally
) -> tuple[dict[str, dict[str, Any]], _Tally] | None:
    """The row's fields and its own counts (merged by the caller only when it keeps the row),
    or None when the row is refused (recorded in ``tally.bad_gt_fields``)."""
    doc_class = row.get("expected")
    gt = _json_or_raw(row.get("gt_fields"))
    if gt in (None, ""):
        gt = {}
    if not isinstance(gt, dict):
        tally.bad_gt_fields.append(
            {"document_id": row.get("document_id"), "reason": "gt_fields is not a JSON object"}
        )
        return None
    raw_presence = gt.get(PRESENCE_KEY)
    # Only an absent, null or empty-string gt_presence means "no presence codes"; any other
    # value that is not an object ("[]", "false", "0", ...) is reported, never read as empty.
    presence = {} if raw_presence in (None, "") else _json_or_raw(raw_presence)
    if not isinstance(presence, dict):
        tally.bad_gt_fields.append(
            {"document_id": row.get("document_id"), "reason": "gt_presence is not a JSON object"}
        )
        return None
    out: dict[str, dict[str, Any]] = {}
    # This row's counts stay local until the row is kept: a row refused part-way through
    # its keys must leave no count of its earlier keys in the manifest.
    kept = _Tally()
    for key in sorted((set(gt) | set(presence)) - {PRESENCE_KEY}):
        spec = profile.field_spec(doc_class, key)
        value = gt.get(key)
        if key in presence:
            code = presence[key]
            if code == "not_applicable" and spec is None:
                kept.omitted[str(doc_class)][key] += 1
                continue
            if isinstance(code, str) and code in _MAPPED_PRESENCE:
                kept.mapped[f"{code}->{_MAPPED_PRESENCE[code]}"] += 1
                code = _MAPPED_PRESENCE[code]
            if not (isinstance(code, str) and code in HUB_PRESENCES):
                # The reader refuses any other code as an unknown presence (a list code is
                # unhashable: the str check comes first), so the row is never written.
                tally.bad_gt_fields.append(
                    {
                        "document_id": row.get("document_id"),
                        "reason": f"gt_presence code {code!r} for {key!r} is not a known presence",
                    }
                )
                return None
            if code == "populated" and value is None:
                # The reader refuses a populated null, so the row is never written: it is
                # unreadable source data, like a gt_fields that is not an object.
                tally.bad_gt_fields.append(
                    {
                        "document_id": row.get("document_id"),
                        "reason": f"gt_presence says {key!r} is populated but gt_fields has no value",
                    }
                )
                return None
            if key not in gt:
                kept.presence_without_value[key] += 1
            if code == "not_applicable" and value in _EMPTY and value is not None:
                # The dataset fills a not-applicable field with an empty placeholder ("",
                # "[]", ...) of whatever type the column has; it carries no label. Stored as
                # null so the placeholder is never type- or vocabulary-checked as a value.
                kept.not_applicable_nulled[f"{key}={value!r}"] += 1
                value = None
        else:
            if spec is None:
                kept.metadata[key] += 1
                continue
            kept.metadata_fields[key] += 1
            code = "unlabeled" if value in _EMPTY else "populated"
        if spec is not None and spec.type.startswith("entity_list") and isinstance(value, str):
            decoded = _json_or_raw(value)
            if isinstance(decoded, list):
                value = decoded
                kept.decoded[key] += 1
            else:
                kept.undecoded[key] += 1
        out[key] = {"value": value, "presence": code}
    return out, kept


def _text_check(
    default_dir: Path, listed: dict[str, str], hub_rows: Sequence[dict[str, Any]]
) -> tuple[list, list, dict]:
    shards = [_verified_shard(default_dir, shard_path("default", s), listed) for s in SPLITS]
    files = [f for f, _ in shards]
    texts: dict[str, str | None] = {}
    duplicates = 0
    for _, data in shards:
        for row in _read_parquet(data):
            name = str(row.get("filename"))
            if name in texts:
                duplicates += 1
            text = row.get("doc_text")
            texts.setdefault(name, text if isinstance(text, str) else None)
    unverified = []
    reasons: Counter = Counter()
    for row in hub_rows:
        if row["filename"] not in texts:
            reasons["no_text_row"] += 1
            unverified.append(row["document_id"])
            continue
        text = texts[row["filename"]]
        if text is None:
            # a missing doc_text is never "the empty text": it cannot verify the hash
            reasons["no_doc_text"] += 1
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
    manifest_bytes, manifest_sha = read_source(manifest_txt)
    _pinned(pinned, "manifest.txt", manifest_sha, revision)
    listed = parse_dataset_manifest(manifest_bytes.decode("utf-8"))
    if not listed:
        raise ImportRefused("manifest.txt has no sha256 block")
    gt_shards = [_verified_shard(dataset_dir, shard_path("ground_truth", s), listed) for s in SPLITS]
    gt_files = [f for f, _ in gt_shards]
    for f in gt_files:
        _pinned(pinned, f["path"], f["sha256"], revision)

    tally = _Tally()
    rows: list[dict[str, Any]] = []
    for (_, data), split in zip(gt_shards, SPLITS, strict=True):
        for raw in _read_parquet(data):
            parsed = _fields(raw, profile, tally)
            if parsed is None:
                continue
            fields, kept = parsed
            row = {
                "document_id": raw.get("document_id"),
                "doc_class": raw.get("expected"),
                "subclass": _name_or_none(raw.get("expected_subclass")),
                "split": _name_or_none(raw.get("split")) or split,
                "content_sha256": _name_or_none(raw.get("content_sha256")),
                "filename": _name_or_none(raw.get("filename")),
                "fields": fields,
            }
            problem = json_problem(row)
            if problem is not None:
                # The reader would refuse the written line (CorruptLine): refuse the row here.
                tally.bad_gt_fields.append({"document_id": raw.get("document_id"), "reason": problem})
                continue
            tally.add(kept)
            rows.append(row)

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
        "dataset_manifest": {"path": "manifest.txt", "sha256": manifest_sha},
        "files": gt_files + text_files,
        "profile": {"client": profile.client, "sha256": profile_sha256},
        "builder": {
            "name": "mailroom_labels.importers.hub",
            "version": builder_name("import-hub").split()[-1],
        },
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
