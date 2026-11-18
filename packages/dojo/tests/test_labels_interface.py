"""The label interface: structural Protocols, expected values from labelled documents, and the Hub-row
adapter.

The labels package publishes documents (``DocumentLabels``) and field labels
(``FieldLabel``) that the Dojo reads without importing it. The fakes below carry
the labels package's attributes, extra ones included, to prove the Protocols are
structural and ask for nothing more than the Dojo reads.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pytest

from dojo_testkit import OLD_DOJO_COMMIT, decode, encode
from golden_replay import HUB_ROW_FIXTURE, check_against_ledger, jsonl_corpus
from mailroom_dojo.field_scoring import score_extraction
from mailroom_dojo.labels_interface import (
    LABEL_STATUSES,
    ExpectedDocument,
    FieldLabel,
    LabelSource,
    UnverifiedDocumentError,
    expected_values,
    hub_rows,
)


@dataclass(frozen=True)
class FakeFieldLabel:
    """The labels package's field label shape (with its extra attributes)."""

    value: Any
    status: str = "interim"
    source: str = "claude-interim"
    reviewer: str | None = None
    presence: str = "populated"
    layers_seen: tuple[str, ...] = ("hub", "interim")


@dataclass(frozen=True)
class FakeDocumentLabels:
    """The labels package's document shape."""

    document_id: str
    doc_class: str
    fields: dict[str, FakeFieldLabel]
    scorable_fields: frozenset[str]
    subclass: str | None = None
    content_sha256: str = "0" * 64
    verified: bool = True
    unverified_reasons: tuple[str, ...] = ()


@dataclass
class FakeLabelSet:
    """The labels package's snapshot shape."""

    snapshot_id: str
    manifest_hash: str
    items: list[FakeDocumentLabels] = field(default_factory=list)

    def documents(self):
        """Every document."""
        return iter(self.items)


def _claim(**changes: Any) -> FakeDocumentLabels:
    """An insurance-claim document with three labelled fields, two of them scorable."""
    values = dict(
        document_id="claim-001",
        doc_class="insurance_claim",
        fields={
            "claim_number": FakeFieldLabel("CLM-2024-0042", status="gold", reviewer="JJB"),
            "claimed_amount": FakeFieldLabel("$12,500.00", status="near_gold"),
            "adjuster": FakeFieldLabel("Dana Smith", status="hub"),
        },
        scorable_fields=frozenset({"claim_number", "claimed_amount"}),
    )
    values.update(changes)
    return FakeDocumentLabels(**values)


def test_the_labels_shapes_satisfy_the_protocols():
    """Objects with the labels package's attributes are FieldLabel, ExpectedDocument and LabelSource."""
    document = _claim()
    assert isinstance(document.fields["claim_number"], FieldLabel)
    assert isinstance(document, ExpectedDocument)
    assert isinstance(FakeLabelSet("snap-1", "abc", [document]), LabelSource)
    assert not isinstance(object(), ExpectedDocument)
    assert LABEL_STATUSES == ("hub", "interim", "near_gold", "gold")


def test_only_scorable_fields_become_expected_values(mailroom_profile):
    """Labelled fields outside scorable_fields are left out and named; statuses travel with the values."""
    values = expected_values(_claim(), mailroom_profile)
    assert dict(values.fields) == {"claim_number": "CLM-2024-0042", "claimed_amount": "$12,500.00"}
    assert dict(values.field_status) == {"claim_number": "gold", "claimed_amount": "near_gold"}
    assert values.unscored_fields == ("adjuster",)
    assert values.verified is True and values.class_key == "insurance_claim"


def test_expected_values_score_through_score_extraction(mailroom_profile):
    """The mapping feeds score_extraction directly; the unscorable adjuster is never scored."""
    values = expected_values(_claim(), mailroom_profile)
    predicted = {"claim_number": "clm-2024-0042", "claimed_amount": "12500", "adjuster": "Someone Else"}
    result = score_extraction(mailroom_profile, values.class_key, predicted, values.fields)
    assert result.field_scores == {"claim_number": 1.0, "claimed_amount": 1.0}
    assert "adjuster" not in result.field_scores


def test_an_unverified_document_is_refused_unless_flagged(mailroom_profile):
    """A hash mismatch is never scored silently: refused by default, flagged when asked."""
    document = _claim(verified=False, unverified_reasons=("content_sha256 mismatch",))
    with pytest.raises(UnverifiedDocumentError) as refused:
        expected_values(document, mailroom_profile)
    assert refused.value.document_id == "claim-001"
    assert refused.value.reasons == ("content_sha256 mismatch",)
    assert "content_sha256 mismatch" in str(refused.value)
    flagged = expected_values(document, mailroom_profile, unverified="flag")
    assert flagged.verified is False
    assert flagged.unverified_reasons == ("content_sha256 mismatch",)
    assert dict(flagged.fields) == dict(expected_values(_claim(), mailroom_profile).fields)
    with pytest.raises(ValueError, match="unverified must be"):
        expected_values(document, mailroom_profile, unverified="ignore")


def test_a_label_status_outside_the_vocabulary_is_refused(mailroom_profile):
    """A status that is not hub, interim, near_gold or gold names the document and field, scorable or not."""
    for name in ("claim_number", "adjuster"):
        fields = dict(_claim().fields)
        fields[name] = FakeFieldLabel("x", status="silver")
        with pytest.raises(ValueError, match=rf"document 'claim-001' field '{name}': label status 'silver'"):
            expected_values(_claim(fields=fields), mailroom_profile)


def test_an_unknown_class_is_refused(mailroom_profile):
    """A document of a class the profile does not know cannot be shaped."""
    with pytest.raises(KeyError, match="unknown document class 'auto_policy'"):
        expected_values(_claim(doc_class="auto_policy"), mailroom_profile)


@pytest.mark.parametrize(
    ("subclass", "existing", "expected_marker", "derived"),
    [
        ("Carrier", None, "carrier", ("cms_source_table",)),
        (" pde ", None, "pde", ("cms_source_table",)),
        ("auto", None, None, ()),
        (None, None, None, ()),
        ("inpatient", "outpatient", "outpatient", ()),
    ],
)
def test_a_subclass_derived_marker_is_filled(mailroom_profile, subclass, existing, expected_marker, derived):
    """The CMS marker comes from the subclass when the labels leave it empty, as the profile's variant
    says."""
    fields = dict(_claim().fields)
    scorable = set(_claim().scorable_fields)
    if existing is not None:
        fields["cms_source_table"] = FakeFieldLabel(existing)
        scorable.add("cms_source_table")
    values = expected_values(
        _claim(fields=fields, scorable_fields=frozenset(scorable), subclass=subclass), mailroom_profile
    )
    assert values.fields.get("cms_source_table") == expected_marker
    assert values.derived_fields == derived


def test_the_dojo_does_not_import_the_labels_package():
    """The Protocols are structural: no module of the Dojo imports mailroom_labels."""
    import ast

    from dojo_testkit import SOURCE_ROOT

    for path in SOURCE_ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            )
            assert not any(name.startswith("mailroom_labels") for name in names), path


# ---------------------------------------------------------------------------
# Hub-row reference adapter: differential parity with the old gt_metadata helpers
# ---------------------------------------------------------------------------


def hub_row_corpus() -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """The Hub-row corpus header and cases."""
    return jsonl_corpus(HUB_ROW_FIXTURE)


def _hub_adapters(profile):
    """Map each corpus function to the adapter's call."""
    return {
        "parse_hub_fields": lambda args: hub_rows.parse_hub_fields(args["raw"]),
        "hub_presence_statuses": lambda args: hub_rows.hub_presence_statuses(args["fields"]),
        "parse_field_values": lambda args: hub_rows.parse_field_values(args["record"]),
        "scoring_fields_from_hub_row": lambda args: hub_rows.scoring_fields_from_hub_row(
            args["fields"],
            profile,
            field_types=args["field_types"],
            extra_keys=args["extra_keys"],
            drop_unmapped=args["drop_unmapped"],
            class_key=args["doc_type"],
            subclass=args["subclass"],
        ),
        "presence_expectations_from_clause_labels": lambda args: (
            hub_rows.presence_expectations_from_clause_labels(args["labels"], field=args["field"])
        ),
        "presence_expectations_from_hub_row": lambda args: hub_rows.presence_expectations_from_hub_row(
            args["fields"], field=args["field"]
        ),
    }


def replay_hub_case(case, profile):
    """Run one case through the adapter and encode its outcome."""
    try:
        result = _hub_adapters(profile)[case["function"]](decode(case["args"]))
    except Exception as error:  # the exception is the outcome being compared
        return {"raises": type(error).__name__, "message": str(error)}
    return {"result": encode(result)}


def test_the_hub_row_corpus_header():
    """The corpus names the old Dojo and covers every adapter function from both sources."""
    header, cases = hub_row_corpus()
    assert header["old_commit"] == OLD_DOJO_COMMIT and header["old_version"] == "0.30.1"
    assert dict(Counter(case["function"] for case in cases)) == header["cases_by_function"]
    assert {case["function"] for case in cases} == set(_hub_adapters(None))
    assert {case["source"] for case in cases} == {"old_tests", "generated"}
    raised = Counter(case["outcome"]["raises"] for case in cases if "raises" in case["outcome"])
    assert raised["TypeError"] >= 1 and raised["ValueError"] >= 1, raised


def test_every_hub_row_case_gives_the_old_output(mailroom_profile):
    """Replay every case with the mailroom profile; results equal as canonical JSON, exceptions by type and
    message."""
    _, cases = hub_row_corpus()
    replayed = [
        (case["id"], case["function"], case["outcome"], replay_hub_case(case, mailroom_profile))
        for case in cases
    ]
    exceptions_differ = [
        case_id for case_id, _, recorded, outcome in replayed if "raises" in recorded and outcome != recorded
    ]
    problems = check_against_ledger(HUB_ROW_FIXTURE, replayed)
    assert not problems, f"{len(problems)} problems of {len(cases)} cases, first: {problems[:3]}"
    assert not exceptions_differ, exceptions_differ[:5]


def test_pending_annotation_drops_and_absence_blanks(mailroom_profile):
    """Pending fields are unlabelled (dropped); not-applicable and documented-absent fields are labelled
    empty."""
    row = {
        "adjuster": "Stale Name",
        "denial_reasons": '["late"]',
        "claim_number": "C-1",
        "token_estimate": 900,
        "gt_presence": json.dumps({"adjuster": "pending_annotation", "denial_reasons": "not_applicable"}),
    }
    shaped = hub_rows.scoring_fields_from_hub_row(row, mailroom_profile)
    assert shaped == {"denial_reasons": "", "claim_number": "C-1"}
    assert row["adjuster"] == "Stale Name"


def test_placeholder_only_categories_are_not_expected():
    """A category whose spans are all placeholders is left out; an empty one is recorded as not expected."""
    labels = {
        "Cap": [{"text": "[*]"}, {"text": "____"}],
        "Law": [{"text": "Delaware law governs."}],
        "Ex": [],
    }
    assert hub_rows.presence_expectations_from_clause_labels(labels, field="clauses") == {
        "Law": {"expected": True, "answer": "Delaware law governs.", "field": "clauses"},
        "Ex": {"expected": False, "answer": "", "field": "clauses"},
    }
    pending = {"cuad_clause_labels": labels, "gt_presence": {"cuad_clause_labels": "pending_annotation"}}
    assert hub_rows.presence_expectations_from_hub_row(pending, field="clauses") is None
    assert (
        hub_rows.presence_expectations_from_hub_row({"cuad_clause_labels": {"Ex": []}}, field="clauses")
        is None
    )
