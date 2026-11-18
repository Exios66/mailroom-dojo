"""Client profile loading: the valid synthetic profile loads; every invalid shape is refused
with every problem listed; a new class is a profile entry only (R4)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from labels_testkit import PROFILE_PATH, hub_row, make_snapshot, near_gold_row
from mailroom_labels.coverage import coverage
from mailroom_labels.errors import ProfileError
from mailroom_labels.layers import corpus_index, read_layers
from mailroom_labels.profile import FIELD_TYPES, load_profile, parse_profile
from mailroom_labels.quality import check
from mailroom_labels.resolve import resolve
from mailroom_labels.snapshot import load_snapshot_set


def raw() -> dict:
    return yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))


def test_synthetic_profile_loads() -> None:
    prof = load_profile(PROFILE_PATH)
    assert prof.client == "synthetic-client"
    assert prof.rank_index("review:gold") == 0
    assert prof.rank_index("interim:disputed") is None
    assert prof.field_spec("letter", "urgency").vocabulary == "urgency"
    assert prof.field_spec("letter", "nope") is None
    assert prof.field_spec("nope", "sender") is None


def test_dojo_field_type_vocabulary() -> None:
    assert set(FIELD_TYPES) == {
        "name",
        "label",
        "date",
        "money",
        "id",
        "free_text",
        "containment",
        "cms_doc_ref",
        "entity_list",
        "entity_list:name",
        "entity_list:free_text",
        "entity_list:id",
        "entity_list:cms_doc_ref",
    }


def _mutate(path: str, value: object) -> dict:
    data = copy.deepcopy(raw())
    keys = path.split(".")
    node = data
    for key in keys[:-1]:
        node = node[key]
    if value is KeyError:
        del node[keys[-1]]
    else:
        node[keys[-1]] = value
    return data


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        ("classes.letter.fields.sender.type", "person", "is not a Dojo field type"),
        ("classes.letter.fields.sender.type", "entity_list:date", "is not a Dojo field type"),
        ("classes.letter.fields.urgency.vocabulary", "missing_vocab", "is not defined under vocabularies"),
        ("classes.letter.fields.sender.vocabulary", "urgency", "only a label is closed"),
        ("classes.letter.fields.sender.colour", "blue", "unknown key 'colour'"),
        ("precedence", ["review:gold", "hub", "interim:disputed"], "never truth"),
        ("precedence", ["review:gold", "hub", "hub"], "lists 'hub' twice"),
        ("precedence", ["hub", "made_up"], "unknown rank 'made_up'"),
        ("precedence", [], "precedence is empty"),
        ("scorable_statuses", ["hub", "silver"], "unknown status 'silver'"),
        ("vocabularies.urgency", [], "vocabularies.urgency is empty"),
        ("vocabularies.urgency", ["a", "a"], "lists 'a' twice"),
        ("classes", {}, "classes must be a non-empty mapping"),
        ("classes.letter.fields", {}, "fields must be a non-empty mapping"),
        ("classes.letter.subclasses", ["email", "email"], "lists 'email' twice"),
        ("client", " padded ", "client must be a non-empty name"),
        ("dataset", KeyError, "missing key 'dataset'"),
        ("extra_top", 1, "unknown key 'extra_top'"),
        ("classes.letter.fields.urgency.vocabulary", ["urgency"], "vocabulary must be a name"),
        ("classes.letter.fields.urgency.vocabulary", {"a": 1}, "vocabulary must be a name"),
    ],
)
def test_invalid_profile_is_refused_with_the_reason(path: str, value: object, expected: str) -> None:
    with pytest.raises(ProfileError) as info:
        parse_profile(_mutate(path, value))
    assert any(expected in p for p in info.value.problems), info.value.problems


def test_every_problem_is_listed_at_once() -> None:
    data = _mutate("classes.letter.fields.sender.type", "person")
    data["precedence"] = ["hub", "made_up"]
    data["scorable_statuses"] = ["silver"]
    with pytest.raises(ProfileError) as info:
        parse_profile(data)
    assert len(info.value.problems) == 3


def test_duplicate_yaml_key_is_refused(tmp_path: Path) -> None:
    text = PROFILE_PATH.read_text(encoding="utf-8").replace(
        "      recipient: {type: name, tier: tier1}\n",
        "      recipient: {type: name, tier: tier1}\n      sender: {type: id}\n",
    )
    path = tmp_path / "profile.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ProfileError, match="duplicate key 'sender'"):
        load_profile(path)


def test_unreadable_and_non_yaml_profiles(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="cannot read"):
        load_profile(tmp_path / "absent.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("client: [unclosed\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="not valid YAML"):
        load_profile(bad)
    bad.write_text("- a list\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="must be a mapping"):
        load_profile(bad)


def test_a_new_document_class_is_a_profile_entry_only(tmp_path: Path) -> None:
    """R4 end to end: add a class nobody coded for; its labels load, check, resolve and
    show up in coverage with no code change."""
    data = raw()
    data["vocabularies"]["invoice_state"] = ["open", "paid"]
    data["classes"]["invoice"] = {
        "specialist": "invoice_specialist",
        "subclasses": ["utility"],
        "fields": {"total": {"type": "money"}, "state": {"type": "label", "vocabulary": "invoice_state"}},
    }
    prof = parse_profile(data)
    dirs = [
        make_snapshot(tmp_path, "hub", [hub_row("INV1", "invoice", "utility", {"total": "$5.00"})]),
        make_snapshot(
            tmp_path, "near_gold", [near_gold_row("INV1", {"state": "paid"}, "invoice", "utility")]
        ),
    ]
    snaps = load_snapshot_set(dirs)
    assert check(snaps, prof).findings == ()
    read = read_layers(snaps)
    (doc,) = resolve(read.records, prof, corpus_index(read.corpus))
    assert doc.scorable_fields == frozenset({"total", "state"})
    report = coverage([doc], prof)
    assert report["classes"]["invoice"]["fields"]["state"]["by_status"]["near_gold"] == 1
