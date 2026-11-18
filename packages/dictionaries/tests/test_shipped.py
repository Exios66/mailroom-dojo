"""The shipped dictionaries: they load, resolve their own keys, keep their rules and match the lock."""

from __future__ import annotations

import threading

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mailroom_dictionaries import (
    DATA_DIR,
    VocabularyError,
    list_dictionaries,
    load_dictionary,
    shipped_dictionaries,
)
from mailroom_dictionaries.lock import LOCK_NAME, lock_problems, read_lock

EXPECTED = {
    "claim_type",
    "communication_type",
    "corporate_record_intent",
    "correspondence_intent",
    "coverage_determination",
    "cuad_family",
    "decision_slots",
    "doc_classes",
    "entity_element_types",
    "field_types",
    "insurance_claim_intent",
    "label_statuses",
    "merger_agreement_intent",
    "merger_consideration",
    "outcomes",
    "review_causes",
    "urgency",
}


def test_every_data_file_is_a_shipped_dictionary() -> None:
    files = {p.stem for p in DATA_DIR.glob("*.yaml")} - {"glossary"}
    assert set(list_dictionaries()) == files == EXPECTED
    assert list_dictionaries() == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_key_normalizes_to_itself(name: str) -> None:
    d = load_dictionary(name)
    assert d.keys(), name
    for key in d:
        assert d.normalize(key) == key
        assert d.require(key.upper()) == key
        for alias in d.get(key).aliases:  # type: ignore[union-attr]
            assert d.normalize(alias) == key


def test_load_dictionary_is_cached() -> None:
    assert load_dictionary("urgency") is load_dictionary("urgency")


def test_unknown_dictionary_suggests_a_name() -> None:
    with pytest.raises(VocabularyError, match="no shipped dictionary named 'urgncy'; did you mean 'urgency'"):
        load_dictionary("urgncy")


def test_concurrent_first_loads_agree() -> None:
    shipped_dictionaries.cache_clear()
    results: list[dict[str, dict[str, object]]] = []
    barrier = threading.Barrier(8)

    def load() -> None:
        barrier.wait()
        results.append(shipped_dictionaries().manifest())

    threads = [threading.Thread(target=load) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8 and all(r == results[0] for r in results)


def test_label_status_precedence() -> None:
    d = load_dictionary("label_statuses")
    assert d.ordered
    assert sorted(d.keys(), key=d.rank) == ["gold", "hub", "interim", "near_gold"]
    assert d.rank("gold") < d.rank("hub") < d.rank("interim") < d.rank("near_gold")
    assert d.require("Near-Gold") == "near_gold" and d.require("HUB") == "hub"
    assert "lower rank wins" in d.description
    for key in d:
        assert d.get(key).attributes["applies_when"].strip()  # type: ignore[union-attr]


def test_urgency_is_ordered_and_takes_time_sensitive() -> None:
    d = load_dictionary("urgency")
    assert d.keys() == ("routine", "time-sensitive", "urgent", "critical")
    assert [d.rank(k) for k in d] == [1, 2, 3, 4]
    assert d.require("Time Sensitive") == "time-sensitive"


def test_label_status_words_are_exact() -> None:
    assert set(load_dictionary("label_statuses").keys()) == {"hub", "interim", "near_gold", "gold"}


def test_field_type_words_are_exact() -> None:
    assert load_dictionary("field_types").keys() == (
        "name",
        "label",
        "date",
        "money",
        "id",
        "free_text",
        "containment",
        "entity_list",
        "cms_doc_ref",
    )
    assert load_dictionary("entity_element_types").keys() == ("name", "free_text", "id", "cms_doc_ref")


def test_doc_classes_reference_shipped_dictionaries() -> None:
    shipped = shipped_dictionaries()
    classes = shipped["doc_classes"]
    assert classes.keys() == (
        "contract",
        "merger_agreement",
        "corporate_record",
        "correspondence",
        "insurance_claim",
    )
    for key in classes:
        entry = classes.get(key)
        assert entry is not None
        for field, target in entry.attributes["closed_label_fields"].items():
            assert target in shipped, (key, field, target)
    # Two classes share the field name `intent` with different values: distinct dictionaries.
    intents = {classes.get(k).attributes["closed_label_fields"].get("intent") for k in classes}  # type: ignore[union-attr]
    assert intents == {
        None,
        "merger_agreement_intent",
        "corporate_record_intent",
        "correspondence_intent",
        "insurance_claim_intent",
    }


def test_decision_slots_are_the_eight_of_r10() -> None:
    assert load_dictionary("decision_slots").keys() == (
        "sorter_reviewer",
        "archive_gate",
        "judge_replacement",
        "judge_check",
        "boss_check",
        "arbiter_veto",
        "closed_field_resolver",
        "field_review",
    )


def test_outcomes() -> None:
    assert load_dictionary("outcomes").keys() == ("archived", "review", "failed")


def test_lock_matches_the_data_files() -> None:
    """Fails when a dictionary changed without a version bump, or the lock and files disagree."""
    problems = lock_problems(read_lock(DATA_DIR / LOCK_NAME), shipped_dictionaries().manifest())
    assert not problems, "\n".join(p.message for p in problems)


ALL_SURFACES = sorted(
    {s for d in shipped_dictionaries().values() for e in d.entries for s in (e.key, *e.aliases)}
)


@given(
    name=st.sampled_from(sorted(EXPECTED)),
    value=st.one_of(st.text(max_size=40), st.sampled_from(ALL_SURFACES)),
)
def test_normalize_is_idempotent_and_closed(name: str, value: str) -> None:
    d = load_dictionary(name)
    key = d.normalize(value)
    if key is not None:
        assert key in d
        assert d.normalize(key) == key
        assert d.require(value) == key
    else:
        with pytest.raises(VocabularyError):
            d.require(value)


@given(name=st.sampled_from(sorted(EXPECTED)), data=st.data())
def test_normalize_ignores_case_and_separators(name: str, data: st.DataObject) -> None:
    d = load_dictionary(name)
    key = data.draw(st.sampled_from(d.keys()))
    mangled = "".join(
        data.draw(st.sampled_from([c.upper(), c.lower()])) + data.draw(st.sampled_from(["", " ", "-", "_"]))
        for c in key
    )
    assert d.normalize(f"  {mangled}  ") == key
