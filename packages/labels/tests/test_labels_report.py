"""The report script prints our own tokens only: never a label value, an audit value or a
sandbox value, wherever in the report it would appear (CLAUDE.md, "Hard rules")."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from labels_testkit import PROFILE_PATH, hub_row, interim_row, make_snapshot, review_row
from mailroom_record.hashing import canonical_json

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "labels_report.py"
SENTINEL = "Sentinel Q. Person"


def _report_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("labels_report_tokens_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sentinel_set(root: Path) -> list[Path]:
    """A clean set where the sentinel sits in every value the report could print: a Hub
    value an audit disagrees with, an interim correction, a human gold decision, the
    case's layers, and a superseded decision in the import accounting."""
    hub = make_snapshot(
        root,
        "hub",
        [
            hub_row("D1", fields={"sender": f"{SENTINEL} (hub)", "urgency": "routine"}),
            hub_row("D2", fields={"sender": "Plain"}),
        ],
    )
    interim = make_snapshot(
        root, "interim", [interim_row("D2", {"recipient": ("correction", f"{SENTINEL} (interim)")})]
    )
    review = make_snapshot(
        root,
        "review",
        [
            review_row("D1", "sender", f"{SENTINEL} (audit)"),
            review_row("D1", "recipient", f"{SENTINEL} (human)", human=True),
            review_row("D2", "recipient", f"{SENTINEL} (audit 2)"),
            review_row("D2", "action_items", [f"{SENTINEL} (list)"]),
        ],
        source={
            "repository": "synthetic",
            "superseded": [
                {"doc_id": "D1", "field": "sender", "value": f"{SENTINEL} (superseded)", "reason": "r"}
            ],
        },
    )
    return [hub, interim, review]


def _crosscheck() -> dict[str, Any]:
    value = ["hub", "populated", f"{SENTINEL} (sandbox)"]
    return {
        "method": "synthetic",
        "documents_compared": 2,
        "sandbox_fields_applied": {"sender": 1},
        "comparison": {"agree": 1},
        "examples": {
            "differ": [{"doc": "D1", "class": "letter", "field": "sender", "sandbox": value, "ours": value}]
        },
        "review_layer_effect": {"none": 0},
        "audit_vs_sandbox_gold_catalog": {
            "audit_decisions": 1,
            "equal": 0,
            "different": [
                {
                    "doc": "D1",
                    "field": "sender",
                    "audit": f"{SENTINEL} (audit)",
                    "catalog": f"{SENTINEL} (catalog)",
                    "near_gold_overlay": [f"{SENTINEL} (overlay)"],
                }
            ],
            "not_in_catalog": [],
            "catalog_status_of_audited_labels": {"near_gold": 1},
        },
    }


def test_the_data_quality_report_prints_no_value_text(tmp_path: Path) -> None:
    module = _report_module()
    built = module.build(
        _sentinel_set(tmp_path), PROFILE_PATH, _crosscheck(), [("D1", "sender"), ("D2", "recipient"), ("D2", "action_items")], "2026-01-01"
    )
    assert built is not None, "the synthetic set must pass the quality gate"
    quality_md, cov, cov_md = built
    # every section that used to print a value is present, so the check below means something
    for heading in (
        "Audit decisions that disagree with a populated Hub value or an interim correction: 2 ",
        "Human desk decisions: 1;",
        "### Case D1 sender",
        "### Case D2 recipient",
        "### Case D2 action_items",
        "### review-test",
        "Examples, differ:",
        "Claude audit decisions against the sandbox gold catalog: 1 decisions",
    ):
        assert heading in quality_md
    for text in (quality_md, cov_md, repr(cov)):
        assert "Sentinel" not in text and "Person" not in text
    # a value is named by its hash and size, so two reports can still be compared
    human_value = f"{SENTINEL} (human)"
    digest = hashlib.sha256(canonical_json(human_value)).hexdigest()[:12]
    assert f"string of {len(human_value)} chars, sha256:{digest}" in quality_md
    sandbox = hashlib.sha256(canonical_json(f"{SENTINEL} (sandbox)")).hexdigest()[:12]
    assert f"hub, populated, string of {len(SENTINEL) + 10} chars, sha256:{sandbox}" in quality_md
