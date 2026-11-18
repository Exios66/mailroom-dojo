"""parse_field_type against the shipped field_types and entity_element_types dictionaries."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mailroom_common.errors import ConfigError
from mailroom_dictionaries import FieldType, VocabularyError, load_dictionary, parse_field_type


@pytest.mark.parametrize(
    ("text", "base", "element"),
    [
        ("name", "name", None),
        ("label", "label", None),
        ("date", "date", None),
        ("money", "money", None),
        ("id", "id", None),
        ("free_text", "free_text", None),
        ("containment", "containment", None),
        ("cms_doc_ref", "cms_doc_ref", None),
        ("entity_list", "entity_list", None),
        ("entity_list:name", "entity_list", "name"),
        ("entity_list:free_text", "entity_list", "free_text"),
        ("entity_list:id", "entity_list", "id"),
        ("entity_list:cms_doc_ref", "entity_list", "cms_doc_ref"),
    ],
)
def test_valid_field_types(text: str, base: str, element: str | None) -> None:
    parsed = parse_field_type(text)
    assert parsed == FieldType(base, element)
    assert str(parsed) == text


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("dates", "'dates' is not in dictionary 'field_types' version 1; did you mean 'date'"),
        ("entity_list:money", "'money' is not in dictionary 'entity_element_types'"),
        ("entity_list:entity_list", "'entity_list' is not in dictionary 'entity_element_types'"),
        ("entity_list:", "the element type after ':' is empty"),
        ("date:name", "'date' takes no element type"),
        ("entity_list:name:id", "'name:id' is not in dictionary 'entity_element_types'"),
        ("Entity List", "write 'Entity List' as the canonical key 'entity_list'"),
        ("entity_list:Free Text", "write 'Free Text' as the canonical key 'free_text'"),
        ("", "'' is not in dictionary 'field_types'"),
        (":name", "'' is not in dictionary 'field_types'"),
    ],
)
def test_invalid_field_types(text: str, match: str) -> None:
    with pytest.raises(VocabularyError, match=match) as info:
        parse_field_type(text)
    assert isinstance(info.value, ConfigError)


def test_non_string() -> None:
    with pytest.raises(VocabularyError, match="must be a string, got list"):
        parse_field_type(["date"])


@given(st.text(max_size=30))
def test_parse_never_returns_an_unknown_type(text: str) -> None:
    try:
        parsed = parse_field_type(text)
    except VocabularyError:
        return
    assert parsed.base in load_dictionary("field_types")
    assert parsed.element is None or parsed.element in load_dictionary("entity_element_types")
    assert str(parsed) == text
