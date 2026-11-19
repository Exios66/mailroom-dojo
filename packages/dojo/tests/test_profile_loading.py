"""Loading the scoring profile: the mailroom content, every validation failure, immutability and the content
hash."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from dojo_testkit import MAILROOM_PROFILE_PATH, mailroom_profile_mapping, minimal_profile_mapping
from mailroom_dojo.profile import (
    ClassSpec,
    FieldSpec,
    FrozenMapping,
    LabelVocabulary,
    ProfileError,
    ScoringProfile,
    load_scoring_profile,
    profile_canonical_form,
)
from mailroom_dojo.profile.loading import _canonical_json


def _class_index(mapping: dict[str, Any], key: str) -> int:
    """Return the list index of a class in a profile mapping."""
    return next(index for index, spec in enumerate(mapping["classes"]) if spec["key"] == key)


def _expect_error(mapping: Any, path: str, words: str = "") -> ProfileError:
    """Load ``mapping`` and assert a ProfileError at exactly ``path`` whose message contains ``words``."""
    with pytest.raises(ProfileError) as caught:
        load_scoring_profile(mapping)
    assert caught.value.path == path, str(caught.value)
    assert words in caught.value.problem, str(caught.value)
    assert str(caught.value).startswith(f"{path}: " if path else "")
    return caught.value


# ---------------------------------------------------------------------------
# The mailroom profile reproduces the old Dojo's content
# ---------------------------------------------------------------------------


def test_mailroom_profile_classes_in_registry_order(mailroom_profile):
    """Eight classes in the old registry order; three retired."""
    assert mailroom_profile.class_keys == (
        "contract",
        "corporate_record",
        "due_diligence",
        "correspondence",
        "compliance_filing",
        "court_opinion",
        "insurance_claim",
        "merger_agreement",
    )
    assert mailroom_profile.live_class_keys == (
        "contract",
        "corporate_record",
        "correspondence",
        "insurance_claim",
        "merger_agreement",
    )
    assert set(mailroom_profile.retired_class_keys) == {"due_diligence", "compliance_filing", "court_opinion"}


def test_mailroom_profile_classification_order_puts_specific_classes_first(mailroom_profile):
    """The old pattern order, then the classes the old table lacked."""
    assert mailroom_profile.classification_match_order == (
        "merger_agreement",
        "corporate_record",
        "correspondence",
        "compliance_filing",
        "insurance_claim",
        "contract",
        "due_diligence",
        "court_opinion",
    )


def test_mailroom_profile_settings_equal_the_old_defaults(mailroom_profile):
    """Every threshold and switch equals llm-dojo-scoring 0.30.1's Settings defaults."""
    profile = mailroom_profile
    assert profile.thresholds.ambiguous_band == (0.5, 0.85)
    assert dict(profile.thresholds.type_bands) == {}
    assert profile.thresholds.list_match_threshold == 0.6
    assert (profile.audit.enabled, profile.audit.token_coverage) == (True, 0.7)
    assert (profile.presence.route_by_reasoning, profile.presence.embedding_threshold) == (True, 0.7)
    assert (profile.embedding.enabled, profile.embedding.rescue_below) == (False, 0.7)
    trace = profile.trace_capture
    assert (
        trace.capture_confidence,
        trace.capture_reasoning,
        trace.confidence_min,
        trace.confidence_band,
        trace.compute_calibration_error,
        trace.missing_confidence,
    ) == (True, True, None, None, True, "absent")
    assert profile.unmapped_fields == "heuristic"
    assert profile.unknown_label == "other"
    assert profile.scorer_plugins == ("cms_doc_ref",)
    assert len(profile.list_matching.role_words) == 23
    assert profile.field_name_defaults.containment == {
        "governing_law",
        "term_length",
        "renewal_terms",
        "subject_matter",
    }
    assert profile.field_name_defaults.recall_coverage == {
        "parties",
        "keywords",
        "cuad_clauses",
        "maud_clauses",
        "claim_checklist",
        "action_items",
    }
    assert profile.retired_fields == {
        "key_obligations",
        "termination_clauses",
        "key_provisions",
        "key_points",
        "referenced_communications",
    }
    assert len(profile.never_scored_fields) == 18 and "expected_subclass" in profile.never_scored_fields


def test_mailroom_field_maps_match_the_old_registry(mailroom_profile):
    """Field types per class; corporate_record holds the 40-field map the old specialist suite scored with."""
    assert len(mailroom_profile.field_types("contract")) == 17
    assert len(mailroom_profile.field_types("correspondence")) == 11
    assert len(mailroom_profile.field_types("insurance_claim")) == 17
    assert len(mailroom_profile.field_types("merger_agreement")) == 21
    corporate = mailroom_profile.field_types("corporate_record")
    assert len(corporate) == 40
    assert list(corporate)[:9] == [
        "entity_name",
        "record_type",
        "effective_date",
        "signatories",
        "jurisdiction",
        "filing_number",
        "intent",
        "subject_matter",
        "keywords",
    ]
    assert corporate["registered_agent"] == "name"
    assert mailroom_profile.require_class("corporate_record").field("registered_agent").subclasses == (
        "articles_of_incorporation",
        "certificate_of_formation",
    )
    assert mailroom_profile.field_types("insurance_claim")["supporting_documents"] == "entity_list"


def test_name_rules_reach_declared_and_undeclared_fields(mailroom_profile):
    """Containment and recall rules apply by name to declared fields and to keys a class lacks."""
    assert mailroom_profile.compares_by_containment("contract", "governing_law")
    assert mailroom_profile.compares_by_containment("contract", "subject_matter")  # not a contract field
    assert mailroom_profile.compares_by_containment("audit", "subject_matter")  # not a class
    assert not mailroom_profile.compares_by_containment("contract", "document_name")
    assert mailroom_profile.scores_coverage_by_recall("corporate_record", "parties")  # not declared there
    assert mailroom_profile.scores_coverage_by_recall("insurance_claim", "claim_checklist")
    assert not mailroom_profile.scores_coverage_by_recall("contract", "signatories")


def test_cms_variant_is_data(mailroom_profile):
    """The CMS special case is a row variant with a marker, a guard, overrides and an added field."""
    insurance = mailroom_profile.require_class("insurance_claim")
    (variant,) = insurance.variants
    assert variant.name == "cms_desynpuf"
    assert variant.marker.field == "cms_source_table"
    assert set(variant.marker.values) == {"carrier", "inpatient", "outpatient", "pde"}
    assert variant.marker.fallback_fields == ("claim_type",)
    assert variant.marker.skip_when_unstated and variant.marker.derive_from == "subclass"
    assert (variant.guard.field, variant.guard.allowed, variant.guard.allow_empty) == (
        "claim_type",
        ("health",),
        True,
    )
    assert variant.documented_absent_fields == ("adjuster", "denial_reasons")
    assert dict(variant.field_type_overrides) == {
        "claim_type": "label",
        "cms_source_table": "label",
        "supporting_documents": "entity_list:cms_doc_ref",
    }
    assert variant.added_field("cms_source_table").vocabulary.tokens == (
        "carrier",
        "inpatient",
        "outpatient",
        "pde",
    )
    assert insurance.field("cms_source_table") is None
    override = insurance.field("claim_type").predicted_value_type_override
    assert override.field_type == "label" and override.matches(" PDE ") and not override.matches("health")
    assert mailroom_profile.label_vocabulary("insurance_claim", "cms_source_table") is not None


def test_accepted_unscored_fields_are_on_contract_only(mailroom_profile):
    """merger_consideration and maud_clauses are accepted, never scored, on a contract; merger scores them."""
    assert mailroom_profile.require_class("contract").accepted_unscored_fields == {
        "merger_consideration",
        "maud_clauses",
    }
    assert "maud_clauses" in mailroom_profile.field_types("merger_agreement")


def test_every_label_field_has_a_vocabulary_and_every_vocabulary_token_folds_to_itself(mailroom_profile):
    """A label field always folds, and its own tokens are fixed points of the fold."""
    for spec in mailroom_profile.classes:
        for field_spec in spec.fields + tuple(f for v in spec.variants for f in v.added_fields):
            if field_spec.field_type == "label":
                assert field_spec.vocabulary is not None, (spec.key, field_spec.name)
                for token in field_spec.vocabulary.tokens:
                    assert field_spec.vocabulary.fold(token) == token, (spec.key, field_spec.name, token)


def test_unknown_class_lookups(mailroom_profile):
    """Unknown classes give None, an empty fold and the unknown subclass; require_class names the known
    ones."""
    assert mailroom_profile.class_spec("audit") is None
    assert mailroom_profile.class_spec(None) is None
    assert mailroom_profile.canonical_label("audit", "intent", "notice") == ""
    assert mailroom_profile.normalize_subclass("audit", "email") == "other"
    assert mailroom_profile.normalize_subclass("due_diligence", "anything") == "other"
    with pytest.raises(KeyError, match="known: .*contract"):
        mailroom_profile.require_class("audit")


# ---------------------------------------------------------------------------
# Failure states: each names its YAML path
# ---------------------------------------------------------------------------


def _mutate(edit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Return the mailroom mapping after ``edit``."""
    mapping = mailroom_profile_mapping()
    edit(mapping)
    return mapping


def _insurance(mapping: dict[str, Any]) -> dict[str, Any]:
    """The insurance_claim class mapping."""
    return mapping["classes"][_class_index(mapping, "insurance_claim")]


def _cms_variant(mapping: dict[str, Any]) -> dict[str, Any]:
    """The CMS row variant mapping."""
    return _insurance(mapping)["variants"][0]


INSURANCE = "classes[6]"

FAILURES: list[tuple[str, Callable[[dict[str, Any]], None], str, str]] = [
    ("unknown top-level key", lambda m: m.update(thresholdz={}), "thresholdz", "unknown key"),
    (
        "unknown threshold key",
        lambda m: m["thresholds"].update(band=[0, 1]),
        "thresholds.band",
        "unknown key",
    ),
    ("unknown class key", lambda m: _insurance(m).update(colour="red"), f"{INSURANCE}.colour", "unknown key"),
    (
        "unknown field key",
        lambda m: _insurance(m)["fields"]["claim_type"].update(weight=2),
        f"{INSURANCE}.fields.claim_type.weight",
        "unknown key",
    ),
    (
        "unknown vocabulary key",
        lambda m: _insurance(m)["fields"]["claim_type"]["vocabulary"].update(case="upper"),
        f"{INSURANCE}.fields.claim_type.vocabulary.case",
        "unknown key",
    ),
    (
        "unknown variant key",
        lambda m: _cms_variant(m).update(priority=1),
        f"{INSURANCE}.variants[0].priority",
        "unknown key",
    ),
    (
        "unknown trace key",
        lambda m: m["trace_capture"].update(capture_everything=True),
        "trace_capture.capture_everything",
        "unknown key",
    ),
    ("missing client", lambda m: m.pop("client"), "client", "is required"),
    ("missing classes", lambda m: m.pop("classes"), "classes", "is required"),
    ("empty classes", lambda m: m.update(classes=[]), "classes", "must not be empty"),
    (
        "missing field type",
        lambda m: _insurance(m)["fields"].update(adjuster={"compare": "default"}),
        f"{INSURANCE}.fields.adjuster.type",
        "is required",
    ),
    ("wrong format", lambda m: m.update(profile_format=2), "profile_format", "reads format 1"),
    ("boolean format", lambda m: m.update(profile_format=True), "profile_format", "reads format 1"),
    (
        "unregistered scalar type",
        lambda m: _insurance(m)["fields"].update(adjuster="lable"),
        f"{INSURANCE}.fields.adjuster.type",
        "unregistered field type 'lable'",
    ),
    (
        "unregistered element type",
        lambda m: _insurance(m)["fields"].update(keywords="entity_list:nam"),
        f"{INSURANCE}.fields.keywords.type",
        "unregistered element type 'nam'",
    ),
    (
        "nested list type",
        lambda m: _insurance(m)["fields"].update(keywords="entity_list:entity_list:name"),
        f"{INSURANCE}.fields.keywords.type",
        "unregistered element type",
    ),
    (
        "plug-in type not declared",
        lambda m: m.update(scorer_plugins=[]),
        f"{INSURANCE}.variants[0].field_type_overrides.supporting_documents",
        "unregistered element type 'cms_doc_ref'",
    ),
    (
        "label without vocabulary",
        lambda m: _insurance(m)["fields"].update(urgency="label"),
        f"{INSURANCE}.fields.urgency",
        "needs a vocabulary",
    ),
    (
        "vocabulary on a non-label field",
        lambda m: _insurance(m)["fields"].update(insurer={"type": "name", "vocabulary": {"tokens": ["a"]}}),
        f"{INSURANCE}.fields.insurer.vocabulary",
        "only label fields",
    ),
    (
        "recall on a scalar",
        lambda m: _insurance(m)["fields"].update(insurer={"type": "name", "coverage": "recall"}),
        f"{INSURANCE}.fields.insurer.coverage",
        "entity lists",
    ),
    (
        "containment on a date",
        lambda m: _insurance(m)["fields"].update(date_filed={"type": "date", "compare": "containment"}),
        f"{INSURANCE}.fields.date_filed.compare",
        "containment applies",
    ),
    (
        "duplicate class",
        lambda m: m["classes"].append(copy.deepcopy(m["classes"][0])),
        "classes[8].key",
        "duplicate class 'contract'",
    ),
    (
        "duplicate sorter label",
        lambda m: m["classes"][1].update(sorter_label="contract"),
        "classes[1].sorter_label",
        "already used by classes[0]",
    ),
    ("bad class key", lambda m: m["classes"][0].update(key="Contract"), "classes[0].key", "snake_case"),
    ("bad status", lambda m: m["classes"][0].update(status="paused"), "classes[0].status", "live, retired"),
    (
        "bad field name",
        lambda m: _insurance(m)["fields"].update({"Claim Number": "id"}),
        f"{INSURANCE}.fields.Claim Number",
        "snake_case",
    ),
    (
        "variant marker field not in class",
        lambda m: _cms_variant(m)["marker"].update(field="cms_table"),
        f"{INSURANCE}.variants[0].marker.field",
        "not a field of the class or of this variant",
    ),
    (
        "variant fallback field not in class",
        lambda m: _cms_variant(m)["marker"].update(fallback_fields=["claimtype"]),
        f"{INSURANCE}.variants[0].marker.fallback_fields[0]",
        "not a field",
    ),
    (
        "variant guard field not in class",
        lambda m: _cms_variant(m)["guard"].update(field="product_line"),
        f"{INSURANCE}.variants[0].guard.field",
        "not a field",
    ),
    (
        "documented absent field not in class",
        lambda m: _cms_variant(m).update(documented_absent_fields=["adjuster", "denial_reason"]),
        f"{INSURANCE}.variants[0].documented_absent_fields[1]",
        "not a field",
    ),
    (
        "override field not in class",
        lambda m: _cms_variant(m)["field_type_overrides"].update(support_docs="id"),
        f"{INSURANCE}.variants[0].field_type_overrides.support_docs",
        "not a field",
    ),
    (
        "override to an unregistered type",
        lambda m: _cms_variant(m)["field_type_overrides"].update(claim_type="lbl"),
        f"{INSURANCE}.variants[0].field_type_overrides.claim_type",
        "unregistered field type",
    ),
    (
        "marker value outside the marker vocabulary",
        lambda m: _cms_variant(m)["marker"].update(values=["carrier", "dme"]),
        f"{INSURANCE}.variants[0].marker.values[1]",
        "not a token",
    ),
    (
        "marker value not a subclass",
        lambda m: (
            _cms_variant(m)["added_fields"]["cms_source_table"]["vocabulary"].update(
                tokens=["carrier", "inpatient", "outpatient", "pde", "dme"]
            )
            or _cms_variant(m)["marker"].update(values=["carrier", "dme"])
        ),
        f"{INSURANCE}.variants[0].marker.values[1]",
        "not a subclass",
    ),
    (
        "uppercase marker value",
        lambda m: _cms_variant(m)["marker"].update(values=["Carrier"]),
        f"{INSURANCE}.variants[0].marker.values[0]",
        "lowercase",
    ),
    (
        "added field already in class",
        lambda m: _cms_variant(m)["added_fields"].update(adjuster="name"),
        f"{INSURANCE}.variants[0].added_fields.adjuster",
        "already a field",
    ),
    (
        "duplicate variant",
        lambda m: _insurance(m)["variants"].append(copy.deepcopy(_cms_variant(m))),
        f"{INSURANCE}.variants[1].name",
        "duplicate variant",
    ),
    (
        "threshold as text",
        lambda m: m["thresholds"].update(list_match_threshold="0.6"),
        "thresholds.list_match_threshold",
        "must be a number",
    ),
    (
        "threshold as boolean",
        lambda m: m["audit"].update(token_coverage=True),
        "audit.token_coverage",
        "must be a number",
    ),
    (
        "threshold NaN",
        lambda m: m["embedding"].update(rescue_below=float("nan")),
        "embedding.rescue_below",
        "finite",
    ),
    (
        "threshold infinite",
        lambda m: m["presence"].update(embedding_threshold=float("inf")),
        "presence.embedding_threshold",
        "finite",
    ),
    (
        "threshold above one",
        lambda m: m["thresholds"].update(list_match_threshold=1.5),
        "thresholds.list_match_threshold",
        "between 0.0 and 1.0",
    ),
    (
        "threshold below zero",
        lambda m: m["audit"].update(token_coverage=-0.1),
        "audit.token_coverage",
        "between",
    ),
    (
        "band reversed",
        lambda m: m["thresholds"].update(ambiguous_band=[0.9, 0.5]),
        "thresholds.ambiguous_band",
        "above high",
    ),
    (
        "band too short",
        lambda m: m["thresholds"].update(ambiguous_band=[0.5]),
        "thresholds.ambiguous_band",
        "two numbers",
    ),
    (
        "band member text",
        lambda m: m["thresholds"].update(ambiguous_band=[0.5, "high"]),
        "thresholds.ambiguous_band[1]",
        "must be a number",
    ),
    (
        "type band for an unknown type",
        lambda m: m["thresholds"].update(type_bands={"nmae": "always"}),
        "thresholds.type_bands.nmae",
        "unregistered field type",
    ),
    (
        "type band bad value",
        lambda m: m["thresholds"].update(type_bands={"name": "sometimes"}),
        "thresholds.type_bands.name",
        "two numbers",
    ),
    (
        "confidence band reversed",
        lambda m: m["trace_capture"].update(confidence_band=[0.8, 0.2]),
        "trace_capture.confidence_band",
        "above high",
    ),
    (
        "missing confidence policy",
        lambda m: m["trace_capture"].update(missing_confidence="assume_half"),
        "trace_capture.missing_confidence",
        "absent, assume_1, assume_0",
    ),
    ("boolean as text", lambda m: m["audit"].update(enabled="true"), "audit.enabled", "true or false"),
    (
        "unmapped policy",
        lambda m: m.update(unmapped_fields="guess"),
        "unmapped_fields",
        "heuristic, skip, error",
    ),
    (
        "bad synonym regex",
        lambda m: m["classes"][0].update(synonyms=["agreement("]),
        "classes[0].synonyms[0]",
        "regular expression",
    ),
    (
        "vocabulary tokens colliding",
        lambda m: _insurance(m)["fields"]["claim_type"]["vocabulary"].update(tokens=["auto", "Auto"]),
        f"{INSURANCE}.fields.claim_type.vocabulary.tokens[1]",
        "same compact form",
    ),
    (
        "vocabulary duplicate token",
        lambda m: _insurance(m)["fields"]["claim_type"]["vocabulary"].update(tokens=["auto", "auto"]),
        f"{INSURANCE}.fields.claim_type.vocabulary.tokens[1]",
        "duplicate",
    ),
    (
        "vocabulary token without letters",
        lambda m: _insurance(m)["fields"]["claim_type"]["vocabulary"].update(tokens=["auto", "--"]),
        f"{INSURANCE}.fields.claim_type.vocabulary.tokens[1]",
        "no letters",
    ),
    (
        "alias target not a token",
        lambda m: _insurance(m)["fields"]["coverage_determination"]["vocabulary"]["aliases"].update(
            paid="aproved"
        ),
        f"{INSURANCE}.fields.coverage_determination.vocabulary.aliases.paid",
        "not a token",
    ),
    (
        "aliases on an exact fold",
        lambda m: _insurance(m)["fields"]["claim_type"]["vocabulary"].update(aliases={"car": "auto"}),
        f"{INSURANCE}.fields.claim_type.vocabulary.aliases",
        "need fold",
    ),
    (
        "uppercase alias",
        lambda m: _insurance(m)["fields"]["coverage_determination"]["vocabulary"]["aliases"].update(
            {"Paid In Full": "approved"}
        ),
        f"{INSURANCE}.fields.coverage_determination.vocabulary.aliases.Paid In Full",
        "lowercase",
    ),
    (
        "aliases colliding on compact form",
        lambda m: _insurance(m)["fields"]["coverage_determination"]["vocabulary"]["aliases"].update(
            {"notcovered": "approved"}
        ),
        f"{INSURANCE}.fields.coverage_determination.vocabulary.aliases.notcovered",
        "same compact form",
    ),
    (
        "inventory alias target typo",
        lambda m: _insurance(m)["fields"]["intent"]["vocabulary"].update(aliases={"fnol": "claim_filng"}),
        f"{INSURANCE}.fields.intent.vocabulary.aliases.fnol",
        "any inventory vocabulary",
    ),
    (
        "prefix alias on an exact fold",
        lambda m: _insurance(m)["fields"]["claim_type"]["vocabulary"].update(prefix_aliases={"car": "auto"}),
        f"{INSURANCE}.fields.claim_type.vocabulary.prefix_aliases",
        "need fold inventory",
    ),
    (
        "catalog without catalog fold",
        lambda m: _insurance(m)["fields"]["claim_type"]["vocabulary"].update(
            catalog=[{"key": "auto", "label": "Auto"}]
        ),
        f"{INSURANCE}.fields.claim_type.vocabulary.catalog",
        "fold catalog",
    ),
    (
        "catalog fold without catalog",
        lambda m: _insurance(m)["fields"]["claim_type"]["vocabulary"].update(fold="catalog"),
        f"{INSURANCE}.fields.claim_type.vocabulary.catalog",
        "non-empty catalog",
    ),
    (
        "catalog key not a token",
        lambda m: m["classes"][0]["subclasses"]["catalog"].append(
            {"key": "lease", "label": "Lease Agreement"}
        ),
        "classes[0].subclasses.catalog[25].key",
        "not a token",
    ),
    (
        "catalog label without letters",
        lambda m: m["classes"][0]["subclasses"].update(catalog=[{"key": "ip", "label": "--"}]),
        "classes[0].subclasses.catalog[0].label",
        "no letters",
    ),
    (
        "unknown fold",
        lambda m: _insurance(m)["fields"]["claim_type"]["vocabulary"].update(fold="fuzzy"),
        f"{INSURANCE}.fields.claim_type.vocabulary.fold",
        "exact, aliases, inventory, catalog, allowed_set",
    ),
    (
        "subclass fold exact",
        lambda m: _insurance(m)["subclasses"].update(fold="exact"),
        f"{INSURANCE}.subclasses.fold",
        "catalog or allowed_set",
    ),
    (
        "field subclass unknown",
        lambda m: m["classes"][1]["fields"]["holder"].update(subclasses=["warrant"]),
        "classes[1].fields.holder.subclasses[0]",
        "not a subclass",
    ),
    (
        "accepted field is scored",
        lambda m: m["classes"][0].update(accepted_unscored_fields=["parties"]),
        "classes[0].accepted_unscored_fields",
        "scored field",
    ),
    (
        "match order unknown class",
        lambda m: m["classification"].update(match_order=["contracts"]),
        "classification.match_order[0]",
        "not a class",
    ),
    (
        "match order duplicate",
        lambda m: m["classification"].update(match_order=["contract", "contract"]),
        "classification.match_order[1]",
        "duplicate",
    ),
    (
        "class alias shadows a class",
        lambda m: m.update(class_aliases={"contract": "merger_agreement"}),
        "class_aliases.contract",
        "must not be a class key",
    ),
    (
        "class alias to nowhere",
        lambda m: m.update(class_aliases={"claim": "insurance_claims"}),
        "class_aliases.claim",
        "not a class",
    ),
    (
        "plug-in shadows a built-in",
        lambda m: m.update(scorer_plugins=["cms_doc_ref", "date"]),
        "scorer_plugins[1]",
        "replace a built-in",
    ),
    (
        "never-scored blank",
        lambda m: m.update(never_scored_fields=["gt_presence", " "]),
        "never_scored_fields[1]",
        "blank",
    ),
    ("client with spaces", lambda m: m.update(client=" mailroom "), "client", "surrounding whitespace"),
    ("section not a mapping", lambda m: m.update(audit=[True, 0.7]), "audit", "must be a mapping"),
    (
        "fields not a mapping",
        lambda m: _insurance(m).update(fields=["claim_number"]),
        f"{INSURANCE}.fields",
        "must be a mapping",
    ),
    ("non-string key", lambda m: m["thresholds"].update({True: 1}), "thresholds", "keys must be strings"),
]


@pytest.mark.parametrize(("case", "edit", "path", "words"), FAILURES, ids=[case[0] for case in FAILURES])
def test_malformed_profile_is_refused_naming_its_path(case, edit, path, words):
    """Each malformed profile raises ProfileError at exactly the offending YAML path."""
    _expect_error(_mutate(edit), path, words)


def test_insurance_claim_is_the_seventh_class():
    """The failure table above addresses insurance_claim as classes[6]."""
    assert _class_index(mailroom_profile_mapping(), "insurance_claim") == 6


def test_top_level_must_be_a_mapping(tmp_path):
    """A YAML file holding a list is a malformed profile; a list passed in code is a type error."""
    path = tmp_path / "scoring.yaml"
    path.write_text("- contract\n- correspondence\n")
    _expect_error(path, "", "must be a mapping")
    with pytest.raises(TypeError, match="path or a mapping, not list"):
        load_scoring_profile([])  # type: ignore[arg-type]


def test_duplicate_yaml_key_is_refused(tmp_path):
    """PyYAML would keep the last of two equal keys; the loader refuses instead and names the line."""
    text = MAILROOM_PROFILE_PATH.read_text().replace(
        "  list_match_threshold: 0.6", "  list_match_threshold: 0.6\n  list_match_threshold: 0.9"
    )
    path = tmp_path / "scoring.yaml"
    path.write_text(text)
    error = _expect_error(path, "", "duplicate key 'list_match_threshold'")
    assert "line" in error.problem


def test_a_yaml_merge_key_cannot_be_overridden_but_a_disjoint_merge_loads(tmp_path):
    """Merge keys (``<<``) are allowed, but a local key that repeats a merged one is a duplicate, not an
    override: the profile format has no precedence rule, so every key is stated once."""
    base = MAILROOM_PROFILE_PATH.read_text()
    assert "  token_coverage: 0.7\n" in base
    disjoint = base.replace("  token_coverage: 0.7\n", "  <<: {token_coverage: 0.7}\n")
    (tmp_path / "disjoint.yaml").write_text(disjoint)
    assert load_scoring_profile(tmp_path / "disjoint.yaml") == load_scoring_profile(MAILROOM_PROFILE_PATH)
    overriding = base.replace(
        "  token_coverage: 0.7\n", "  <<: {token_coverage: 0.8}\n  token_coverage: 0.7\n"
    )
    (tmp_path / "overriding.yaml").write_text(overriding)
    _expect_error(tmp_path / "overriding.yaml", "", "duplicate key 'token_coverage'")


@pytest.mark.parametrize("key", ["[a, b]", "{a: 1}"], ids=["sequence", "mapping"])
def test_unhashable_yaml_key_is_a_profile_error(tmp_path, key):
    """A YAML complex key (a sequence or a mapping) cannot be a dict key; the loader names its line instead
    of raising TypeError."""
    text = yaml.safe_dump(minimal_profile_mapping()) + f"? {key}\n: 1\n"
    path = tmp_path / "scoring.yaml"
    path.write_text(text)
    error = _expect_error(path, "", "unhashable key")
    assert "line" in error.problem


HUGE = 10**400  # a valid JSON and YAML integer, too large for a float


@pytest.mark.parametrize(
    ("edit", "path"),
    [
        (lambda m: m["thresholds"].update(list_match_threshold=HUGE), "thresholds.list_match_threshold"),
        (lambda m: m["thresholds"].update(list_match_threshold=-HUGE), "thresholds.list_match_threshold"),
        (lambda m: m["thresholds"].update(ambiguous_band=[0.5, HUGE]), "thresholds.ambiguous_band[1]"),
        (lambda m: m["presence"].update(embedding_threshold=HUGE), "presence.embedding_threshold"),
    ],
    ids=["threshold", "negative", "band", "presence"],
)
def test_integer_too_large_for_a_float_is_a_profile_error(edit, path):
    """float() of a huge integer raises OverflowError; the loader reports it as out of range at its path."""
    _expect_error(_mutate(edit), path, "must be between")


def test_integer_too_large_for_a_float_in_yaml_is_a_profile_error(tmp_path):
    """The same through a YAML file, where PyYAML reads the digits as an int."""
    text = MAILROOM_PROFILE_PATH.read_text().replace(
        "  list_match_threshold: 0.6", f"  list_match_threshold: {HUGE}"
    )
    assert str(HUGE) in text
    path = tmp_path / "scoring.yaml"
    path.write_text(text)
    _expect_error(path, "thresholds.list_match_threshold", "must be between")


def test_integer_too_long_to_read_in_yaml_is_a_profile_error(tmp_path):
    """PyYAML raises ValueError (Python's integer digit limit) on a 5,000 digit integer; that is a malformed
    file, reported as a ProfileError naming it."""
    path = tmp_path / "scoring.yaml"
    path.write_text(yaml.safe_dump(minimal_profile_mapping()) + "huge: " + "9" * 5000 + "\n")
    _expect_error(path, "", "scoring.yaml is not valid YAML")


@pytest.mark.parametrize(
    ("edit", "path", "words"),
    [
        (lambda m: m["embedding"].update(enabled=10**5000), "embedding.enabled", "true or false"),
        (lambda m: m.update(unmapped_fields=10**5000), "unmapped_fields", "must be one of"),
        (
            lambda m: m["thresholds"].update(ambiguous_band=10**5000),
            "thresholds.ambiguous_band",
            "two numbers",
        ),
        (lambda m: m.update(profile_format=10**5000), "profile_format", "reads format"),
        (lambda m: m["thresholds"].update({10**5000: 1}), "thresholds", "keys must be strings"),
    ],
    ids=["boolean", "choice", "band", "format", "key"],
)
def test_a_value_too_long_to_print_still_gives_a_profile_error(edit, path, words):
    """repr() of an integer over Python's digit limit raises ValueError; the error message must not."""
    error = _expect_error(_mutate(edit), path, words)
    assert "too large to show" in error.problem


def test_a_file_that_is_not_utf8_is_a_profile_error(tmp_path):
    """A file starting with a UTF-16 byte order mark cannot be read as UTF-8; that is a ProfileError naming
    the file, not a raw UnicodeDecodeError."""
    path = tmp_path / "scoring.yaml"
    path.write_bytes(b"\xff\xfe" + yaml.safe_dump(minimal_profile_mapping()).encode("utf-16-le"))
    _expect_error(path, "", "scoring.yaml is not UTF-8 text")


def test_yaml_nested_deeper_than_the_interpreter_allows_is_a_profile_error(tmp_path):
    """A value nested 5,000 levels deep makes PyYAML recurse past the interpreter's limit; that is a malformed
    file, reported as a ProfileError naming it, not a raw RecursionError."""
    path = tmp_path / "scoring.yaml"
    path.write_text(yaml.safe_dump(minimal_profile_mapping()) + "deep: " + "[" * 5000 + "]" * 5000 + "\n")
    _expect_error(path, "", "scoring.yaml is not valid YAML")


def test_an_impossible_yaml_timestamp_is_a_profile_error(tmp_path):
    """PyYAML raises ValueError building a timestamp with month 13; that is reported as a ProfileError."""
    path = tmp_path / "scoring.yaml"
    path.write_text(yaml.safe_dump(minimal_profile_mapping()) + "when: 2020-13-01\n")
    _expect_error(path, "", "scoring.yaml is not valid YAML")


def test_invalid_yaml_is_a_profile_error(tmp_path):
    """A YAML syntax error is reported as a ProfileError naming the file."""
    path = tmp_path / "scoring.yaml"
    path.write_text("classes: [\n")
    _expect_error(path, "", "scoring.yaml is not valid YAML")


def test_yaml_one_point_one_booleans_as_keys_are_refused(tmp_path):
    """``yes:`` and ``no:`` are booleans in YAML 1.1, so a field called ``no`` is refused, not silently
    renamed."""
    mapping = minimal_profile_mapping()
    text = yaml.safe_dump(mapping).replace("policy_number: id", "no: id")
    path = tmp_path / "scoring.yaml"
    path.write_text(text)
    _expect_error(path, "classes[0].fields", "keys must be strings")


def test_missing_file_is_file_not_found(tmp_path):
    """A missing profile file is not a malformed profile."""
    with pytest.raises(FileNotFoundError):
        load_scoring_profile(tmp_path / "absent.yaml")


def test_minimal_profile_gets_the_engine_defaults():
    """A profile that states nothing else gets the old Dojo's defaults."""
    profile = load_scoring_profile(minimal_profile_mapping())
    assert profile.thresholds.ambiguous_band == (0.5, 0.85)
    assert profile.thresholds.list_match_threshold == 0.6
    assert profile.audit.token_coverage == 0.7
    assert profile.embedding.enabled is False
    assert profile.unmapped_fields == "heuristic"
    assert profile.classification_match_order == ("auto_policy_declaration",)
    assert profile.class_spec("auto_policy_declaration").sorter_label == "auto_policy_declaration"


def test_class_aliases_resolve():
    """A class alias finds the class it names."""
    profile = load_scoring_profile(
        minimal_profile_mapping(class_aliases={"declaration": "auto_policy_declaration"})
    )
    assert profile.class_spec("declaration").key == "auto_policy_declaration"
    assert profile.field_types("declaration")["premium"] == "money"


# ---------------------------------------------------------------------------
# Immutability and hashing
# ---------------------------------------------------------------------------


def test_profile_and_its_parts_are_immutable(mailroom_profile):
    """No attribute, mapping or collection of a loaded profile can be changed in place."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        mailroom_profile.client = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        mailroom_profile.thresholds.list_match_threshold = 0.1
    spec = mailroom_profile.require_class("insurance_claim")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.fields[0].field_type = "name"
    with pytest.raises(TypeError):
        spec.variants[0].field_type_overrides["claim_type"] = "name"  # type: ignore[index]
    with pytest.raises(AttributeError):
        spec.variants[0].field_type_overrides._items = {}  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        mailroom_profile.never_scored_fields.add("x")  # type: ignore[attr-defined]
    assert isinstance(mailroom_profile.classes, tuple) and isinstance(spec.fields, tuple)
    vocabulary = spec.field("intent").vocabulary
    with pytest.raises(TypeError):
        vocabulary.aliases["fnol"] = "other"  # type: ignore[index]


def test_field_types_returns_a_fresh_dict(mailroom_profile):
    """Changing the returned map never changes the profile."""
    first = mailroom_profile.field_types("contract")
    first["document_name"] = "id"
    assert mailroom_profile.field_types("contract")["document_name"] == "name"


def test_profiles_are_hashable_and_equal_by_content(mailroom_profile):
    """Two loads of one file are equal, share a hash and collapse in a set."""
    again = load_scoring_profile(MAILROOM_PROFILE_PATH)
    assert again == mailroom_profile and hash(again) == hash(mailroom_profile)
    assert len({again, mailroom_profile}) == 1
    assert {hash(spec) for spec in mailroom_profile.classes}
    assert hash(mailroom_profile.require_class("contract").subclasses) == hash(
        again.require_class("contract").subclasses
    )


def test_content_hash_is_the_sha256_of_the_canonical_form(mailroom_profile):
    """The hash can be recomputed from the canonical form alone."""
    assert (
        hashlib.sha256(_canonical_json(profile_canonical_form(mailroom_profile))).hexdigest()
        == mailroom_profile.content_hash
    )
    assert len(mailroom_profile.content_hash) == 64


def test_path_and_mapping_sources_give_the_same_hash(mailroom_profile):
    """Loading from the file or from its parsed mapping (anchors expanded) is the same profile."""
    assert load_scoring_profile(mailroom_profile_mapping()).content_hash == mailroom_profile.content_hash
    assert load_scoring_profile(str(MAILROOM_PROFILE_PATH)).content_hash == mailroom_profile.content_hash


def _shuffle_mappings(value: Any, rng: random.Random, ordered_keys: frozenset[str]) -> Any:
    """Return ``value`` with every mapping's key order shuffled, except ordered alias tables."""
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {
            key: (item if key in ordered_keys else _shuffle_mappings(item, rng, ordered_keys))
            for key, item in items
        }
    if isinstance(value, list):
        return [_shuffle_mappings(item, rng, ordered_keys) for item in value]
    return value


@given(st.integers(0, 2**32 - 1))
def test_content_hash_ignores_mapping_key_order(seed):
    """Reordering any mapping's keys (fields, sections, thresholds) keeps the hash."""
    mapping = mailroom_profile_mapping()
    shuffled = _shuffle_mappings(mapping, random.Random(seed), frozenset({"aliases", "prefix_aliases"}))
    assert load_scoring_profile(shuffled).content_hash == load_scoring_profile(mapping).content_hash


@pytest.mark.parametrize(
    "edit",
    [
        lambda m: m["thresholds"].update(list_match_threshold=0.61),
        lambda m: m["thresholds"].update(type_bands={"date": "never"}),
        lambda m: m.update(version="2026-11-19"),
        lambda m: m["classes"].insert(0, m["classes"].pop(1)),
        lambda m: m["classes"][0]["fields"].update(document_name="free_text"),
        lambda m: m["classes"][0]["fields"].update(governing_law={"type": "name", "compare": "default"}),
        lambda m: m["list_matching"]["role_words"].append("tenant"),
        lambda m: m["classes"][0]["subclasses"]["catalog"].reverse(),
        lambda m: m["embedding"].update(enabled=True),
    ],
)
def test_content_hash_changes_with_content(mailroom_profile, edit):
    """Any change of a rule, a type, a value or an ordered list changes the hash."""
    assert load_scoring_profile(_mutate(edit)).content_hash != mailroom_profile.content_hash


def test_stating_a_default_explicitly_keeps_the_hash():
    """Writing a default value out does not change the profile's identity."""
    plain = load_scoring_profile(minimal_profile_mapping())
    explicit = load_scoring_profile(
        minimal_profile_mapping(thresholds={"list_match_threshold": 0.6}, unknown_label="other")
    )
    assert plain.content_hash == explicit.content_hash


def test_frozen_mapping_behaves_like_a_read_only_dict():
    """Insertion order kept, equality and hash order-blind, no mutation."""
    first, second = FrozenMapping({"a": 1, "b": 2}), FrozenMapping({"b": 2, "a": 1})
    assert list(first) == ["a", "b"] and first == second and hash(first) == hash(second)
    assert first == {"a": 1, "b": 2} and len(first) == 2 and first["a"] == 1
    with pytest.raises(AttributeError):
        del first._items  # type: ignore[attr-defined]
    assert "FrozenMapping" in repr(first)


def test_models_can_be_built_in_code_and_freeze_their_inputs():
    """Dataclasses built directly turn lists into tuples and dicts into frozen mappings."""
    vocabulary = LabelVocabulary(tokens=["a", "b"], aliases={"x": "a"})
    spec = ClassSpec(key="k", status="live", fields=[FieldSpec("f", "label", vocabulary=vocabulary)])
    assert spec.fields[0].vocabulary.tokens == ("a", "b")
    assert isinstance(vocabulary.aliases, FrozenMapping)
    assert spec.sorter_label == "k" and spec.field_types() == {"f": "label"}
    assert isinstance(load_scoring_profile(minimal_profile_mapping()), ScoringProfile)


def test_profile_file_is_valid_for_a_fresh_reader():
    """The committed YAML loads in a fresh interpreter (no hidden state from this test run)."""
    import subprocess
    import sys

    code = (
        "from mailroom_dojo.profile import load_scoring_profile;"
        f"print(load_scoring_profile({str(MAILROOM_PROFILE_PATH)!r}).content_hash)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == load_scoring_profile(Path(MAILROOM_PROFILE_PATH)).content_hash


def test_every_class_field_type_equals_the_frozen_old_registry(mailroom_profile):
    """Each class's fields, types and order equal the old Dojo's registry, byte for byte.

    corporate_record is compared with the 40-field map the old specialist suite scored with. A wrong type in
    scoring.yaml (for example a claim checklist typed as a name list) changes scores silently, so every
    declared field is compared, not just counts.
    """
    frozen = json.loads((Path(__file__).parent / "fixtures" / "old_registry_field_types.json").read_text())
    expected = {**frozen["registry"], "corporate_record": frozen["corporate_record_specialist_suite"]}
    assert set(mailroom_profile.class_keys) >= set(expected)
    for class_key, old_types in expected.items():
        new_types = dict(mailroom_profile.field_types(class_key))
        assert new_types == old_types, class_key
        if (
            class_key != "corporate_record"
        ):  # the old union lists the schema fields first; only the content matters
            assert list(new_types) == list(old_types), f"{class_key}: field order differs"


def test_a_frozen_mapping_offers_no_writable_view_of_its_items():
    """The stored items are a read-only view: changing them would leave the cached hash stale."""
    from mailroom_dojo.profile.frozen import FrozenMapping

    frozen = FrozenMapping({"a": 1})
    hash(frozen)
    with pytest.raises(TypeError):
        frozen._items["b"] = 2  # type: ignore[index]
    with pytest.raises(AttributeError):
        frozen._items = {}  # type: ignore[misc]
    assert dict(frozen) == {"a": 1}


def test_a_frozen_mapping_cannot_be_initialized_twice():
    """Calling ``__init__`` again would replace the items and clear the cached hash of a dict key."""
    frozen = FrozenMapping({"a": 1})
    table = {frozen: "kept"}
    with pytest.raises(TypeError, match="already initialized"):
        frozen.__init__({"b": 2})  # type: ignore[misc]
    assert dict(frozen) == {"a": 1} and table[FrozenMapping({"a": 1})] == "kept"
    assert dict(FrozenMapping()) == {}


_SURROGATE = chr(0xD800)


def _with_surrogate(place: str) -> dict:
    """The minimal profile with a lone surrogate written at one place."""
    mapping = minimal_profile_mapping()
    spec = mapping["classes"][0]
    vocabulary = spec["fields"]["coverage_tier"]["vocabulary"]
    if place == "client":
        mapping["client"] = _SURROGATE
    elif place == "version":
        mapping["version"] = "1" + _SURROGATE
    elif place == "sorter_label":
        spec["sorter_label"] = "Auto " + _SURROGATE
    elif place == "synonym":
        spec["synonyms"] = ["auto" + _SURROGATE]
    elif place == "token":
        vocabulary["tokens"] = ["basic", "p" + _SURROGATE]
    elif place == "alias_key":
        vocabulary.update(fold="aliases", aliases={"x" + _SURROGATE: "basic"})
    elif place == "unknown_key":
        mapping["x" + _SURROGATE] = 1
    return mapping


@pytest.mark.parametrize(
    ("place", "path"),
    [
        ("client", "client"),
        ("version", "version"),
        ("sorter_label", "classes[0].sorter_label"),
        ("synonym", "classes[0].synonyms[0]"),
        ("token", "classes[0].fields.coverage_tier.vocabulary.tokens[1]"),
        ("alias_key", "classes[0].fields.coverage_tier.vocabulary.aliases.x\\ud800"),
        ("unknown_key", None),
    ],
)
def test_a_lone_surrogate_is_a_profile_error_at_its_key(place, path):
    """A parsed mapping can hold text no UTF-8 encoder accepts; the loader names where instead of raising
    UnicodeEncodeError from the content hash, and the error itself prints."""
    with pytest.raises(ProfileError) as caught:
        load_scoring_profile(_with_surrogate(place))
    if path is not None:
        assert caught.value.path == path and "lone surrogate" in caught.value.problem
    str(caught.value).encode("utf-8")  # the message itself is valid text
    assert isinstance(load_scoring_profile(minimal_profile_mapping(client="Société")), ScoringProfile)


@pytest.mark.parametrize(
    "synonym",
    ["x{999999999999}", "(" * 5000 + "x" + ")" * 5000],
    ids=["repetition_overflow", "nesting_too_deep"],
)
def test_a_synonym_the_regex_compiler_cannot_build_is_a_profile_error(synonym):
    """re.compile raises OverflowError or RecursionError (not re.error) for these; the loader names the
    synonym instead. A valid synonym is kept as written."""
    mapping = minimal_profile_mapping()
    mapping["classes"][0]["synonyms"] = ["auto policy", synonym]
    _expect_error(mapping, "classes[0].synonyms[1]", "is not a valid regular expression")
    mapping["classes"][0]["synonyms"] = [r"auto\s+policy"]
    assert load_scoring_profile(mapping).classes[0].synonyms == (r"auto\s+policy",)


def _two_live_classes(notice_label: str, claim_label: str | None = None, claim_status: str = "live") -> dict:
    """A profile with live class ``notice`` and a class ``claim`` (labels as given)."""
    mapping = minimal_profile_mapping()
    template = mapping["classes"][0]
    notice = {**copy.deepcopy(template), "key": "notice", "sorter_label": notice_label}
    claim = {**copy.deepcopy(template), "key": "claim", "status": claim_status}
    if claim_label is not None:
        claim["sorter_label"] = claim_label
    mapping["classes"] = [notice, claim]
    return mapping


@pytest.mark.parametrize(
    ("notice_label", "claim_label"),
    [("CLAIM", None), ("Claim Form", "claim form"), ("claim", None)],
    ids=["label_is_another_key_in_capitals", "labels_differ_only_in_case", "label_is_another_key"],
)
def test_sorter_labels_are_unique_in_the_classifiers_case_blind_namespace(notice_label, claim_label):
    """A sorter label the classifier would read as another live class (ignoring case) is refused at load,
    not later by ClassLabelSet.from_profile."""
    mapping = _two_live_classes(notice_label, claim_label)
    with pytest.raises(ProfileError) as caught:
        load_scoring_profile(mapping)
    assert caught.value.path in ("classes[0].sorter_label", "classes[1].sorter_label"), str(caught.value)
    assert "already" in caught.value.problem
    retired = _two_live_classes(notice_label, claim_label, claim_status="retired")
    if notice_label != "claim":  # the exact duplicate of a key or label is refused whatever the status
        assert load_scoring_profile(retired).live_class_keys == ("notice",)


def test_a_replaced_profile_gets_the_hash_of_its_own_rules(mailroom_profile):
    """``dataclasses.replace`` cannot carry the old hash to new rules: the hash is derived, never passed."""
    changed = dataclasses.replace(
        mailroom_profile,
        thresholds=dataclasses.replace(mailroom_profile.thresholds, list_match_threshold=0.9),
    )
    assert changed.thresholds.list_match_threshold == 0.9
    assert changed.content_hash != mailroom_profile.content_hash and changed != mailroom_profile
    assert (
        changed.content_hash
        == load_scoring_profile(
            _mutate(lambda m: m["thresholds"].update(list_match_threshold=0.9))
        ).content_hash
    )
    assert dataclasses.replace(mailroom_profile) == mailroom_profile
    with pytest.raises(TypeError):
        dataclasses.replace(mailroom_profile, content_hash="0" * 64)
