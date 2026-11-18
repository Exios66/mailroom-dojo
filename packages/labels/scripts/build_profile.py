"""Generate ``config/clients/mailroom-dataset/profile.yaml`` from its sources. Never edit
the profile by hand: change a source (or the tables below) and re-run this script.

Sources (all local, read-only):

- the EDA field registry ``src/mailroom_eda/labeling/fields.yaml`` (classes, specialists,
  fields, Dojo field types, closed vocabularies) and its Dojo snapshot
  ``src/mailroom_eda/labeling/dojo_snapshot/dojo_schemas.json`` (``default_field_types``,
  which every registry type must equal, and ``corporate_subclass_schemas``, whose entity
  keys are corporate-record fields), at a recorded EDA commit;
- the Hub ``ground_truth`` parquet shards (verified against ``manifest.txt``), for the
  subclasses of every class and for the dataset's own labelled keys the registry does not
  name (``HUB_NATIVE_TYPES`` types them; an untyped one fails the build);
- optionally, overlay or review snapshot directories (``--covers``): every field they label
  must be in the profile, or the build fails.

The label precedence and the scorable statuses are fixed below (plan decision 1 and the
Task 2 decision on Claude audit decisions). A vocabulary the registry attaches to a list
field (``vocabulary_part``) is recorded as a comment only: the profile closes ``label``
fields alone.

    python packages/labels/scripts/build_profile.py --eda-dir <EDA checkout> \
        --eda-commit <40-hex> --hub-dir <fetch_hub.py output> \
        --covers labels/interim-2026-11-01 labels/near-gold-2026-11-17 \
        --out config/clients/mailroom-dataset/profile.yaml          # --check: compare only

Exit 0: written (or identical under ``--check``); 1: a source problem or a difference; 2: usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from mailroom_labels.importers.common import file_sha256
from mailroom_labels.importers.hub import SPLITS, parse_dataset_manifest, shard_path
from mailroom_labels.profile import parse_profile
from mailroom_labels.snapshot import load_snapshot

CLIENT = "mailroom-dataset"
DATASET = {"repository": "Lucius-Morningstar/mailroom-dataset", "revision": "ccd040f6a1b6b414bdefd161cdc622b68f6dacf8"}
EDA_REPOSITORY = "Exios66/Mailroom-Corpus-EDA"
FIELDS_YAML = "src/mailroom_eda/labeling/fields.yaml"
DOJO_SCHEMAS = "src/mailroom_eda/labeling/dojo_snapshot/dojo_schemas.json"

#: Highest first. Human desk decisions outrank everything; the Claude Opus audit decisions
#: (review:near_gold) outrank interim gap_fill / derived, a Hub absence and near_gold, never
#: a populated Hub value or an interim correction (Task 2 decision, 2026-11-18). Interim
#: fills replace a Hub absence (hub:absent) as the sandbox's apply_overlay does; near_gold
#: labels never do (apply_near_gold).
PRECEDENCE = [
    "review:gold",
    "interim:correction",
    "hub",
    "review:near_gold",
    "interim:gap_fill",
    "interim:derived",
    "hub:absent",
    "near_gold",
]
SCORABLE_STATUSES = ["hub", "interim", "near_gold", "gold"]

#: Keys the Hub labels (they carry a populated or documented-absent presence code) that the
#: EDA registry does not name, with their Dojo type. The CUAD and MAUD clause labels are the
#: experts' category -> spans objects, kept as the JSON text the Hub stores.
HUB_NATIVE_TYPES = {
    "content_topic": "label",
    "sentiment_label": "label",
    "cuad_clause_labels": "free_text",
    "maud_clause_labels": "free_text",
}
#: Fields the sandbox overlays label that neither the registry nor the Hub names.
OVERLAY_FIELDS = {
    "insurance_claim": {
        # interim `derived` label: the CMS source table, read from the row's own subclass
        # (sandbox interim_labels.CMS_TABLES, dojo issue #93).
        "cms_source_table": {"type": "label", "vocabulary": "cms_source_table"},
    },
}
OVERLAY_VOCABULARIES = {"cms_source_table": ["carrier", "inpatient", "outpatient", "pde"]}


class BuildError(Exception):
    pass


def _hub_rows(hub_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    import pyarrow.parquet as pq

    listed = parse_dataset_manifest((hub_dir / "manifest.txt").read_text(encoding="utf-8"))
    rows, files = [], []
    for split in SPLITS:
        rel = shard_path("ground_truth", split)
        digest = file_sha256(hub_dir / rel)
        if listed.get(rel) != digest:
            raise BuildError(f"{rel}: sha256 {digest} is not the one manifest.txt lists")
        files.append({"path": rel, "sha256": digest})
        rows += pq.read_table(hub_dir / rel).to_pylist()
    return rows, files


def _hub_facts(rows: list[dict[str, Any]]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    subclasses: dict[str, set[str]] = defaultdict(set)
    labelled: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        cls = row["expected"]
        if row.get("expected_subclass"):
            subclasses[cls].add(row["expected_subclass"])
        gt = json.loads(row["gt_fields"]) if row.get("gt_fields") else {}
        presence = json.loads(gt.get("gt_presence") or "{}")
        for key, code in presence.items():
            if code in ("populated", "schema_documented_absence"):
                labelled[cls].add(key)
    return subclasses, labelled


def build(eda_dir: Path, eda_commit: str, hub_dir: Path, covers: list[Path]) -> str:
    registry = yaml.safe_load((eda_dir / FIELDS_YAML).read_text(encoding="utf-8"))
    dojo = json.loads((eda_dir / DOJO_SCHEMAS).read_text(encoding="utf-8"))
    rows, hub_files = _hub_rows(hub_dir)
    subclasses, hub_labelled = _hub_facts(rows)
    problems: list[str] = []
    comments: list[str] = []

    vocabularies = {k: list(v) for k, v in sorted(registry["vocabularies"].items())}
    classes: dict[str, Any] = {}
    for cls, spec in sorted(registry["classes"].items()):
        default_types = dojo["default_field_types"].get(cls, {})
        fields: dict[str, dict[str, Any]] = {}
        for name, f in spec["fields"].items():
            if default_types.get(name) != f["type"]:
                problems.append(f"{cls}.{name}: registry type {f['type']!r} != Dojo {default_types.get(name)!r}")
            entry: dict[str, Any] = {"type": f["type"]}
            vocab = f.get("vocabulary")
            if vocab and f["type"] == "label":
                entry["vocabulary"] = vocab
            elif vocab:
                comments.append(f"{cls}.{name}: registry vocabulary {vocab!r} ({f.get('vocabulary_part')}) "
                                f"applies to a {f['type']} field and is not enforced here")
            fields[name] = entry
        if cls == "corporate_record":
            for sub, sub_spec in sorted(dojo["corporate_subclass_schemas"].items()):
                for ent in sub_spec.get("entities", []):
                    key, ftype = ent["key"], ent["type"]
                    if key in fields and fields[key]["type"] != ftype:
                        problems.append(f"{cls}.{key}: subclass {sub} types it {ftype!r}, already {fields[key]['type']!r}")
                    fields.setdefault(key, {"type": ftype})
        for key in sorted(hub_labelled.get(cls, set()) - set(fields)):
            if key not in HUB_NATIVE_TYPES:
                problems.append(f"{cls}.{key}: the Hub labels it but neither the registry nor HUB_NATIVE_TYPES types it")
                continue
            fields[key] = {"type": HUB_NATIVE_TYPES[key]}
        for key, entry in OVERLAY_FIELDS.get(cls, {}).items():
            fields.setdefault(key, dict(entry))
        classes[cls] = {
            "specialist": spec["specialist"],
            "subclasses": sorted(subclasses.get(cls, set())),
            "fields": dict(sorted(fields.items())),
        }
    for cls in sorted(set(subclasses) - set(classes)):
        problems.append(f"the Hub has class {cls!r}, which the registry does not define")
    vocabularies.update(OVERLAY_VOCABULARIES)

    profile = {
        "client": CLIENT,
        "dataset": {
            **DATASET,
            "hub_files": hub_files,
            "registry": {
                "repository": EDA_REPOSITORY,
                "commit": eda_commit,
                "files": [
                    {"path": FIELDS_YAML, "sha256": file_sha256(eda_dir / FIELDS_YAML)},
                    {"path": DOJO_SCHEMAS, "sha256": file_sha256(eda_dir / DOJO_SCHEMAS)},
                ],
                "dojo_version": dojo.get("dojo_version"),
                "dojo_commit": dojo.get("dojo_commit"),
            },
        },
        "precedence": PRECEDENCE,
        "scorable_statuses": SCORABLE_STATUSES,
        "vocabularies": dict(sorted(vocabularies.items())),
        "classes": classes,
    }
    parsed = parse_profile(json.loads(json.dumps(profile)))
    for directory in covers:
        snap = load_snapshot(directory)
        for raw in snap.rows():
            row = raw.data
            cls = row.get("doc_class")
            for name in sorted((row.get("labels") or {})):
                if parsed.field_spec(cls, name) is None:
                    problems.append(f"{snap.snapshot_id}: {cls}.{name} is not in the profile")
    if problems:
        raise BuildError("; ".join(sorted(set(problems))))

    header = [
        "# GENERATED by packages/labels/scripts/build_profile.py: do not edit by hand.",
        f"# Sources: {EDA_REPOSITORY}@{eda_commit} ({FIELDS_YAML}, {DOJO_SCHEMAS}),",
        f"# {DATASET['repository']}@{DATASET['revision']} ground_truth (subclasses, Hub-native fields).",
        "# Precedence: human desk decisions (review:gold) first; Claude audit decisions",
        "# (review:near_gold) outrank interim gap_fill/derived, a Hub absence and near_gold, never",
        "# a populated Hub value or an interim correction; interim fills replace a Hub absence.",
        *(f"# Note: {c}" for c in sorted(comments)),
    ]
    body = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=100)
    return "\n".join(header) + "\n" + body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the mailroom-dataset client profile.")
    parser.add_argument("--eda-dir", type=Path, required=True)
    parser.add_argument("--eda-commit", required=True)
    parser.add_argument("--hub-dir", type=Path, required=True, help="fetch_hub.py output (manifest.txt + parquet)")
    parser.add_argument("--covers", type=Path, nargs="*", default=[], help="snapshots whose fields must be covered")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="compare with --out instead of writing")
    args = parser.parse_args(argv)
    if len(args.eda_commit) != 40:
        print("--eda-commit must be the full 40-hex commit", file=sys.stderr)
        return 2
    try:
        text = build(args.eda_dir, args.eda_commit, args.hub_dir, args.covers)
    except BuildError as exc:
        print(f"profile not built: {exc}", file=sys.stderr)
        return 1
    if args.check:
        same = args.out.is_file() and args.out.read_text(encoding="utf-8") == text
        print("profile identical" if same else f"profile differs from a fresh build: {args.out}")
        return 0 if same else 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
