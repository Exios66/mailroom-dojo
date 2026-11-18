"""Plain helpers shared by the Dojo tests (fixtures live in conftest.py).

Importable because the root ``pyproject.toml`` puts ``packages/dojo/tests`` on the
pytest ``pythonpath``.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
SOURCE_ROOT = PACKAGE_ROOT / "src" / "mailroom_dojo"
MAILROOM_PROFILE_PATH = REPOSITORY_ROOT / "config" / "clients" / "mailroom-dataset" / "scoring.yaml"
SCALAR_CORPUS_PATH = PACKAGE_ROOT / "tests" / "fixtures" / "differential" / "scalar_cases.jsonl"
CAPTURE_SCRIPT_PATH = PACKAGE_ROOT / "scripts" / "capture_old_dojo_scalar_cases.py"
OLD_DOJO_COMMIT = "23f8234c3c548bdc203033388bb316e52e588a2c"


@cache
def capture_script() -> ModuleType:
    """Load the capture script by path, so tests reuse its exact encoding rules."""
    spec = importlib.util.spec_from_file_location("capture_old_dojo_scalar_cases", CAPTURE_SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def encode(value: Any) -> Any:
    """Encode a value with the corpus's tagged JSON rules."""
    return capture_script().encode(value)


def decode(value: Any) -> Any:
    """Decode tagged JSON from the corpus."""
    return capture_script().decode(value)


def canonical(value: Any) -> str:
    """Canonical JSON text, so ``1`` and ``1.0`` and ``None`` and ``0.0`` stay different."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


@cache
def scalar_corpus() -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Return the corpus header and its cases (read once per process)."""
    lines = SCALAR_CORPUS_PATH.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])["header"]
    return header, tuple(json.loads(line) for line in lines[1:])


class FakeSimilarity:
    """A similarity backend that answers from a spec, and remembers what it was asked.

    Specs: ``{"kind": "constant", "value": v}``, ``{"kind": "none"}``, ``{"kind": "nan"}``,
    ``{"kind": "raises"}``, ``{"kind": "value", "value": anything}`` (returned as is).
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        """Hold the spec."""
        self.spec = spec
        self.calls: list[tuple[str, str]] = []

    def similarity(self, first: str, second: str) -> Any:
        """Answer according to the spec."""
        self.calls.append((first, second))
        kind = self.spec["kind"]
        if kind in ("constant", "value"):
            return self.spec["value"]
        if kind == "none":
            return None
        if kind == "nan":
            return float("nan")
        raise RuntimeError("fake similarity backend failure")


def mailroom_profile_mapping() -> dict[str, Any]:
    """Return a fresh, mutable parse of the mailroom scoring profile (for building variants of it)."""
    import yaml

    return copy.deepcopy(yaml.safe_load(MAILROOM_PROFILE_PATH.read_text(encoding="utf-8")))


def minimal_profile_mapping(**overrides: Any) -> dict[str, Any]:
    """Return a small valid profile mapping with one class; ``overrides`` replace top-level keys."""
    mapping: dict[str, Any] = {
        "profile_format": 1,
        "client": "synthetic",
        "version": "1",
        "classes": [
            {
                "key": "auto_policy_declaration",
                "status": "live",
                "fields": {
                    "policy_number": "id",
                    "effective_date": "date",
                    "premium": "money",
                    "insureds": "entity_list:name",
                    "coverage_tier": {
                        "type": "label",
                        "vocabulary": {"tokens": ["basic", "standard", "premium"], "fold": "exact"},
                    },
                },
            }
        ],
    }
    mapping.update(overrides)
    return mapping
