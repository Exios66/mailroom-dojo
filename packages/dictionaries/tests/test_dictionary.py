"""One dictionary file: loading, lookups, hashing and every validation failure, on real files."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from mailroom_common.errors import ConfigError
from mailroom_dictionaries import Dictionary, VocabularyError, fold, load_dictionary_file

WriteDict = Callable[..., Path]


def load(write_dict: WriteDict, data: dict[str, Any] | str, name: str | None = None) -> Dictionary:
    return load_dictionary_file(write_dict(data, name))


def refused(
    write_dict: WriteDict, data: dict[str, Any] | str, match: str, name: str | None = None
) -> ConfigError:
    """Loading must raise exactly ConfigError (not a subclass) whose message matches."""
    with pytest.raises(ConfigError, match=match) as info:
        load(write_dict, data, name)
    assert type(info.value) is ConfigError
    assert info.value.context["source"].endswith(".yaml")
    return info.value


# --- lookups -------------------------------------------------------------------------------


def test_loads_entries_in_file_order(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    d = load(write_dict, colors)
    assert (d.name, d.version, d.ordered) == ("colors", 1, False)
    assert d.keys() == ("red", "light-blue", "green")
    assert d.get("red") is not None and d.get("red").aliases == ("crimson",)  # type: ignore[union-attr]
    assert d.get("blue") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("red", "red"),
        ("RED", "red"),
        ("  Red\t", "red"),
        ("crimson", "red"),
        ("Crim son", "red"),
        ("light blue", "light-blue"),
        ("LIGHT_BLUE", "light-blue"),
        ("light--blue", "light-blue"),
        ("blue", None),
        ("", None),
        ("   ", None),
        (None, None),
        (3, None),
        (["red"], None),
    ],
)
def test_normalize(
    write_dict: WriteDict, colors: dict[str, Any], value: object, expected: str | None
) -> None:
    assert load(write_dict, colors).normalize(value) == expected


def test_contains_is_exact_canonical_key_only(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    d = load(write_dict, colors)
    assert "red" in d and "light-blue" in d
    assert "RED" not in d and "crimson" not in d and "light_blue" not in d and 1 not in d


def test_require_suggests_close_keys_and_names_the_dictionary(
    write_dict: WriteDict, colors: dict[str, Any]
) -> None:
    d = load(write_dict, colors)
    with pytest.raises(VocabularyError) as info:
        d.require("gren")
    err = info.value
    assert isinstance(err, ConfigError)
    assert "'gren' is not in dictionary 'colors' version 1; did you mean 'green'?" in str(err)
    assert err.context["suggestions"] == ["green"]
    assert (err.context["dictionary"], err.context["version"], err.context["value"]) == ("colors", 1, "gren")


def test_require_suggestion_through_an_alias_names_the_key(
    write_dict: WriteDict, colors: dict[str, Any]
) -> None:
    with pytest.raises(VocabularyError, match="did you mean 'red'") as info:
        load(write_dict, colors).require("crimsom")
    assert info.value.context["suggestions"] == ["red"]


def test_require_without_close_match_has_no_hint(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    with pytest.raises(VocabularyError) as info:
        load(write_dict, colors).require("zzzzzz")
    assert "did you mean" not in str(info.value)
    assert info.value.context["suggestions"] == []


def test_require_non_string(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    with pytest.raises(
        VocabularyError, match=r"expected a string, got int \(dictionary 'colors' version 1\)"
    ):
        load(write_dict, colors).require(7)


def test_deprecated_key_and_its_aliases_resolve_to_the_replacement(
    write_dict: WriteDict, colors: dict[str, Any]
) -> None:
    colors["entries"].append(
        {
            "key": "verdant",
            "label": "Verdant",
            "description": "Old name.",
            "aliases": ["leafy"],
            "deprecated": True,
            "replaced_by": "green",
        }
    )
    d = load(write_dict, colors)
    assert d.normalize("Verdant") == "green" and d.normalize("leafy") == "green"
    assert d.require("verdant") == "green"
    assert "verdant" not in d and "verdant" not in d
    assert d.get("verdant") is not None and d.get("verdant").deprecated  # type: ignore[union-attr]


def test_deprecated_key_without_replacement_does_not_resolve(
    write_dict: WriteDict, colors: dict[str, Any]
) -> None:
    colors["entries"].append(
        {"key": "mauve", "label": "Mauve", "description": "Retired.", "deprecated": True}
    )
    d = load(write_dict, colors)
    assert d.normalize("mauve") is None
    with pytest.raises(VocabularyError, match="'mauve' is deprecated and has no replacement"):
        d.require("mauve")


def test_rank_follows_declared_ranks(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["ordered"] = True
    for rank, entry in zip((1, 5, 9), colors["entries"], strict=True):
        entry["rank"] = rank
    d = load(write_dict, colors)
    assert [d.rank(k) for k in ("crimson", "Light Blue", "green")] == [1, 5, 9]
    with pytest.raises(VocabularyError, match="not in dictionary"):
        d.rank("blue")


def test_rank_on_unordered_dictionary_is_refused(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    with pytest.raises(VocabularyError, match="'colors' is not ordered"):
        load(write_dict, colors).rank("red")


def test_deprecated_entry_ranks_as_its_replacement(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["ordered"] = True
    for rank, entry in enumerate(colors["entries"], start=1):
        entry["rank"] = rank
    colors["entries"].append(
        {"key": "verdant", "label": "V", "description": "Old.", "deprecated": True, "replaced_by": "green"}
    )
    assert load(write_dict, colors).rank("verdant") == 3


def test_attributes_are_read_only(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"][0]["attributes"] = {"rgb": [255, 0, 0], "meta": {"warm": True}}
    entry = load(write_dict, colors).get("red")
    assert entry is not None
    assert entry.attributes["rgb"] == (255, 0, 0) and entry.attributes["meta"]["warm"] is True
    with pytest.raises(TypeError):
        entry.attributes["rgb"] = (0, 0, 0)  # type: ignore[index]
    with pytest.raises(TypeError):
        entry.attributes["meta"]["warm"] = False


# --- hashing -------------------------------------------------------------------------------


def test_hash_ignores_key_order_and_written_defaults(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    first = load(write_dict, colors).content_hash()
    reordered = {k: colors[k] for k in reversed(list(colors))}
    reordered["entries"] = [dict(reversed(list(e.items()))) for e in colors["entries"]]
    for entry in reordered["entries"]:
        entry.setdefault("aliases", [])
        entry["deprecated"] = False
        entry["attributes"] = {}
    assert load(write_dict, reordered).content_hash() == first


def test_hash_ignores_version_but_sees_any_content_change(
    write_dict: WriteDict, colors: dict[str, Any]
) -> None:
    first = load(write_dict, colors).content_hash()
    assert len(first) == 64
    colors["version"] = 2
    assert load(write_dict, colors).content_hash() == first
    colors["entries"][2]["description"] = "Green!"
    assert load(write_dict, colors).content_hash() != first


def test_hash_sees_entry_order(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    first = load(write_dict, colors).content_hash()
    colors["entries"].reverse()
    assert load(write_dict, colors).content_hash() != first


# --- folding -------------------------------------------------------------------------------


def test_fold() -> None:
    assert fold(" Time-Sensitive ") == fold("time_sensitive") == fold("TIME SENSITIVE") == "timesensitive"
    assert fold("Straße") == "strasse"


# --- unreadable or malformed files ---------------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config file") as info:
        load_dictionary_file(tmp_path / "colors.yaml")
    assert info.value.context["source"] == str(tmp_path / "colors.yaml")


def test_directory_instead_of_file(tmp_path: Path) -> None:
    (tmp_path / "colors.yaml").mkdir()
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_dictionary_file(tmp_path / "colors.yaml")


def test_non_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "colors.yaml"
    path.write_bytes(b"name: colors\ndescription: caf\xe9\n")
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_dictionary_file(path)


def test_empty_file(write_dict: WriteDict) -> None:
    refused(write_dict, "", "must be a mapping, got NoneType")


def test_list_instead_of_mapping(write_dict: WriteDict) -> None:
    refused(write_dict, "- a\n- b\n", "must be a mapping, got list")


def test_truncated_file(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    text = yaml.safe_dump(colors, sort_keys=False)
    cut = text[: text.index("light-blue") + 4]  # ends mid-entry
    refused(write_dict, cut, "invalid dictionary file|invalid YAML")


def test_corrupt_yaml_reports_line(write_dict: WriteDict) -> None:
    err = refused(write_dict, "name: colors\nentries: [\n  {key: red\n", "invalid YAML")
    assert err.context["line"] is not None


def test_huge_line_loads(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"][0]["description"] = "x" * 2_000_000
    d = load(write_dict, colors)
    assert len(d.get("red").description) == 2_000_000  # type: ignore[union-attr]


def test_duplicate_mapping_key_in_entry(write_dict: WriteDict) -> None:
    text = (
        "name: colors\nversion: 1\ndescription: d\nentries:\n"
        "  - key: red\n    label: Red\n    label: Again\n    description: r\n"
    )
    refused(write_dict, text, "duplicate mapping key 'label'")


def test_yaml_boolean_words_stay_keys(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    text = "name: answers\nversion: 1\ndescription: d\nentries:\n  - {key: no, label: No, description: n}\n"
    assert load(write_dict, text, name="answers").keys() == ("no",)


# --- validation rules, one real malformed file each -----------------------------------------


def test_duplicate_entry_key(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"].append({"key": "green", "label": "G2", "description": "again"})
    refused(write_dict, colors, "key 'green' collides with key 'green' of entry 'green'")


@pytest.mark.parametrize("key", ["Red", "1red", "red!", "red:x", "", "_red", "red blue"])
def test_invalid_key_pattern(write_dict: WriteDict, colors: dict[str, Any], key: str) -> None:
    colors["entries"][0]["key"] = key
    refused(write_dict, colors, "key must match")


@pytest.mark.parametrize("where", ["dictionary", "entry", "label"])
def test_empty_description_or_label(write_dict: WriteDict, colors: dict[str, Any], where: str) -> None:
    if where == "dictionary":
        colors["description"] = "  "
    elif where == "entry":
        colors["entries"][1]["description"] = ""
    else:
        colors["entries"][1]["label"] = " "
    refused(write_dict, colors, "is empty")


def test_alias_colliding_with_a_key_after_folding(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"][2]["aliases"] = ["Light Blue"]
    refused(write_dict, colors, "alias 'Light Blue' collides with key 'light-blue' of entry 'light-blue'")


def test_alias_colliding_with_another_alias(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"][2]["aliases"] = ["CRIMSON"]
    refused(write_dict, colors, "alias 'CRIMSON' collides with alias 'crimson' of entry 'red'")


def test_alias_colliding_with_its_own_key(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"][0]["aliases"] = ["RED"]
    refused(write_dict, colors, "collides with key 'red'")


def test_keys_colliding_after_folding(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"].append({"key": "light_blue", "label": "LB", "description": "dup"})
    refused(write_dict, colors, "key 'light_blue' collides with key 'light-blue'")


def test_alias_empty_after_folding(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"][0]["aliases"] = [" - _ "]
    refused(write_dict, colors, "is empty after folding")


def test_replaced_by_pointing_nowhere(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"].append(
        {"key": "old", "label": "O", "description": "o", "deprecated": True, "replaced_by": "purple"}
    )
    refused(write_dict, colors, "replaced_by 'purple' is not a key")


def test_replaced_by_pointing_at_a_deprecated_key(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"] += [
        {"key": "older", "label": "O", "description": "o", "deprecated": True, "replaced_by": "old"},
        {"key": "old", "label": "O", "description": "o", "deprecated": True, "replaced_by": "red"},
    ]
    refused(write_dict, colors, "replaced_by 'old' is itself deprecated")


def test_replaced_by_cycle(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"] += [
        {"key": "a", "label": "A", "description": "a", "deprecated": True, "replaced_by": "b"},
        {"key": "b", "label": "B", "description": "b", "deprecated": True, "replaced_by": "a"},
    ]
    refused(write_dict, colors, "is itself deprecated")


def test_replaced_by_on_a_live_entry(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"][0]["replaced_by"] = "green"
    refused(write_dict, colors, "replaced_by is set but the entry is not deprecated")


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("extra", "x", "extra: Extra inputs are not permitted"),
        ("version", 0, "version must be a positive integer, got 0"),
        ("version", -3, "version must be a positive integer"),
        ("version", "1", "version: Input should be a valid integer"),
        ("version", 1.0, "version: Input should be a valid integer"),
        ("version", True, "version: Input should be a valid integer"),
        ("ordered", "yes", "ordered: Input should be a valid boolean"),
        ("entries", [], "dictionary has no entries"),
        ("name", "Colors", "dictionary name 'Colors' must match"),
    ],
)
def test_top_level_rules(
    write_dict: WriteDict, colors: dict[str, Any], field: str, value: object, match: str
) -> None:
    colors[field] = value
    refused(write_dict, colors, match, name="colors")


def test_missing_required_fields(write_dict: WriteDict) -> None:
    refused(write_dict, {"name": "colors"}, "version: Field required.*description: Field required")


def test_unknown_entry_field(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"][0]["colour"] = "red"
    refused(write_dict, colors, r"entries\.0\.colour: Extra inputs are not permitted")


def test_name_must_equal_file_stem(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    refused(write_dict, colors, "must equal the file name 'shades'", name="shades")


def test_reserved_name(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["name"] = "glossary"
    refused(write_dict, colors, "'glossary' is reserved")


def test_date_attribute_must_be_quoted(write_dict: WriteDict) -> None:
    text = (
        "name: colors\nversion: 1\ndescription: d\nentries:\n"
        "  - {key: red, label: R, description: r, attributes: {since: 2026-11-18}}\n"
    )
    refused(write_dict, text, "got date; quote dates")


def test_nan_attribute(write_dict: WriteDict) -> None:
    text = (
        "name: colors\nversion: 1\ndescription: d\nentries:\n"
        "  - {key: red, label: R, description: r, attributes: {x: .nan}}\n"
    )
    refused(write_dict, text, "must be finite")


def test_ordered_dictionary_with_duplicate_rank(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["ordered"] = True
    for rank, entry in zip((1, 2, 2), colors["entries"], strict=True):
        entry["rank"] = rank
    refused(write_dict, colors, "entry 'green': duplicate rank 2 \\(also on 'light-blue'\\)")


def test_ordered_dictionary_missing_rank(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["ordered"] = True
    colors["entries"][0]["rank"] = 1
    refused(write_dict, colors, "needs a rank on every entry")


def test_ordered_dictionary_out_of_order(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["ordered"] = True
    for rank, entry in zip((1, 3, 2), colors["entries"], strict=True):
        entry["rank"] = rank
    refused(write_dict, colors, "listed in rank order")


def test_rank_on_unordered_dictionary(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["entries"][0]["rank"] = 1
    refused(write_dict, colors, "rank is set but the dictionary is not ordered")


def test_rank_on_deprecated_entry(write_dict: WriteDict, colors: dict[str, Any]) -> None:
    colors["ordered"] = True
    for rank, entry in enumerate(colors["entries"], start=1):
        entry["rank"] = rank
    colors["entries"].append(
        {"key": "old", "label": "O", "description": "o", "deprecated": True, "replaced_by": "red", "rank": 9}
    )
    refused(write_dict, colors, "deprecated entries take their replacement's rank")
