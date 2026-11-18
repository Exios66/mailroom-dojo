"""The shipped dictionaries against the sources they mirror (llm-mailroom and the Dojo).

``fixtures/source_values.json`` pins the source values and the commits they were read from;
it is written only by ``scripts/check_against_sources.py --write-fixture``. A renamed or
removed value fails here until the fixture is regenerated on purpose.

The cross-check with the Dojo's label scorer runs only where ``llm_dojo_scoring`` is
importable (it is not a dependency of this workspace).
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from mailroom_dictionaries import shipped_dictionaries

FIXTURE = Path(__file__).parent / "fixtures" / "source_values.json"

#: (dictionary, key, label) whose label this package resolves and the Dojo's scorer does not.
#: Labels are display names and a scored answer is the key; listed so a new one is noticed.
#: Not covered either, on purpose: non-ASCII spellings (full-width letters, typographic
#: dashes), which this package folds by NFKC and the Dojo's ASCII-only fold drops.
DELIBERATE = frozenset(
    {
        ("merger_consideration", "other", "Other / Unspecified"),
        ("claim_type", "workers_comp", "Workers' compensation"),
    }
)


def _fixture() -> dict[str, Any]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_fixture_names_the_source_commits() -> None:
    sources = _fixture()["sources"]
    assert set(sources) == {"llm-mailroom", "llm-dojo-scoring"}
    assert all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in sources.values())


def test_shipped_keys_match_the_source_values() -> None:
    shipped = shipped_dictionaries()
    rows = _fixture()["dictionaries"]
    assert len(rows) >= 15
    for name, row in rows.items():
        assert name in shipped, f"{name}: in {row['from']} but not shipped"
        keys = list(shipped[name].keys())
        expected = row["keys"]
        if not row["ordered"]:
            keys, expected = sorted(keys), sorted(expected)
        assert keys == expected, (
            f"{name} drifted from {row['from']}: shipped {keys}, source {expected}. If the change is "
            "deliberate, regenerate the fixture with scripts/check_against_sources.py --write-fixture"
        )


def _spellings(surface: str) -> set[str]:
    return {
        surface,
        surface + ".",
        surface.upper(),
        surface.title(),
        surface.replace("_", " "),
        surface.replace(" ", "/"),
        f'"{surface}"',
        f"  {surface}\t",
    }


def test_normalize_accepts_what_the_dojo_label_scorer_accepts() -> None:
    pytest.importorskip("llm_dojo_scoring")  # only its absence skips; a broken submodule fails
    scored_enums: Any = importlib.import_module("llm_dojo_scoring.schemas.scored_enums")
    shipped = shipped_dictionaries()
    classes = shipped["doc_classes"]
    problems: list[str] = []
    checked = 0
    for cls in classes:
        entry = classes.get(cls)
        assert entry is not None
        fields = entry.attributes["closed_label_fields"]
        scored = scored_enums.SCORED_ENUM_FIELDS.get(cls, {})
        problems += [f"{cls}.{f}: scored by the Dojo but not mapped" for f in scored if f not in fields]
        for field, name in fields.items():
            if field not in scored:
                continue  # record_type: closed, but scored as a name
            dictionary = shipped[name]
            for item in dictionary.entries:
                surfaces = [("key", item.key), ("label", item.label)] + [("alias", a) for a in item.aliases]
                for kind, surface in surfaces:
                    if kind == "label" and (name, item.key, surface) in DELIBERATE:
                        assert dictionary.normalize(surface) == item.key
                        continue
                    for value in sorted(_spellings(surface)):
                        checked += 1
                        dojo = scored_enums.canonical_label(cls, field, value) or None
                        mine = dictionary.normalize(value)
                        if dojo != mine:
                            problems.append(f"{name} {kind} {value!r}: Dojo {dojo!r}, dictionary {mine!r}")
    assert checked > 500
    assert not problems, "\n".join(problems)
