"""Dictionary sets: cross-references, overrides, manifests, and a client profile's own vocabulary."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from mailroom_common.errors import ConfigError
from mailroom_dictionaries import (
    DATA_DIR,
    DictionarySet,
    VocabularyError,
    load_dictionary_dir,
    load_dictionary_file,
    parse_field_type,
    shipped_dictionaries,
)

WriteDict = Callable[..., Path]


def shipped_yaml(name: str) -> dict[str, Any]:
    data = yaml.safe_load((DATA_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_manifest_pins_every_member() -> None:
    manifest = shipped_dictionaries().manifest()
    assert list(manifest) == sorted(manifest)
    assert manifest["urgency"]["version"] == 1 and len(manifest["urgency"]["hash"]) == 64


def test_same_name_twice_is_refused(write_dict: WriteDict, colors: dict[str, Any], tmp_path: Path) -> None:
    first = load_dictionary_file(write_dict(colors))
    other = tmp_path / "other"
    other.mkdir()
    (other / "colors.yaml").write_text(yaml.safe_dump(colors), encoding="utf-8")
    second = load_dictionary_file(other / "colors.yaml")
    with pytest.raises(ConfigError, match="'colors' is given twice") as info:
        DictionarySet([first, second])
    assert len(info.value.context["sources"]) == 2


def test_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="dictionary directory does not exist"):
        load_dictionary_dir(tmp_path / "nope")


def test_doc_class_naming_a_missing_dictionary(tmp_path: Path) -> None:
    data = shipped_yaml("doc_classes")
    data["entries"][0]["attributes"]["closed_label_fields"]["cuad_family"] = "cuad_families"
    (tmp_path / "doc_classes.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    client = load_dictionary_file(tmp_path / "doc_classes.yaml")  # the file alone is fine
    with pytest.raises(ConfigError, match="names dictionary 'cuad_families', which is not loaded"):
        shipped_dictionaries().with_overrides([client])


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda a: a["subclasses"].append("ip"), r"duplicate subclasses \['ip'\]"),
        (lambda a: a["subclasses"].append("IP"), "duplicate subclasses"),
        (lambda a: a["subclasses"].append("Bad Key"), "must match"),
        (lambda a: a.pop("subclasses"), r"missing \['subclasses'\]"),
        (lambda a: a.update(colour="x"), r"unknown \['colour'\]"),
        (lambda a: a.update(subclasses="ip"), "subclasses must be a list of strings"),
        (lambda a: a.update(closed_label_fields=["cuad_family"]), "closed_label_fields must be a mapping"),
        (lambda a: a["closed_label_fields"].update({"Bad Field": "urgency"}), "must map a field name"),
    ],
)
def test_doc_classes_file_rules(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], object], match: str
) -> None:
    data = shipped_yaml("doc_classes")
    mutate(data["entries"][0]["attributes"])
    (tmp_path / "doc_classes.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_dictionary_file(tmp_path / "doc_classes.yaml")


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda e: e[0]["attributes"].update(default_element="name"), "default_element is only allowed"),
        (lambda e: e[0]["attributes"].update(takes_element="no"), "takes_element must be true or false"),
        (lambda e: e[0].update(attributes={}), r"missing \['takes_element'\]"),
    ],
)
def test_field_types_file_rules(
    tmp_path: Path, mutate: Callable[[list[dict[str, Any]]], object], match: str
) -> None:
    data = shipped_yaml("field_types")
    mutate(data["entries"])
    (tmp_path / "field_types.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_dictionary_file(tmp_path / "field_types.yaml")


def copy_data(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    shutil.copytree(DATA_DIR, target)
    return target


def test_element_type_must_be_a_scalar_field_type(tmp_path: Path) -> None:
    data_dir = copy_data(tmp_path)
    elements = shipped_yaml("entity_element_types")
    elements["entries"].append({"key": "entity_list", "label": "Lists", "description": "Nested lists."})
    (data_dir / "entity_element_types.yaml").write_text(yaml.safe_dump(elements), encoding="utf-8")
    with pytest.raises(ConfigError, match="'entity_list' must be a field type that takes no element"):
        load_dictionary_dir(data_dir)


def test_default_element_must_exist(tmp_path: Path) -> None:
    data_dir = copy_data(tmp_path)
    types = shipped_yaml("field_types")
    for entry in types["entries"]:
        if entry["key"] == "entity_list":
            entry["attributes"]["default_element"] = "money"
    (data_dir / "field_types.yaml").write_text(yaml.safe_dump(types), encoding="utf-8")
    with pytest.raises(ConfigError, match="default_element 'money' is not an entity element type"):
        load_dictionary_dir(data_dir)


def test_field_types_without_element_dictionary(tmp_path: Path) -> None:
    data_dir = copy_data(tmp_path)
    (data_dir / "entity_element_types.yaml").unlink()
    with pytest.raises(ConfigError, match="field_types needs the entity_element_types dictionary"):
        load_dictionary_dir(data_dir)


def test_client_profile_adds_a_document_type_without_code(tmp_path: Path) -> None:
    """R4: a new class with its own closed label vocabulary is two data files, no code change."""
    classes = shipped_yaml("doc_classes")
    classes["version"] = 1
    classes["entries"].append(
        {
            "key": "auto_policy",
            "label": "Auto policy",
            "description": "A synthetic client's auto insurance policy.",
            "attributes": {
                "subclasses": ["personal", "commercial"],
                "closed_label_fields": {"coverage": "coverage_tier"},
            },
        }
    )
    profile = tmp_path / "clients" / "acme"
    profile.mkdir(parents=True)
    (profile / "doc_classes.yaml").write_text(yaml.safe_dump(classes, sort_keys=False), encoding="utf-8")
    (profile / "coverage_tier.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "coverage_tier",
                "version": 1,
                "description": "Synthetic coverage tiers.",
                "ordered": True,
                "entries": [
                    {"key": "basic", "label": "Basic", "description": "Basic.", "rank": 1},
                    {
                        "key": "full",
                        "label": "Full",
                        "description": "Full.",
                        "rank": 2,
                        "aliases": ["comprehensive"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    client = [load_dictionary_file(p) for p in sorted(profile.glob("*.yaml"))]
    merged = shipped_dictionaries().with_overrides(client)

    assert (
        "auto_policy" in merged["doc_classes"] and "auto_policy" not in shipped_dictionaries()["doc_classes"]
    )
    target = merged["doc_classes"].get("auto_policy").attributes["closed_label_fields"]["coverage"]  # type: ignore[union-attr]
    assert merged[target].require("Comprehensive") == "full" and merged[target].rank("full") == 2
    assert (
        merged.manifest()["doc_classes"]["hash"] != shipped_dictionaries().manifest()["doc_classes"]["hash"]
    )
    assert str(parse_field_type("entity_list:name", dictionaries=merged)) == "entity_list:name"
    assert "coverage_tier" not in shipped_dictionaries()
    with pytest.raises(VocabularyError, match="'platinum' is not in dictionary 'coverage_tier' version 1"):
        merged[target].require("platinum")


def test_overrides_given_twice_are_refused(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    d = load_dictionary_file(write_dict(colors))
    with pytest.raises(ConfigError, match="given twice"):
        shipped_dictionaries().with_overrides([d, d])
