from __future__ import annotations

from pathlib import Path

import pytest

from mailroom_common.errors import ConfigError
from mailroom_common.yamlio import load_yaml, loads_yaml


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
