from __future__ import annotations

import copy
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from mailroom_common.config import ConfigModel, config_hash, deep_merge, load_config, load_layered
from mailroom_common.errors import ConfigError


class Mode(StrEnum):
    fast = "fast"
    careful = "careful"


class Limits(ConfigModel):
    max_pages: int
    threshold: float = 0.5


class Profile(ConfigModel):
    name: str
    mode: Mode = Mode.fast
    limits: Limits
    tags: tuple[str, ...] = ()
    out_dir: Path = Path("out")
    since: datetime | None = None
    endpoint: str = "http://localhost"


BASE_YAML = """\
name: demo
limits:
  max_pages: 10
tags: [a, b]
"""


def _file(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- load_config ------------------------------------------------------------------------


def test_yaml_and_json_load_to_the_same_model_and_hash(tmp_path: Path) -> None:
    from_yaml = load_config(_file(tmp_path, "p.yaml", BASE_YAML), Profile)
    as_json = json.dumps({"tags": ["a", "b"], "limits": {"max_pages": 10}, "name": "demo"})
    from_json = load_config(_file(tmp_path, "p.json", as_json), Profile)
    from_yml = load_config(_file(tmp_path, "p.YML", BASE_YAML), Profile)
    assert from_yaml == from_json == from_yml
    assert from_yaml.tags == ("a", "b") and from_yaml.limits.max_pages == 10
    assert config_hash(from_yaml) == config_hash(from_json)


def test_json_mode_types_are_accepted_from_their_json_forms(tmp_path: Path) -> None:
    text = BASE_YAML + "mode: careful\nout_dir: runs/x\nsince: 2026-11-18T08:42:00Z\n"
    profile = load_config(_file(tmp_path, "p.yaml", text), Profile)
    assert profile.mode is Mode.careful
    assert profile.out_dir == Path("runs/x")
    assert profile.since is not None and profile.since.year == 2026


def test_loaded_model_is_frozen(tmp_path: Path) -> None:
    profile = load_config(_file(tmp_path, "p.yaml", BASE_YAML), Profile)
    with pytest.raises(ValidationError, match="frozen"):
        profile.name = "other"


def test_validation_error_lists_every_problem_with_dotted_paths(tmp_path: Path) -> None:
    text = "name: 5\nlimits:\n  max_pages: '10'\n  extra_key: 1\ntags: [ok, 3]\nmode: slow\n"
    path = _file(tmp_path, "bad.yaml", text)
    with pytest.raises(ConfigError) as info:
        load_config(path, Profile)
    message = info.value.message
    assert message.startswith("config does not match Profile: ")
    for expected in (
        "name: Input should be a valid string",
        "limits.max_pages: Input should be a valid integer",
        "limits.extra_key: Extra inputs are not permitted",
        "tags.1: Input should be a valid string",
        "mode: Input should be 'fast' or 'careful'",
    ):
        assert expected in message
    assert info.value.context == {"source": str(path), "problems": 5}


def test_missing_required_field_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="limits: Field required"):
        load_config(_file(tmp_path, "p.yaml", "name: x\n"), Profile)


def test_strict_types_refuse_bool_as_number(tmp_path: Path) -> None:
    text = "name: x\nlimits: {max_pages: true}\n"
    with pytest.raises(ConfigError, match="limits.max_pages: Input should be a valid integer"):
        load_config(_file(tmp_path, "p.yaml", text), Profile)


def test_validation_message_does_not_echo_values(tmp_path: Path) -> None:
    text = "name: x\nlimits: {max_pages: 'hunter2-secret'}\n"
    with pytest.raises(ConfigError) as info:
        load_config(_file(tmp_path, "p.yaml", text), Profile)
    assert "hunter2-secret" not in str(info.value)


@pytest.mark.parametrize("name", ["p.toml", "p.txt", "p", "p.yaml.bak"])
def test_unknown_extension_is_refused(tmp_path: Path, name: str) -> None:
    path = _file(tmp_path, name, BASE_YAML)
    with pytest.raises(ConfigError, match="unknown config file extension") as info:
        load_config(path, Profile)
    assert info.value.context["source"] == str(path)


def test_missing_and_unreadable_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(tmp_path / "missing.yaml", Profile)
    (tmp_path / "dir.json").mkdir()
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(tmp_path / "dir.json", Profile)
    bad = tmp_path / "latin1.yaml"
    bad.write_bytes(b"name: caf\xe9\n")
    with pytest.raises(ConfigError, match="cannot read config file") as info:
        load_config(bad, Profile)
    assert info.value.context["source"] == str(bad)


@pytest.mark.parametrize(
    ("name", "text", "match"),
    [
        ("e.yaml", "", "config file is empty"),
        ("e.yaml", "# only a comment\n", "config file is empty"),
        ("e.json", "null", "config file is empty"),
        ("l.yaml", "- a\n- b\n", "must hold a mapping at the top, got list"),
        ("s.json", '"text"', "must hold a mapping at the top, got str"),
    ],
)
def test_empty_and_non_mapping_files_are_refused(tmp_path: Path, name: str, text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(_file(tmp_path, name, text), Profile)


@pytest.mark.parametrize(
    ("text", "match", "line"),
    [
        ('{"name": "a",\n "name": "b"}', "duplicate object key 'name'", None),
        ('{"name": NaN}', "NaN is not valid JSON", None),
        ('{"name": "a",\n\n "limits": }', "Expecting value", 3),
    ],
)
def test_invalid_json_is_a_config_error(tmp_path: Path, text: str, match: str, line: int | None) -> None:
    path = _file(tmp_path, "p.json", text)
    with pytest.raises(ConfigError, match=match) as info:
        load_config(path, Profile)
    assert info.value.context["source"] == str(path)
    assert info.value.context["line"] == line


def test_invalid_yaml_is_a_config_error_with_line(tmp_path: Path) -> None:
    path = _file(tmp_path, "p.yaml", "name: a\nname: b\n")
    with pytest.raises(ConfigError, match="duplicate mapping key 'name'") as info:
        load_config(path, Profile)
    assert info.value.context == {"source": str(path), "line": 2}


@pytest.mark.parametrize(
    ("text", "match", "key"),
    [
        ("name: x\nlimits: {max_pages: 1, threshold: .nan}\n", "NaN and infinity", "limits.threshold"),
        ("name: x\nlimits: {max_pages: 1, threshold: -.inf}\n", "NaN and infinity", "limits.threshold"),
        ("name: !!binary aGk=\nlimits: {max_pages: 1}\n", "type bytes cannot be represented", "name"),
        (
            "name: x\ntags: !!set {a: null}\nlimits: {max_pages: 1}\n",
            "type set cannot be represented",
            "tags",
        ),
    ],
)
def test_yaml_values_json_cannot_hold_are_refused(tmp_path: Path, text: str, match: str, key: str) -> None:
    with pytest.raises(ConfigError, match=match) as info:
        load_config(_file(tmp_path, "p.yaml", text), Profile)
    assert info.value.context["key"] == key


def test_yaml_dates_become_iso_strings(tmp_path: Path) -> None:
    class Dated(ConfigModel):
        day: str
        stamp: datetime

    model = load_config(_file(tmp_path, "d.yaml", "day: 2026-11-18\nstamp: 2026-11-18 08:42:00\n"), Dated)
    assert model.day == "2026-11-18"
    assert model.stamp == datetime(2026, 11, 18, 8, 42)


# --- interpolation ------------------------------------------------------------------------


ENV_YAML = """\
name: "${CLIENT}"
endpoint: "https://${HOST:-localhost}:${PORT:-8000}/v1"
limits: {max_pages: 1}
tags: ["cost $$5", "${CLIENT}-${CLIENT}", "plain $HOME", "lone $"]
"""


def test_interpolation_replaces_defaults_and_escapes(tmp_path: Path) -> None:
    path = _file(tmp_path, "p.yaml", ENV_YAML)
    profile = load_config(path, Profile, env={"CLIENT": "acme", "PORT": "9000"})
    assert profile.name == "acme"
    assert profile.endpoint == "https://localhost:9000/v1"
    assert profile.tags == ("cost $5", "acme-acme", "plain $HOME", "lone $")


def test_default_applies_to_empty_variables_but_plain_reference_keeps_empty(tmp_path: Path) -> None:
    path = _file(tmp_path, "p.yaml", ENV_YAML)
    profile = load_config(path, Profile, env={"CLIENT": "", "HOST": ""})
    assert profile.name == ""
    assert profile.endpoint == "https://localhost:8000/v1"


def test_without_env_strings_are_literal(tmp_path: Path) -> None:
    profile = load_config(_file(tmp_path, "p.yaml", ENV_YAML), Profile)
    assert profile.name == "${CLIENT}"
    assert profile.tags[0] == "cost $$5"


def test_interpolation_only_touches_string_values_not_keys(tmp_path: Path) -> None:
    class Free(ConfigModel):
        model_config = ConfigModel.model_config | {"extra": "allow"}

    path = _file(tmp_path, "p.yaml", "'${KEY}': '${VAL}'\nn: 5\n")
    model = load_config(path, Free, env={"KEY": "k", "VAL": "v"})
    assert model.model_dump() == {"${KEY}": "v", "n": 5}


def test_missing_variable_names_it_and_its_key_but_no_values(tmp_path: Path) -> None:
    path = _file(
        tmp_path, "p.yaml", "name: x\nlimits: {max_pages: 1}\ntags: ['${API_TOKEN}', '${MISSING_ONE}']\n"
    )
    env = {"API_TOKEN": "sk-super-secret-value", "OTHER_SECRET": "pw-123"}
    with pytest.raises(ConfigError, match="undefined variable MISSING_ONE with no default") as info:
        load_config(path, Profile, env=env)
    rendered = str(info.value)
    assert info.value.context == {"source": str(path), "key": "tags.1"}
    for secret in env.values():
        assert secret not in rendered


@pytest.mark.parametrize("text", ["${", "${1BAD}", "${NAME", "${NAME:default}", "${ NAME }", "pre ${} post"])
def test_malformed_references_are_refused(tmp_path: Path, text: str) -> None:
    path = _file(tmp_path, "p.json", json.dumps({"name": text, "limits": {"max_pages": 1}}))
    with pytest.raises(ConfigError, match="malformed") as info:
        load_config(path, Profile, env={"NAME": "n"})
    assert info.value.context["key"] == "name"


# --- deep_merge and load_layered ---------------------------------------------------------


def test_deep_merge_rules() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1, "c": {"keep": True}}
    overlay = {"a": {"y": [3], "z": None}, "b": {"now": "dict"}, "c": None, "d": 4}
    assert deep_merge(base, overlay) == {
        "a": {"x": 1, "y": [3], "z": None},
        "b": {"now": "dict"},
        "c": None,
        "d": 4,
    }


def test_deep_merge_never_mutates_or_aliases_its_inputs() -> None:
    base: dict[str, Any] = {"a": {"x": [1]}, "only_base": {"deep": [1]}}
    overlay: dict[str, Any] = {"a": {"y": [2]}, "only_overlay": {"deep": [2]}}
    base_copy, overlay_copy = copy.deepcopy(base), copy.deepcopy(overlay)
    merged = deep_merge(base, overlay)
    merged["a"]["x"].append(99)
    merged["a"]["y"].append(99)
    merged["only_base"]["deep"].append(99)
    merged["only_overlay"]["deep"].append(99)
    assert base == base_copy and overlay == overlay_copy


def test_load_layered_later_files_win_and_only_the_result_is_validated(tmp_path: Path) -> None:
    base = _file(tmp_path, "base.yaml", BASE_YAML)
    overlay = _file(tmp_path, "client.json", json.dumps({"limits": {"threshold": 0.9}, "tags": ["z"]}))
    third = _file(tmp_path, "local.yml", "mode: careful\n")
    profile = load_layered([base, overlay, third], Profile)
    assert profile.limits.model_dump() == {"max_pages": 10, "threshold": 0.9}
    assert profile.tags == ("z",) and profile.mode is Mode.careful and profile.name == "demo"


def test_load_layered_validation_error_names_every_file(tmp_path: Path) -> None:
    base = _file(tmp_path, "base.yaml", BASE_YAML)
    overlay = _file(tmp_path, "over.yaml", "limits: {max_pages: many}\n")
    with pytest.raises(ConfigError, match="limits.max_pages") as info:
        load_layered([base, overlay], Profile)
    assert info.value.context["source"] == [str(base), str(overlay)]


def test_load_layered_reports_the_failing_layer(tmp_path: Path) -> None:
    base = _file(tmp_path, "base.yaml", BASE_YAML)
    broken = _file(tmp_path, "broken.yaml", "name: [\n")
    with pytest.raises(ConfigError, match="invalid YAML") as info:
        load_layered([base, broken], Profile)
    assert info.value.context["source"] == str(broken)


def test_load_layered_interpolates_each_layer(tmp_path: Path) -> None:
    base = _file(tmp_path, "base.yaml", BASE_YAML)
    overlay = _file(tmp_path, "over.yaml", "name: '${WHO}'\n")
    assert load_layered([base, overlay], Profile, env={"WHO": "w"}).name == "w"


def test_load_layered_needs_a_file() -> None:
    with pytest.raises(ConfigError, match="at least one config file"):
        load_layered([], Profile)


# --- config_hash --------------------------------------------------------------------------


def test_config_hash_ignores_key_order_and_explicit_defaults(tmp_path: Path) -> None:
    short = load_config(_file(tmp_path, "a.yaml", BASE_YAML), Profile)
    spelled_out = load_config(
        _file(
            tmp_path,
            "b.yaml",
            "tags: [a, b]\nmode: fast\nlimits: {threshold: 0.5, max_pages: 10}\nname: demo\n",
        ),
        Profile,
    )
    assert config_hash(short) == config_hash(spelled_out)
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})


def test_config_hash_changes_when_a_value_changes(tmp_path: Path) -> None:
    first = load_config(_file(tmp_path, "a.yaml", BASE_YAML), Profile)
    second = load_config(_file(tmp_path, "b.yaml", BASE_YAML.replace("10", "11")), Profile)
    assert config_hash(first) != config_hash(second)
    assert len(config_hash(first)) == 64


def test_config_hash_of_model_equals_hash_of_its_json_dump(tmp_path: Path) -> None:
    profile = load_config(_file(tmp_path, "a.yaml", BASE_YAML), Profile)
    assert config_hash(profile) == config_hash(profile.model_dump(mode="json"))


# --- properties ---------------------------------------------------------------------------

scalars = st.none() | st.booleans() | st.integers() | st.text(max_size=5)
keys = st.text(alphabet="abcd", min_size=1, max_size=2)
trees = st.recursive(scalars, lambda children: st.dictionaries(keys, children, max_size=4), max_leaves=15)
dict_trees = st.dictionaries(keys, trees, max_size=4)


@given(dict_trees)
def test_merging_an_empty_overlay_or_onto_empty_is_identity(tree: dict[str, Any]) -> None:
    assert deep_merge(tree, {}) == tree
    assert deep_merge({}, tree) == tree


@st.composite
def same_shape_layers(draw: st.DrawFn) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Three partial trees over one shape, so each key path is a mapping in all or none."""
    shape = draw(st.recursive(st.just(None), lambda c: st.dictionaries(keys, c, min_size=1, max_size=3)))
    if shape is None:
        shape = {}

    def fill(node: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, child in node.items():
            if not draw(st.booleans()):
                continue
            out[key] = fill(child) if isinstance(child, dict) else draw(scalars)
        return out

    return fill(shape), fill(shape), fill(shape)


@given(same_shape_layers())
def test_deep_merge_is_associative_when_shapes_agree(layers: tuple[dict[str, Any], ...]) -> None:
    a, b, c = layers
    assert deep_merge(deep_merge(a, b), c) == deep_merge(a, deep_merge(b, c))


def test_deep_merge_is_not_associative_when_a_layer_resets_a_mapping() -> None:
    a, b, c = {"k": {"x": 1}}, {"k": 0}, {"k": {"y": 2}}
    assert deep_merge(deep_merge(a, b), c) == {"k": {"y": 2}}
    assert deep_merge(a, deep_merge(b, c)) == {"k": {"x": 1, "y": 2}}


@given(dict_trees, dict_trees)
def test_overlay_values_always_win_at_the_top_level(a: dict[str, Any], b: dict[str, Any]) -> None:
    merged = deep_merge(a, b)
    assert set(merged) == set(a) | set(b)
    for key, value in b.items():
        if not (isinstance(value, dict) and isinstance(a.get(key), dict)):
            assert merged[key] == value
