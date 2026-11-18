"""The field scorer registry: built-ins, plug-ins declared by a profile, resolution and fallbacks."""

from __future__ import annotations

import pytest
from dojo_testkit import minimal_profile_mapping

from mailroom_dojo.field_scoring import scalar_scorers
from mailroom_dojo.field_scoring.plugins import BUNDLED_PLUGINS
from mailroom_dojo.field_scoring.plugins.cms_identifiers import score_cms_doc_ref
from mailroom_dojo.field_scoring.scorer_registry import (
    BUILTIN_SCORERS,
    FieldScorerRegistry,
    UnknownFieldTypeError,
    heuristic_field_type,
)
from mailroom_dojo.profile import load_scoring_profile
from mailroom_dojo.profile.field_types import BUILTIN_SCALAR_TYPES


def _exact_text(predicted: object, expected: object, rescue=None) -> float:
    """A client plug-in: exact text match."""
    return float(str(predicted) == str(expected))


def test_builtin_scorers_cover_exactly_the_builtin_types():
    """The loader's type names and the registry's scorers are the same set, each the documented function."""
    assert set(BUILTIN_SCORERS) == set(BUILTIN_SCALAR_TYPES)
    assert BUILTIN_SCORERS["date"] is scalar_scorers.score_date_field
    assert BUILTIN_SCORERS["containment"] is scalar_scorers.score_containment_field
    assert BUNDLED_PLUGINS["cms_doc_ref"] is score_cms_doc_ref


def test_every_mailroom_field_type_resolves(mailroom_profile):
    """Every declared, overridden and added field type of the mailroom profile has a scorer."""
    registry = FieldScorerRegistry.for_profile(mailroom_profile)
    types = set()
    for spec in mailroom_profile.classes:
        types.update(spec.field_types().values())
        for variant in spec.variants:
            types.update(variant.field_type_overrides.values())
            types.update(field.field_type for field in variant.added_fields)
        for field in spec.fields:
            if field.predicted_value_type_override is not None:
                types.add(field.predicted_value_type_override.field_type)
    assert "entity_list:cms_doc_ref" in types
    for field_type in types:
        resolved = registry.resolve(field_type)
        assert not resolved.fell_back and callable(resolved.scorer)
    assert registry.resolve("entity_list:cms_doc_ref").scorer is score_cms_doc_ref
    assert registry.resolve("entity_list").scalar_type == "name"


def test_builtin_registry_does_not_know_profile_plugins():
    """cms_doc_ref exists only when a profile declares it."""
    registry = FieldScorerRegistry.builtin()
    assert not registry.knows("cms_doc_ref") and not registry.knows("entity_list:cms_doc_ref")
    with pytest.raises(UnknownFieldTypeError, match="unregistered field type 'cms_doc_ref'"):
        registry.resolve("entity_list:cms_doc_ref")


def test_unknown_type_falls_back_only_when_asked_and_says_so():
    """Without ``unknown_as`` an unknown type raises; with it, the result is marked as a fallback."""
    registry = FieldScorerRegistry.builtin()
    with pytest.raises(UnknownFieldTypeError):
        registry.resolve("vin")
    resolved = registry.resolve("entity_list:vin", unknown_as="name")
    assert (resolved.is_list, resolved.scalar_type, resolved.fell_back) == (True, "name", True)
    assert resolved.scorer is scalar_scorers.score_name_field
    with pytest.raises(UnknownFieldTypeError):
        registry.resolve("vin", unknown_as="also_unknown")


def test_client_plugins_are_declared_in_the_profile_and_supplied_in_code():
    """A plug-in the profile declares must be supplied (or bundled); an undeclared one is refused."""
    declared = load_scoring_profile(minimal_profile_mapping(scorer_plugins=["vin"]))
    with pytest.raises(UnknownFieldTypeError, match="declares plug-in 'vin'"):
        FieldScorerRegistry.for_profile(declared)
    registry = FieldScorerRegistry.for_profile(declared, {"vin": _exact_text})
    assert registry.resolve("entity_list:vin").scorer is _exact_text
    plain = load_scoring_profile(minimal_profile_mapping())
    with pytest.raises(ValueError, match=r"\['vin'\] are not declared"):
        FieldScorerRegistry.for_profile(plain, {"vin": _exact_text})


def test_a_supplied_plugin_overrides_the_bundled_one(mailroom_profile):
    """A caller can swap the bundled CMS scorer for its own under the declared name."""
    registry = FieldScorerRegistry.for_profile(mailroom_profile, {"cms_doc_ref": _exact_text})
    assert registry.scalar_scorer("cms_doc_ref") is _exact_text


def test_with_plugin_returns_a_new_registry_and_never_replaces():
    """Adding a type leaves the original registry untouched; re-adding a type is refused."""
    registry = FieldScorerRegistry.builtin()
    extended = registry.with_plugin("vin", _exact_text)
    assert extended.knows("vin") and not registry.knows("vin")
    with pytest.raises(ValueError, match="already registered"):
        extended.with_plugin("name", _exact_text)
    assert extended.scalar_types == frozenset(BUILTIN_SCALAR_TYPES) | {"vin"}


@pytest.mark.parametrize("name", ["", "entity_list", "entity_list:vin", "a:b", 7])
def test_bad_type_names_are_refused(name):
    """Type names cannot be empty, the list prefix, or contain a colon."""
    with pytest.raises(ValueError, match="invalid scalar field type name"):
        FieldScorerRegistry({name: _exact_text})


def test_non_callable_scorer_is_refused():
    """A scorer must be callable."""
    with pytest.raises(TypeError, match="not callable"):
        FieldScorerRegistry({"vin": "exact"})


@pytest.mark.parametrize(
    ("field_name", "value", "expected"),
    [
        ("parties", ["A"], "entity_list"),
        ("effective_date", "x", "date"),
        ("Update_Date", None, "date"),
        ("purchase_price", "1", "money"),
        ("total_due", "1", "money"),
        ("policy_number", "1", "id"),
        ("docket", "1", "id"),
        ("valid", "1", "id"),  # "id" inside "valid": the old substring rule, kept
        ("insured", "x", "name"),
        ("candidate", "x", "date"),  # "date" inside "candidate", kept
    ],
)
def test_heuristic_field_type(field_name, value, expected):
    """Unmapped fields are typed by value shape and then by substrings of the name, in that order."""
    assert heuristic_field_type(field_name, value) == expected
