"""R4 end to end: a new document type is a scoring-profile entry, with no code change.

A YAML profile written here adds a synthetic insurance class (sorter label, synonyms,
typed fields, a label vocabulary with aliases, thresholds). It is loaded, noisy
sorter outputs are classified, and extractions are scored through
``score_extraction`` with exact expected numbers. The test proves no file of the
Dojo's source changed and that the class name appears nowhere in it; that a broken
profile fails at load naming its YAML path; that two new types work at once; and
which classification and extraction results for the existing mailroom classes are
unchanged when classes are added (and the one way they can change: a new class put
earlier in the match order wins first-match ties).
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from dojo_testkit import SOURCE_ROOT, decode, mailroom_profile_mapping
from golden_replay import CLASSIFICATION_FIXTURE, boundary_document, jsonl_corpus, plain
from mailroom_dojo.classification import ClassLabelSet, accuracy, normalize_label, per_class_stats
from mailroom_dojo.field_scoring import score_extraction
from mailroom_dojo.profile import ProfileError, ScoringProfile, load_scoring_profile

NEW_CLASS = "auto_policy_declaration"
SECOND_CLASS = "homeowners_claim_notice"

AUTO_POLICY_CLASS = {
    "key": NEW_CLASS,
    "status": "live",
    "sorter_label": "Auto Policy Declarations",
    "synonyms": [r"\bdec(?:laration)?s? page\b"],
    "fields": {
        "policy_number": "id",
        "effective_date": "date",
        "premium": "money",
        "insureds": "entity_list:name",
        "coverage_tier": {
            "type": "label",
            "vocabulary": {
                "tokens": ["basic", "standard", "premium"],
                "fold": "aliases",
                "aliases": {"full coverage": "premium", "minimum": "basic", "state minimum": "basic"},
            },
        },
    },
}

HOMEOWNERS_CLASS = {
    "key": SECOND_CLASS,
    "status": "live",
    "sorter_label": "Homeowners Claim Notice",
    "synonyms": [r"\bfnol\b", r"\bfirst notice of loss\b"],
    "fields": {
        "claim_reference": "id",
        "date_of_loss": "date",
        "estimated_damage": "money",
        "peril": {
            "type": "label",
            "vocabulary": {"tokens": ["fire", "water", "wind", "theft", "other"], "fold": "exact"},
        },
        "damaged_items": "entity_list:free_text",
    },
}


def synthetic_profile_mapping(*classes: dict[str, Any]) -> dict[str, Any]:
    """A new client's profile with the given classes, its own thresholds and strict unmapped-field policy."""
    return {
        "profile_format": 1,
        "client": "synthetic-insurer",
        "version": "1",
        "unmapped_fields": "skip",
        "thresholds": {
            "ambiguous_band": [0.4, 0.9],
            "type_bands": {"date": "never"},
            "list_match_threshold": 0.6,
        },
        "classification": {"match_order": [spec["key"] for spec in classes]},
        "classes": [copy.deepcopy(spec) for spec in classes],
    }


def write_profile(directory: Path, mapping: dict[str, Any], name: str = "scoring.yaml") -> Path:
    """Write a profile mapping as YAML."""
    path = directory / name
    path.write_text(yaml.safe_dump(mapping, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def source_tree_digest() -> dict[str, str]:
    """sha256 of every file under the Dojo's source package (caches and build metadata left out)."""
    return {
        str(path.relative_to(SOURCE_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(SOURCE_ROOT.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


EXPECTED_DECLARATION = {
    "policy_number": "PAP-0042-7781",
    "effective_date": "2025-03-01",
    "premium": "$1,284.50",
    "insureds": ["Maria Lopez", "Daniel Lopez"],
    "coverage_tier": "premium",
}

#: (prediction, field scores, overall score, ambiguous fields) under the synthetic profile.
DECLARATION_CASES = [
    (
        {
            "policy_number": "pap-0042-7781",
            "effective_date": "March 1, 2025",
            "premium": "1284.50",
            "insureds": ["Daniel Lopez", "Maria Lopez"],
            "coverage_tier": "Full Coverage",
            "agent_name": "Bob",
        },
        {"policy_number": 1.0, "effective_date": 1.0, "premium": 1.0, "insureds": 1.0, "coverage_tier": 1.0},
        1.0,
        [],
    ),
    (
        # Transposed digits, day and month swapped (same year: 0.33), 50 cents short,
        # one insured of two (F1 2/3), wrong tier.
        {
            "policy_number": "PAP-0042-7718",
            "effective_date": "2025-01-03",
            "premium": "$1,284.00",
            "insureds": ["Maria Lopez"],
            "coverage_tier": "basic",
        },
        {
            "policy_number": 0.0,
            "effective_date": 0.33,
            "premium": 0.0,
            "insureds": 0.6667,
            "coverage_tier": 0.0,
        },
        0.1993,
        ["insureds"],
    ),
    (
        # Spaces in the id, month and year only (0.67), currency word, an initial plus an
        # extra insured (P 2/3, R 1, F1 0.8), tier with stray case and space.
        {
            "policy_number": "PAP 0042 7781",
            "effective_date": "March 2025",
            "premium": "USD 1284.5",
            "insureds": ["M. Lopez", "Daniel Lopez", "Ana Ruiz"],
            "coverage_tier": "Premium ",
        },
        {"policy_number": 1.0, "effective_date": 0.67, "premium": 1.0, "insureds": 0.8, "coverage_tier": 1.0},
        0.894,
        ["insureds"],
    ),
    (
        {},
        {"policy_number": 0.0, "effective_date": 0.0, "premium": 0.0, "insureds": 0.0, "coverage_tier": 0.0},
        0.0,
        [],
    ),
]

#: (sorter output, expected class) for the synthetic class.
NOISY_DECLARATION_OUTPUTS = [
    ('{"doc_type": "Auto Policy Declaration"}', NEW_CLASS),
    ("AUTO_POLICY_DECLARATION", NEW_CLASS),
    ("auto policy declarations", NEW_CLASS),
    ('```json\n{"doc_type": "auto policy declarations"}\n```', NEW_CLASS),
    ("This looks like a Declarations Page for a personal auto policy.", NEW_CLASS),
    ("**dec page**", NEW_CLASS),
    ("ERROR: model timeout", NEW_CLASS),
    ("auto policy", NEW_CLASS),
    ("declaration", NEW_CLASS),
]


@pytest.fixture()
def synthetic_profile(tmp_path) -> ScoringProfile:
    """The one-class synthetic profile, loaded from YAML on disk."""
    return load_scoring_profile(write_profile(tmp_path, synthetic_profile_mapping(AUTO_POLICY_CLASS)))


def test_a_new_type_is_classified_and_scored_without_touching_the_source(tmp_path):
    """Load, classify and score a new class from YAML; the source tree is byte-identical and never names
    it."""
    before = source_tree_digest()
    profile = load_scoring_profile(write_profile(tmp_path, synthetic_profile_mapping(AUTO_POLICY_CLASS)))
    label_set = ClassLabelSet.from_profile(profile)
    assert label_set.class_keys == (NEW_CLASS,)
    outputs = [output for output, _ in NOISY_DECLARATION_OUTPUTS]
    expected = [label for _, label in NOISY_DECLARATION_OUTPUTS]
    assert [normalize_label(output, label_set) for output in outputs] == [NEW_CLASS] * 6 + [
        "error: model timeout",
        "auto policy",
        "declaration",
    ]
    assert accuracy(expected, outputs, label_set) == 0.75
    assert per_class_stats(expected, outputs, label_set) == {
        NEW_CLASS: {
            "n": 8,
            "correct": 6,
            "accuracy": 0.75,
            "precision": 1.0,
            "recall": 0.75,
            "f1": 0.8571,
            "f2": 0.7895,
        }
    }
    for predicted, field_scores, overall, ambiguous in DECLARATION_CASES:
        result = score_extraction(profile, NEW_CLASS, predicted, EXPECTED_DECLARATION)
        assert result.field_scores == field_scores
        assert result.overall_score == overall
        assert result.ambiguous_fields == ambiguous
    after = source_tree_digest()
    assert after == before
    assert len(before) >= 30
    for relative in before:
        text = (SOURCE_ROOT / relative).read_bytes().decode("utf-8", errors="replace")
        assert NEW_CLASS not in text and SECOND_CLASS not in text, relative


def test_the_profile_thresholds_drive_ambiguity(tmp_path):
    """The same scores with the default band and no type bands mark another set of fields for review."""
    mapping = synthetic_profile_mapping(AUTO_POLICY_CLASS)
    mapping.pop("thresholds")
    profile = load_scoring_profile(write_profile(tmp_path, mapping))
    predicted = DECLARATION_CASES[2][0]
    result = score_extraction(profile, NEW_CLASS, predicted, EXPECTED_DECLARATION)
    assert result.field_scores == DECLARATION_CASES[2][1]
    assert result.ambiguous_fields == ["effective_date", "insureds"]


def test_unmapped_keys_follow_the_profile_policy(tmp_path, synthetic_profile):
    """An expected key the class does not declare is skipped under ``unmapped_fields: skip``, typed by name
    under ``heuristic``."""
    expected = {**EXPECTED_DECLARATION, "agent_name": "Bob Stone"}
    predicted = {**DECLARATION_CASES[0][0], "agent_name": "Robert Stone"}
    assert (
        "agent_name" not in score_extraction(synthetic_profile, NEW_CLASS, predicted, expected).field_scores
    )
    mapping = synthetic_profile_mapping(AUTO_POLICY_CLASS)
    mapping["unmapped_fields"] = "heuristic"
    heuristic = load_scoring_profile(write_profile(tmp_path, mapping, "heuristic.yaml"))
    assert "agent_name" in score_extraction(heuristic, NEW_CLASS, predicted, expected).field_scores


@pytest.mark.parametrize(
    ("change", "path", "message"),
    [
        (lambda m: m["classes"].clear(), "classes", "must not be empty"),
        (
            lambda m: m["classes"].pop(0),
            "classification.match_order[0]",
            "'auto_policy_declaration' is not a class",
        ),
        (
            lambda m: m["classes"][0]["fields"].update(premium="moneyy"),
            "classes[0].fields.premium.type",
            "unregistered field type 'moneyy'",
        ),
        (
            lambda m: m["classes"][0]["fields"].update(insureds="entity_list:nmae"),
            "classes[0].fields.insureds.type",
            "unregistered element type 'nmae'",
        ),
        (
            lambda m: m["classes"][0]["fields"]["coverage_tier"].update(type="lable"),
            "classes[0].fields.coverage_tier.type",
            "unregistered field type 'lable'",
        ),
        (
            lambda m: m["classes"][0]["fields"].update(coverage_tier="label"),
            "classes[0].fields.coverage_tier",
            "a label field needs a vocabulary",
        ),
        (
            lambda m: m["classes"][0].update(synonyms=["(unclosed"]),
            "classes[0].synonyms[0]",
            "is not a valid regular expression",
        ),
    ],
)
def test_a_broken_new_type_fails_at_load_naming_the_path(tmp_path, change, path, message):
    """Removing the class or mistyping a field type is a ProfileError at load, with the YAML path."""
    mapping = synthetic_profile_mapping(AUTO_POLICY_CLASS, HOMEOWNERS_CLASS)
    mapping["classification"]["match_order"] = [NEW_CLASS]
    change(mapping)
    with pytest.raises(ProfileError) as refused:
        load_scoring_profile(write_profile(tmp_path, mapping))
    assert refused.value.path == path
    assert message in str(refused.value)


def test_two_new_types_at_once(tmp_path):
    """Two new classes in one file: each classifies its own outputs and scores its own fields."""
    profile = load_scoring_profile(
        write_profile(tmp_path, synthetic_profile_mapping(HOMEOWNERS_CLASS, AUTO_POLICY_CLASS))
    )
    label_set = ClassLabelSet.from_profile(profile)
    assert label_set.class_keys == (SECOND_CLASS, NEW_CLASS)
    outputs = {
        "FNOL - kitchen fire": SECOND_CLASS,
        '{"doc_type": "homeowners claim notice"}': SECOND_CLASS,
        "First Notice of Loss": SECOND_CLASS,
        "auto policy declarations": NEW_CLASS,
        "declarations page attached to an FNOL": SECOND_CLASS,
        "Homeowners claim notice with a dec page": SECOND_CLASS,
    }
    assert {output: normalize_label(output, label_set) for output in outputs} == outputs
    expected = {
        "claim_reference": "HO-77-1203",
        "date_of_loss": "2025-07-04",
        "estimated_damage": "$48,000",
        "peril": "fire",
        "damaged_items": ["kitchen cabinets", "range hood"],
    }
    predicted = {
        "claim_reference": "ho-77-1203",
        "date_of_loss": "July 4, 2025",
        "estimated_damage": "48000",
        "peril": "Fire",
        "damaged_items": ["kitchen cabinets"],
    }
    result = score_extraction(profile, SECOND_CLASS, predicted, expected)
    assert result.field_scores == {
        "claim_reference": 1.0,
        "date_of_loss": 1.0,
        "estimated_damage": 1.0,
        "peril": 1.0,
        "damaged_items": 0.6667,
    }
    assert result.overall_score == 0.9333
    declaration = score_extraction(profile, NEW_CLASS, DECLARATION_CASES[1][0], EXPECTED_DECLARATION)
    assert declaration.field_scores == DECLARATION_CASES[1][1]


# ---------------------------------------------------------------------------
# Adding classes to the mailroom profile
# ---------------------------------------------------------------------------


def mailroom_with_new_classes(*, first: bool) -> dict[str, Any]:
    """The mailroom profile with both synthetic classes appended, optionally put first in the match order."""
    mapping = mailroom_profile_mapping()
    mapping["classes"] += [copy.deepcopy(AUTO_POLICY_CLASS), copy.deepcopy(HOMEOWNERS_CLASS)]
    if first:
        mapping["classification"]["match_order"] = [NEW_CLASS, SECOND_CLASS] + mapping["classification"][
            "match_order"
        ]
    return mapping


def _corpus_outputs() -> list[Any]:
    """Every sorter output in the classification corpus's label-reading cases."""
    _, cases = jsonl_corpus(CLASSIFICATION_FIXTURE)
    return [
        decode(case["args"])["value"]
        for case in cases
        if case["function"] == "normalize_label" and "raises" not in case["outcome"]
    ]


def test_appending_classes_keeps_every_existing_classification(mailroom_profile):
    """With new classes after the existing ones, every corpus output read as an existing class reads the same.

    An output that matched no class may now match a new one; that is the point of adding it. The one other
    exception is an output equal to a new class's exact sorter label (see the next test); these fixture
    labels equal no existing class's output.
    """
    base = ClassLabelSet.from_profile(mailroom_profile)
    extended = ClassLabelSet.from_profile(load_scoring_profile(mailroom_with_new_classes(first=False)))
    assert extended.class_keys == base.class_keys + (NEW_CLASS, SECOND_CLASS)
    outputs = (
        _corpus_outputs()
        + [output for output, _ in NOISY_DECLARATION_OUTPUTS]
        + ["contract for an auto policy declarations page", "correspondence about an fnol"]
    )
    moved = []
    for output in outputs:
        before, after = normalize_label(output, base), normalize_label(output, extended)
        if before in base.class_keys:
            assert after == before, output
        elif after != before:
            assert after in (NEW_CLASS, SECOND_CLASS), output
            moved.append(output)
    assert len(outputs) > 800
    assert (
        "auto policy declarations" in moved and "contract for an auto policy declarations page" not in moved
    )


def test_an_appended_class_takes_an_output_equal_to_its_exact_sorter_label(mailroom_profile):
    """The one exception to the append guarantee: a whole output that equals a new class's sorter label
    (ignoring case) is read as that class before any pattern is tried, even when an existing class's pattern
    matched it before. Only that exact text moves; longer text naming both still goes to the earlier class."""
    text = "contract for an auto policy declarations page"
    mapping = mailroom_with_new_classes(first=False)
    mapping["classes"][-2]["sorter_label"] = "Contract for an Auto Policy Declarations Page"
    extended = ClassLabelSet.from_profile(load_scoring_profile(mapping))
    base = ClassLabelSet.from_profile(mailroom_profile)
    assert normalize_label(text, base) == "contract"
    assert normalize_label(text, extended) == NEW_CLASS
    assert normalize_label(f"{text} attached", extended) == "contract"


def test_putting_a_new_class_first_can_take_existing_matches(mailroom_profile):
    """First match wins: a new class placed before contract takes text that names both. Exact labels never
    move."""
    base = ClassLabelSet.from_profile(mailroom_profile)
    first = ClassLabelSet.from_profile(load_scoring_profile(mailroom_with_new_classes(first=True)))
    text = "contract for an auto policy declarations page"
    assert normalize_label(text, base) == "contract"
    assert normalize_label(text, first) == NEW_CLASS
    assert normalize_label("correspondence about an fnol", first) == SECOND_CLASS
    for key in base.class_keys:
        assert normalize_label(key, first) == key
        assert normalize_label(f'{{"doc_type": "{key}"}}', first) == key


@pytest.mark.parametrize("first", [False, True])
def test_adding_classes_moves_no_existing_extraction_score(first):
    """The 16 recorded golden extractions, and each specialist scored with the profile's own field types, are
    unchanged."""
    base = load_scoring_profile(mailroom_profile_mapping())
    extended = load_scoring_profile(mailroom_with_new_classes(first=first))
    assert extended.content_hash != base.content_hash
    calls = [call for call in boundary_document()["calls"] if call["function"] == "score_extraction"]
    assert len(calls) == 16  # an empty or truncated capture must not pass as extraction parity
    for call in calls:
        args = decode(json.loads(json.dumps(call["args"])))
        result = score_extraction(
            extended,
            args["doc_class"],
            args["predicted"],
            args["expected"],
            field_types=args["field_types"],
            doc_text=args["doc_text"],
            audit_predicted=args["audit_predicted"],
            apply_variants=args["cms_aware"],
        )
        assert json.dumps(plain(result), sort_keys=True) == json.dumps(decode(call["result"]), sort_keys=True)
        if args["doc_class"] in base.class_keys:
            own_types = [
                score_extraction(profile, args["doc_class"], args["predicted"], args["expected"]).to_dict()
                for profile in (base, extended)
            ]
            assert own_types[0] == own_types[1]
