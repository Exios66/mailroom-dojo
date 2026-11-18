"""Compare the shipped dictionaries with the llm-mailroom and llm-dojo-scoring checkouts they mirror.

Usage: python scripts/check_against_sources.py --mailroom PATH --dojo PATH [--write-fixture]

Run by hand (it needs both checkouts; CI has neither). It reads llm-mailroom's
``src/config/taxonomy.yaml``, ``src/pipeline/reconsideration.py`` and ``src/schemas/manifest.py``
(parsed, never imported) and imports ``llm_dojo_scoring`` from the Dojo checkout. It reports:

- source values per dictionary (keys, in order where the source orders them) that differ from
  the shipped keys;
- doc_classes labels, descriptions, subclasses and closed label fields that differ from the
  taxonomy and the Dojo's ``DOC_TYPE_SUBCLASSES`` / ``SCORED_ENUM_FIELDS``;
- CUAD and MAUD labels that differ from the Dojo's config;
- every surface (key, label, alias and a few spellings of each) that ``Dictionary.normalize``
  and the Dojo's ``scored_enums.canonical_label`` resolve differently, except the deliberate
  differences listed in ``DELIBERATE``.

``--write-fixture`` also writes ``packages/dictionaries/tests/fixtures/source_values.json``
(the source values and both commit SHAs), which a test compares with the shipped dictionaries.
Exit status: 0 no differences, 1 differences found, 2 bad usage or unreadable checkout.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from mailroom_dictionaries import DictionarySet, shipped_dictionaries

__all__ = ["DELIBERATE", "FIXTURE", "label_differences", "main", "source_values", "spellings"]

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "dictionaries"
    / "tests"
    / "fixtures"
    / "source_values.json"
)

#: (dictionary, key, label) whose label this package resolves but the Dojo's scorer does not.
#: Labels are display names; a scored answer is the key. Kept explicit so a new one is noticed.
DELIBERATE = frozenset(
    {
        ("merger_consideration", "other", "Other / Unspecified"),
        ("claim_type", "workers_comp", "Workers' compensation"),
    }
)


def spellings(surface: str) -> set[str]:
    """The surface and a few ASCII spellings both folds must treat alike."""
    return {
        surface,
        surface + ".",
        surface.upper(),
        surface.title(),
        surface.replace("_", " "),
        surface.replace(" ", "/"),
        f'"{surface}"',
        f"  {surface}\t",
    }


def label_differences(dictionaries: DictionarySet, scored_enums: ModuleType) -> list[str]:
    """Surfaces ``normalize`` and the Dojo's ``canonical_label`` resolve differently, and unmapped fields."""
    problems: list[str] = []
    classes = dictionaries["doc_classes"]
    scored: Any = scored_enums.SCORED_ENUM_FIELDS
    for cls in classes:
        entry = classes.get(cls)
        assert entry is not None
        fields = entry.attributes["closed_label_fields"]
        for field in scored.get(cls, {}):
            if field not in fields:
                problems.append(f"{cls}.{field}: a Dojo scored enum with no closed_label_fields entry")
        for field, name in fields.items():
            if field not in scored.get(cls, {}):
                continue  # closed here but not a scored label (record_type)
            dictionary = dictionaries[name]
            for item in dictionary.entries:
                for kind, surface in [("key", item.key), ("label", item.label)] + [
                    ("alias", alias) for alias in item.aliases
                ]:
                    if kind == "label" and (name, item.key, surface) in DELIBERATE:
                        continue
                    for value in sorted(spellings(surface)):
                        dojo = scored_enums.canonical_label(cls, field, value) or None
                        mine = dictionary.normalize(value)
                        if dojo != mine:
                            problems.append(
                                f"{name} {kind} {surface!r}: {value!r} -> Dojo {dojo!r}, dictionary {mine!r}"
                            )
    return problems


def _module_constants(path: Path) -> tuple[dict[str, str], ast.Module]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constants: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            constants[node.targets[0].id] = node.value.value
    return constants, tree


def _review_causes(mailroom: Path) -> list[str]:
    """The keys of ``CAUSE_LABELS`` in source order, constants resolved."""
    constants, tree = _module_constants(mailroom / "src" / "pipeline" / "reconsideration.py")
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.value, ast.Dict):
            continue
        if isinstance(node.target, ast.Name) and node.target.id == "CAUSE_LABELS":
            keys: list[str] = []
            for key in node.value.keys:
                if isinstance(key, ast.Name):
                    keys.append(constants[key.id])
                elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.append(key.value)
            return keys
    raise ValueError("CAUSE_LABELS not found in src/pipeline/reconsideration.py")


def _outcomes(mailroom: Path) -> list[str]:
    """The terminal ``PipelineStage`` values (every stage but inbox, processing, classified)."""
    tree = ast.parse((mailroom / "src" / "schemas" / "manifest.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "PipelineStage":
            values = [
                item.value.value
                for item in node.body
                if isinstance(item, ast.Assign)
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            ]
            return [v for v in values if v not in {"inbox", "processing", "classified"}]
    raise ValueError("PipelineStage not found in src/schemas/manifest.py")


def source_values(
    mailroom: Path, dojo: dict[str, ModuleType], dictionaries: DictionarySet
) -> dict[str, dict[str, Any]]:
    """``{dictionary: {"keys": [...], "ordered": bool, "from": str}}`` read from the sources."""
    taxonomy = yaml.safe_load((mailroom / "src" / "config" / "taxonomy.yaml").read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {
        "doc_classes": {
            "keys": [c["key"] for c in taxonomy["doc_classes"]],
            "ordered": True,
            "from": "llm-mailroom src/config/taxonomy.yaml doc_classes",
        }
    }
    classes = dictionaries["doc_classes"]
    for cls in taxonomy["doc_classes"]:
        entry = classes.get(cls["key"])
        fields = entry.attributes["closed_label_fields"] if entry is not None else {}
        for field, values in (cls.get("label_vocabularies") or {}).items():
            name = fields.get(field, f"<unmapped {cls['key']}.{field}>")
            out[name] = {
                "keys": list(values),
                "ordered": True,
                "from": f"llm-mailroom src/config/taxonomy.yaml {cls['key']} label_vocabularies {field}",
            }
    out["cms_source_table"] = {
        "keys": list(dojo["scored_enums"].scored_vocabulary("insurance_claim", "cms_source_table")),
        "ordered": True,
        "from": "llm-dojo-scoring schemas/scored_enums.py SCORED_ENUM_FIELDS (insurance_claim)",
    }
    out["corporate_record_type"] = {
        "keys": list(dojo["corporate_record_schema"].RECORD_TYPES),
        "ordered": True,
        "from": "llm-dojo-scoring schemas/corporate_record_schema.py RECORD_TYPES",
    }
    out["review_causes"] = {
        "keys": _review_causes(mailroom),
        "ordered": True,
        "from": "llm-mailroom src/pipeline/reconsideration.py CAUSE_LABELS",
    }
    out["outcomes"] = {
        "keys": _outcomes(mailroom),
        "ordered": False,
        "from": "llm-mailroom src/schemas/manifest.py PipelineStage (terminal values)",
    }
    return dict(sorted(out.items()))


def _value_differences(
    values: dict[str, dict[str, Any]],
    taxonomy: dict[str, Any],
    dojo: dict[str, ModuleType],
    dictionaries: DictionarySet,
) -> list[str]:
    problems: list[str] = []
    for name, row in values.items():
        if name not in dictionaries:
            problems.append(f"{name}: in the sources but not shipped")
            continue
        shipped = list(dictionaries[name].keys())
        same = shipped == row["keys"] if row["ordered"] else sorted(shipped) == sorted(row["keys"])
        if not same:
            problems.append(f"{name}: shipped {shipped} but {row['from']} has {row['keys']}")
    corpus, scored, config = dojo["corpus"], dojo["scored_enums"], dojo["config"]
    classes = dictionaries["doc_classes"]
    for cls in taxonomy["doc_classes"]:
        entry = classes.get(cls["key"])
        if entry is None:
            continue
        if entry.label != cls["label"]:
            problems.append(f"doc_classes {cls['key']}: label {entry.label!r} vs taxonomy {cls['label']!r}")
        if " ".join(entry.description.split()) != " ".join(cls["description"].split()):
            problems.append(f"doc_classes {cls['key']}: description differs from the taxonomy")
        subclasses = list(corpus.DOC_TYPE_SUBCLASSES.get(cls["key"], ()))
        if list(entry.attributes["subclasses"]) != subclasses:
            problems.append(
                f"doc_classes {cls['key']}: subclasses differ from DOC_TYPE_SUBCLASSES {subclasses}"
            )
        for field, name in entry.attributes["closed_label_fields"].items():
            vocab = scored.SCORED_ENUM_FIELDS.get(cls["key"], {}).get(field)
            if vocab is not None and set(vocab[0]) != set(dictionaries[name].keys()):
                problems.append(f"{name}: keys differ from Dojo scored {cls['key']}.{field} {list(vocab[0])}")
    cuad = dictionaries["cuad_family"]
    for subtype in config.CONTRACT_SUBTYPES:
        item = cuad.get(subtype["key"])
        if item is None or (item.label, item.description) != (subtype["label"], subtype["description"]):
            problems.append(
                f"cuad_family {subtype['key']}: label or description differs from CONTRACT_SUBTYPES"
            )
    maud = dictionaries["merger_consideration"]
    for key, label in config.MAUD_CONSIDERATION_LABELS.items():
        item = maud.get(key)
        if item is None or item.label != label:
            problems.append(f"merger_consideration {key}: label differs from MAUD_CONSIDERATION_LABELS")
    return problems


def _head(checkout: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mailroom", type=Path, required=True, help="llm-mailroom checkout")
    parser.add_argument("--dojo", type=Path, required=True, help="llm-dojo-scoring checkout")
    parser.add_argument("--write-fixture", action="store_true", help=f"also write {FIXTURE.name}")
    args = parser.parse_args(argv)
    try:
        heads = {"llm-mailroom": _head(args.mailroom), "llm-dojo-scoring": _head(args.dojo)}
        sys.path.insert(0, str(args.dojo.resolve()))
        dojo = {
            name: importlib.import_module(f"llm_dojo_scoring.{name}")
            for name in (
                "config",
                "schemas.corpus",
                "schemas.scored_enums",
                "schemas.corporate_record_schema",
            )
        }
        dojo = {name.rsplit(".", 1)[-1]: module for name, module in dojo.items()}
        taxonomy = yaml.safe_load(
            (args.mailroom / "src" / "config" / "taxonomy.yaml").read_text(encoding="utf-8")
        )
        dictionaries = shipped_dictionaries()
        values = source_values(args.mailroom, dojo, dictionaries)
    except (OSError, ValueError, ImportError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    problems = _value_differences(values, taxonomy, dojo, dictionaries)
    problems += label_differences(dictionaries, dojo["scored_enums"])
    for problem in problems:
        print(problem)
    print(
        f"{len(problems)} difference(s); llm-mailroom {heads['llm-mailroom'][:7]}, "
        f"llm-dojo-scoring {heads['llm-dojo-scoring'][:7]}"
    )
    if args.write_fixture:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"sources": heads, "dictionaries": values}
        FIXTURE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {FIXTURE}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
