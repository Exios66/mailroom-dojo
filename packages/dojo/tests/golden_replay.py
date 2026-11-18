"""Replaying the old-Dojo golden fixtures through the new package, and the intended-difference ledger.

Shared by ``test_golden_parity.py``, ``test_classification_parity.py`` and
``test_labels_interface.py``. Importable because the root ``pyproject.toml`` puts
``packages/dojo/tests`` on the pytest ``pythonpath``.

A replayed outcome is ``{"result": <tagged JSON>}`` or ``{"raises": type, "message": text}``,
the form the fixtures record. Two outcomes are the same when their results are
equal as canonical JSON (so ``1`` is not ``1.0`` and ``None`` is not ``0.0``), or
when they raise the same type (and, for the paired-length check, the same message).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from functools import cache
from typing import Any

from dojo_testkit import PACKAGE_ROOT, canonical, encode

GOLDEN_DIRECTORY = PACKAGE_ROOT / "tests" / "fixtures" / "golden"
BOUNDARY_FIXTURE = "old_dojo_boundary_calls.json"
CLASSIFICATION_FIXTURE = "classification_cases.jsonl"
HUB_ROW_FIXTURE = "hub_row_cases.jsonl"
FIXTURES = (BOUNDARY_FIXTURE, CLASSIFICATION_FIXTURE, HUB_ROW_FIXTURE)
INTENDED_DIFFERENCES_PATH = GOLDEN_DIRECTORY / "intended_differences.json"

OLD_VERSION = "0.30.1"
OLD_INPUTS_SHA256 = "59292c3564116f87e770bd2e5ed504808f2c4c09caba36a28bbd42387523d580"
OLD_SCORES_SHA256 = "42c7f3c667e526e9e7db39df66470a37ecfdae0eec313c60c98f60ec63ffc480"


def plain(value: Any) -> Any:
    """A result object as plain data (an object with ``to_dict`` becomes its dict)."""
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict) and not isinstance(value, (dict, list, tuple, set, frozenset, str, bytes)):
        return to_dict()
    return value


def outcome_of(call: Callable[[], Any]) -> dict[str, Any]:
    """Run ``call`` and encode its result or exception like the fixtures do."""
    try:
        result = call()
    except Exception as error:  # the exception is the outcome being compared
        return {"raises": type(error).__name__, "message": str(error)}
    return {"result": encode(plain(result))}


def same_outcome(recorded: dict[str, Any], replayed: dict[str, Any]) -> bool:
    """True when two outcomes agree (see the module docstring)."""
    if "result" in recorded:
        return "result" in replayed and canonical(recorded["result"]) == canonical(replayed["result"])
    if replayed.get("raises") != recorded["raises"]:
        return False
    if recorded.get("message", "").startswith("expected length"):
        return replayed.get("message") == recorded["message"]
    return True


@cache
def jsonl_corpus(name: str) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """A JSONL corpus's header and cases."""
    lines = (GOLDEN_DIRECTORY / name).read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0])["header"], tuple(json.loads(line) for line in lines[1:])


@cache
def boundary_document() -> dict[str, Any]:
    """The boundary-call fixture."""
    return json.loads((GOLDEN_DIRECTORY / BOUNDARY_FIXTURE).read_text(encoding="utf-8"))


@cache
def intended_differences() -> dict[tuple[str, str], dict[str, Any]]:
    """The ledger of intended differences, keyed by (fixture, case id)."""
    document = json.loads(INTENDED_DIFFERENCES_PATH.read_text(encoding="utf-8"))
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in document["differences"]:
        key = (entry["fixture"], str(entry["case"]))
        if key in entries:
            raise AssertionError(f"intended difference {key} is listed twice")
        entries[key] = entry
    return entries


def check_against_ledger(
    fixture: str,
    replayed: Iterable[tuple[str, str, dict[str, Any], dict[str, Any]]],
) -> list[str]:
    """Compare replayed cases with their recordings and the ledger; return every problem found.

    ``replayed`` yields ``(case id, function, recorded outcome, replayed outcome)``
    for every case of the fixture that was replayed. A difference must be listed with
    the new outcome it now gives; a listed case that no longer differs is stale.
    Ledger entries are checked only for the functions that were replayed.
    """
    ledger = {case: entry for (name, case), entry in intended_differences().items() if name == fixture}
    problems: list[str] = []
    functions: set[str] = set()
    for case_id, function, recorded, outcome in replayed:
        functions.add(function)
        entry = ledger.pop(case_id, None)
        differs = not same_outcome(recorded, outcome)
        if differs and entry is None:
            problems.append(
                f"unlisted difference in {fixture} case {case_id} ({function}): "
                f"old {canonical(recorded)[:300]} new {canonical(outcome)[:300]}"
            )
        elif not differs and entry is not None:
            problems.append(
                f"stale intended difference in {fixture} case {case_id} ({function}): it no longer differs"
            )
        elif differs and not same_outcome(entry["new_outcome"], outcome):
            problems.append(
                f"intended difference in {fixture} case {case_id} ({function}) changed again: "
                f"listed {canonical(entry['new_outcome'])[:300]} now {canonical(outcome)[:300]}"
            )
    for case_id, entry in ledger.items():
        if entry["function"] in functions:
            problems.append(f"intended difference in {fixture} case {case_id} names no replayed case")
    return problems
