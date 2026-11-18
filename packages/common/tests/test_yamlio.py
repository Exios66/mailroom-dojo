from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from mailroom_common.errors import ConfigError
from mailroom_common.yamlio import load_yaml, loads_yaml


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("010", 10),
        ("-007", -7),
        ("+1", 1),
        ("0", 0),
        ("0o17", 15),
        ("0x1F", 31),
        ("1e3", 1000.0),
        ("1.0e+3", 1000.0),
        ("-2E-2", -0.02),
        (".5", 0.5),
        ("1.", 1.0),
        ("1.10", 1.1),
        ("1:30", "1:30"),
        ("1_000", "1_000"),
        ("0b101", "0b101"),
        ("=", "="),
        ("1e", "1e"),
        ("0x", "0x"),
    ],
)
def test_numbers_follow_the_yaml_1_2_core_schema(text: str, expected: Any) -> None:
    value = loads_yaml(f"n: {text}\n")["n"]
    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.parametrize("text", [".inf", "-.Inf", "+.INF"])
def test_infinities_are_floats(text: str) -> None:
    value = loads_yaml(f"n: {text}\n")["n"]
    assert isinstance(value, float)
    assert math.isinf(value)


def test_nan_is_a_float() -> None:
    assert math.isnan(loads_yaml("n: .nan\n")["n"])


@pytest.mark.parametrize(("text", "tag"), [("k: !!binary aGk=\n", "binary"), ("s: !!set {a}\n", "set")])
def test_binary_and_set_tags_are_refused(text: str, tag: str) -> None:
    with pytest.raises(ConfigError, match=f"!!{tag} is not allowed") as info:
        loads_yaml(text, source="vocab.yaml")
    assert info.value.context == {"source": "vocab.yaml", "line": 1}


def test_plain_mapping_and_lists_load() -> None:
    assert loads_yaml("a: 1\nb: [x, y]\n") == {"a": 1, "b": ["x", "y"]}


def test_duplicate_key_is_refused_with_its_line() -> None:
    with pytest.raises(ConfigError, match="duplicate mapping key 'a'") as info:
        loads_yaml("a: 1\nb: 2\na: 3\n", source="x.yaml")
    assert info.value.context["line"] == 3
    assert info.value.context["source"] == "x.yaml"


def test_duplicate_key_in_nested_mapping_is_refused() -> None:
    with pytest.raises(ConfigError, match="duplicate"):
        loads_yaml("outer:\n  k: 1\n  k: 2\n")


@pytest.mark.parametrize("word", ["no", "on", "off", "yes", "y", "n", "No", "NO", "On"])
def test_yaml_1_1_boolean_words_stay_strings(word: str) -> None:
    assert loads_yaml(f"values: [{word}, other]") == {"values": [word, "other"]}


@pytest.mark.parametrize(("text", "expected"), [("true", True), ("False", False), ("TRUE", True)])
def test_true_and_false_are_booleans(text: str, expected: bool) -> None:
    assert loads_yaml(text) is expected


def test_non_string_key_is_refused() -> None:
    with pytest.raises(ConfigError, match="must be a string"):
        loads_yaml("1: a\n")


def test_unsafe_python_tags_are_refused() -> None:
    with pytest.raises(ConfigError):
        loads_yaml("x: !!python/object/apply:os.system ['true']\n")


def test_syntax_error_reports_line() -> None:
    with pytest.raises(ConfigError) as info:
        loads_yaml("a: [1, 2\nb: 3\n")
    assert info.value.context["line"] is not None


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read") as info:
        load_yaml(tmp_path / "nope.yaml")
    assert str(tmp_path / "nope.yaml") == info.value.context["source"]


def test_non_utf8_file_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_bytes(b"a: \xff\xfe\n")
    with pytest.raises(ConfigError, match="cannot read"):
        load_yaml(path)


def test_anchors_and_merge_keys_still_work() -> None:
    text = "base: &b {x: 1}\nchild:\n  <<: *b\n  y: 2\n"
    assert loads_yaml(text)["child"] == {"x": 1, "y": 2}


def test_explicit_key_overrides_a_merged_key() -> None:
    text = "base: &b {x: 1, y: 1}\nchild:\n  <<: *b\n  x: 2\n"
    assert loads_yaml(text)["child"] == {"x": 2, "y": 1}


def test_explicit_key_before_the_merge_key_still_wins() -> None:
    text = "base: &b {x: 1}\nchild:\n  x: 2\n  <<: *b\n"
    assert loads_yaml(text)["child"] == {"x": 2}


def test_earlier_merge_source_wins_over_later_ones() -> None:
    text = "a: &a {x: 1}\nb: &b {x: 2, y: 2}\nchild:\n  <<: [*a, *b]\n"
    assert loads_yaml(text)["child"] == {"x": 1, "y": 2}


def test_two_written_keys_next_to_a_merge_are_still_a_duplicate() -> None:
    with pytest.raises(ConfigError, match="duplicate mapping key 'x'"):
        loads_yaml("base: &b {x: 1}\nchild:\n  <<: *b\n  x: 2\n  x: 3\n")


def test_merge_of_a_scalar_is_refused() -> None:
    with pytest.raises(ConfigError, match="merge key"):
        loads_yaml("child:\n  <<: 5\n")


def test_nested_merge_keys_resolve() -> None:
    text = "a: &a {x: 1}\nb: &b\n  <<: *a\n  y: 2\nc:\n  <<: *b\n  x: 9\n"
    assert loads_yaml(text)["c"] == {"x": 9, "y": 2}
