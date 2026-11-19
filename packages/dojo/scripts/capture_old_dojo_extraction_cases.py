"""Freeze the old Dojo's outputs for the extraction layers as a differential parity corpus.

Writes ``packages/dojo/tests/fixtures/differential/extraction_cases.jsonl``: a header
line (old commit and version, seed, counts, the sha256 of the old golden
``inputs.json`` and ``scores.json``), then one case per line ``{"id", "source",
"function", "args", "settings", "outcome"}`` (``golden`` cases also carry
``"golden": {"case", "scores_json_paths", "field_scores_sha256"}``). ``settings``
names old settings that differ from the defaults; the replay test builds the same
rules into a scoring profile. ``outcome`` is ``{"result": ...}`` or
``{"raises": "<ExceptionType>"}``. Values use the scalar corpus's tagged JSON
(``capture_old_dojo_scalar_cases.encode``).
Case lines keep the key order of their arguments, because the order of the
expected fields decides the order of ``ambiguous_fields``.

Inputs come from three places:

1. ``golden``: every ``score_extraction`` call the old golden run makes
   (``scripts/freeze_goldens.py`` collectors), each tied to the ``scores.json`` paths
   that hold its ``field_scores``; the capture stops if one is not found there or if
   the recomputed output differs from the one the golden run produced.
2. ``old_tests``: every call to a targeted old function made while the old Dojo's
   tests run (captured with ``sys.setprofile`` in a child process). Calls that pass
   an embedding object are skipped; a ``knobs`` object becomes ``settings``.
3. ``generated``: seeded generated inputs (lists, audits with document text,
   presence, trace shapes, CMS rows, every class of the mailroom profile, unknown
   and retired classes, hostile values, non-default settings, fake embedding
   backends).

Run it only with an interpreter that can import the old Dojo, never in CI::

    PYTHONDONTWRITEBYTECODE=1 <old-venv>/bin/python \\
        packages/dojo/scripts/capture_old_dojo_extraction_cases.py \\
        --old-dojo <llm-dojo-scoring checkout at 23f8234c> \\
        --output packages/dojo/tests/fixtures/differential/extraction_cases.jsonl

It reads the old checkout and writes nothing inside it.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EXPECTED_OLD_COMMIT = "23f8234c3c548bdc203033388bb316e52e588a2c"
DEFAULT_SEED = 20261119
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROFILE_PATH = REPOSITORY_ROOT / "config" / "clients" / "mailroom-dataset" / "scoring.yaml"


def _scalar_script() -> Any:
    """Load the scalar capture script, whose tagged-JSON encoding this corpus shares."""
    path = Path(__file__).with_name("capture_old_dojo_scalar_cases.py")
    spec = importlib.util.spec_from_file_location("capture_old_dojo_scalar_cases", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SCALAR = _scalar_script()
encode, decode, Unencodable = _SCALAR.encode, _SCALAR.decode, _SCALAR.Unencodable
encode_case_arguments = _SCALAR.encode_case_arguments

#: Old direct tests whose calls are captured.
OLD_TEST_FILES = (
    "tests/test_field_scoring.py",
    "tests/test_trace_knobs.py",
    "tests/test_scoring_fixes_127.py",
    "tests/test_v026_truth_fixes.py",
    "tests/test_scoring_fixes_133_132.py",
    "tests/test_paired_cardinality.py",
    "tests/test_parse_compat_enum_key.py",
    "tests/test_bug_sweep_fixes.py",
    "tests/test_doc_scorer_fixtures.py",
    "tests/test_doc_scorers_common.py",
    "tests/test_doc_scorers_types.py",
    "tests/test_intents.py",
    "tests/test_score_document.py",
    "tests/test_scored_enums.py",
    "tests/test_v018_scorecard_honesty.py",
    "tests/test_v022_repairs.py",
    "tests/test_v10_rows.py",
    "tests/test_final_quality_transfer.py",
)

#: (old module file, old function name) -> case function name.
TARGETS: dict[tuple[str, str], str] = {
    ("scoring/field_scoring.py", "score_extraction"): "score_extraction",
    ("scoring/field_scoring.py", "score_entity_list"): "score_entity_list",
    ("scoring/field_scoring.py", "score_field"): "score_field",
    ("scoring/field_scoring.py", "verify_list_items"): "verify_list_items",
    ("scoring/field_scoring.py", "audit_list_field"): "audit_list_field",
    ("scoring/field_scoring.py", "audit_scalar_field"): "audit_scalar_field",
    ("scoring/field_scoring.py", "_verification_tokens"): "verification_tokens",
    ("scoring/field_scoring.py", "_date_grounded_in_doc"): "date_grounded_in_document",
    ("scoring/field_scoring.py", "_element_similarity"): "element_similarity",
    ("scoring/field_scoring.py", "_split_clause_spans"): "split_clause_spans",
    ("scoring/field_scoring.py", "disaggregate_clause_spans"): "disaggregate_clause_spans",
    ("scoring/field_scoring.py", "score_category_presence"): "score_category_presence",
    ("scoring/field_scoring.py", "field_is_ambiguous"): "field_is_ambiguous",
    ("scoring/field_scoring.py", "expected_cms_source_table"): "expected_cms_source_table",
    ("scoring/field_scoring.py", "is_unstated_cms_marker"): "is_unstated_cms_marker",
    ("scoring/field_scoring.py", "is_parse_compat_key"): "is_parse_compat_key",
    ("scoring/field_support.py", "field_audit_support"): "field_audit_support",
    ("scoring/trace_knobs.py", "capture_trace_knobs"): "capture_trace",
    ("scoring/trace_knobs.py", "parse_confidence"): "parse_confidence",
    ("scoring/trace_knobs.py", "parse_reasoning"): "parse_reasoning",
    ("scoring/trace_knobs.py", "confidence_gate"): "confidence_gate",
    ("scoring/trace_knobs.py", "confidence_calibration_error"): "confidence_calibration_error",
}

#: Old settings a case may override, by section.
FIELD_SETTINGS = (
    "ambiguous_band",
    "bipartite_match_threshold",
    "type_bands",
    "partial_gt_fields",
    "containment_fields",
    "verification_enabled",
    "verification_token_coverage",
    "embedding_enabled",
    "embedding_rescue_below",
    "presence_embedding_threshold",
)
TRACE_SETTINGS = (
    "capture_confidence",
    "capture_reasoning",
    "confidence_min",
    "confidence_band",
    "compute_calibration_error",
    "missing_confidence",
    "reasoning_routes_presence",
)


def knobs_as_settings(knobs: Any) -> dict[str, Any]:
    """The trace settings a ``TraceKnobSettings`` object holds, as a settings spec."""
    spec = {name: getattr(knobs, name) for name in TRACE_SETTINGS}
    if spec["confidence_band"] is not None:
        spec["confidence_band"] = list(spec["confidence_band"])
    return spec


# ---------------------------------------------------------------------------
# Phase 1 (child process): the old tests' calls and the golden run's calls
# ---------------------------------------------------------------------------


def _arguments(frame: Any) -> dict[str, Any]:
    """The arguments of a called frame, by parameter name."""
    code = frame.f_code
    return {
        name: frame.f_locals.get(name)
        for name in code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
    }


def collect_test_inputs(old_dojo: Path, output: Path) -> None:
    """Run the old tests under a profile hook and write every targeted call's arguments."""
    package_root = str(old_dojo / "llm_dojo_scoring") + os.sep
    records: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        """Count a skipped call."""
        skipped[reason] = skipped.get(reason, 0) + 1

    def profile(frame, event, arg):  # noqa: ANN001 - sys.setprofile signature
        """Record the arguments of a targeted function at call time."""
        if event != "call":
            return
        filename = frame.f_code.co_filename
        if not filename.startswith(package_root):
            return
        name = TARGETS.get((filename[len(package_root) :], frame.f_code.co_name))
        if name is None:
            return
        arguments = _arguments(frame)
        if arguments.get("embedding") is not None:
            skip(f"{name}:embedding")
            return
        arguments.pop("embedding", None)
        settings = {}
        if arguments.get("knobs") is not None:
            settings = knobs_as_settings(arguments["knobs"])
        arguments.pop("knobs", None)
        try:
            records.append({"function": name, "args": encode(copy.deepcopy(arguments)), "settings": settings})
        except Unencodable as error:
            skip(f"{name}:{error}")
        except Exception as error:  # a value deepcopy cannot copy
            skip(f"{name}:{type(error).__name__}")

    import pytest

    sys.setprofile(profile)
    try:
        exit_code = pytest.main(
            ["-q", "-p", "no:cacheprovider", "--rootdir", str(old_dojo), "-x"]
            + [str(old_dojo / path) for path in OLD_TEST_FILES]
        )
    finally:
        sys.setprofile(None)
    output.write_text(json.dumps({"exit_code": int(exit_code), "records": records, "skipped": skipped}))


def collect_golden_calls(old_dojo: Path, output: Path) -> None:
    """Run the old golden collectors and write every score_extraction call with its in-run result."""
    spec = importlib.util.spec_from_file_location(
        "freeze_goldens", old_dojo / "scripts" / "freeze_goldens.py"
    )
    goldens = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(goldens)
    target = str(old_dojo / "llm_dojo_scoring" / "scoring" / "field_scoring.py")
    current: list[str] = []
    pending: dict[int, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []

    def profile(frame, event, arg):  # noqa: ANN001 - sys.setprofile signature
        """Record score_extraction's arguments at call and its serialized result at return."""
        if frame.f_code.co_filename != target or frame.f_code.co_name != "score_extraction":
            return
        if event == "call":
            pending[id(frame)] = {"case": current[0], "args": encode(copy.deepcopy(_arguments(frame)))}
        elif event == "return" and id(frame) in pending:
            record = pending.pop(id(frame))
            record["in_run_result"] = encode(arg.to_dict())
            records.append(record)

    inputs = goldens.load_inputs()
    goldens._ensure_default_settings()
    sys.setprofile(profile)
    try:
        for task in sorted(goldens.TASK_KINDS):
            current[:] = [f"task:{task}"]
            goldens._collect_task_case(task, inputs["task_cases"][task])
        for name in sorted(goldens.list_suites(live_only=True)):
            current[:] = [f"suite:{name}"]
            goldens._collect_suite_case(name, inputs["suite_cases"][name])
    finally:
        sys.setprofile(None)
    output.write_text(json.dumps({"records": records}))


# ---------------------------------------------------------------------------
# Phase 2: generated inputs
# ---------------------------------------------------------------------------

HOSTILE = [
    None,
    "",
    " ",
    "null",
    "N/A",
    "[]",
    "{}",
    [],
    {},
    0,
    0.0,
    False,
    True,
    float("nan"),
    float("inf"),
    [[1, 2], [3]],
    {"a": 1},
    "日本 Ünïcödé ✓",
    "\x00\t\n",
    "x" * 300,
    10**30,
    -1.5,
]
NAMES = [
    "Acme Corp",
    "ACME Corporation",
    "Acme",
    "Acme Corp.",
    "Beta LLC",
    "Beta Holdings",
    "Gamma Trust",
    "John Smith",
    "Smith, John",
    "J. Smith",
    "Jon Smyth",
    "Delta Partners LP",
    "the Seller",
    "Seller",
    "Buyer",
    "Company",
    "the Licensee and Licensor",
    "Omega Industries Inc",
    "Global Technologies, Ltd",
]
ROLE_WORDS = ["Seller", "Buyer", "the Company", "Licensee", "Parties", "Customer", "Vendor Party", "shipper"]
DATES = [
    "2014-08-18",
    "August 18, 2014",
    "Aug 18 2014",
    "08/18/2014",
    "18th day of August, 2014",
    "the 18t h day of August, 2014",
    "August 2014",
    "2014",
    "August 18",
    "18 August",
    "2015-01-01",
    "12/31/99",
    "04-01-06",
    "3/24/2006",
    "April 1, 2006",
    "N/A",
    "",
    "the date first written above",
    "____ day of ________, 19__",
    "ninety (90) days after the Effective Date",
    20140818,
    "Q3 2014",
    "Tuesday, December 12, 2014",
    "2014-08-19",
    "September 18, 2014",
]
MONEY = [
    "$1,000.00",
    "1000",
    "USD 1,000",
    "$1,000.01",
    "$999.99",
    "one thousand dollars",
    "$5 million",
    "$5,000,000",
    1000,
    1000.0,
    "",
    "$0.00",
    "€250",
    "approximately $1,000",
    "nan",
]
IDS = ["C-1001", "c1001", "C1001", "POL 22-33", "pol2233", "12345", 12345, "ABC/9", "abc9", "X-1", ""]
FREE_TEXT = [
    "The Seller shall deliver the goods within thirty days.",
    "Seller delivers goods in 30 days",
    "This Agreement is governed by the laws of the State of Delaware.",
    "Delaware law governs",
    "Water damage to the kitchen after a pipe burst.",
    "pipe burst caused kitchen water damage",
    "Either party may terminate on ninety days notice; the Buyer may terminate for cause.",
    "Confidential information must not be disclosed. Exceptions apply to public information.",
    "unrelated text about weather",
    "",
]
CMS_REFS = [
    "provider NPI 5040521237",
    "NPI 5040521237",
    "NDC 00703051002",
    "ndc 0703-0510-02",
    "HCPCS detail (1 codes)",
    "HCPCS detail (2 codes)",
    "facility provider id 3900MB",
    "facility provider 3900MB",
    "NPI 5040521238",
    "ICD9 diagnosis (3 codes)",
]
CMS_TABLES = ["pde", "inpatient", "outpatient", "carrier"]
CLASSES_EXTRA = ["audit", "widget_report", "", " Insurance_Claim ", "CONTRACT", "Contract "]
UNMAPPED_KEYS = [
    "effective_date_x",
    "total_value",
    "case_number",
    "random_key",
    "filing_ref",
    "candidate",
    "valid_until",
    "notes",
]
NEVER_SCORED = ["confidence", "reasoning", "gt_presence", "provenance", "expected_subclass", "evidence"]
RETIRED = ["key_obligations", "termination_clauses", "key_points"]
CONFIDENCES = [
    None,
    "",
    True,
    False,
    0,
    0.5,
    0.85,
    1,
    1.0,
    85,
    "85",
    "0.4",
    150,
    -0.1,
    100,
    100.5,
    float("nan"),
    "abc",
    [0.5],
    {"score": 0.7},
    {"value": "90"},
    {"other": 1},
    {"confidence": None},
]
REASONINGS = [
    None,
    "",
    "  ",
    "Because the header says so.",
    ["entry one", "", None, {"field": "parties"}],
    {"summary": "short", "entries": [{"field": "governing_law", "evidence": "Delaware law"}]},
    {"summary": "", "entries": []},
    {"field": "parties", "evidence": "Acme"},
    {"text": "free text"},
    {"entries": [{"span": "x", "section": " 2.1 "}, 5, "plain"]},
    ("tuple entry",),
    42,
    {"evidence": None, "ref": "3.2"},
    [],
]


def label_values(rng: random.Random, vocabulary: dict[str, Any] | None) -> list[Any]:
    """Surfaces for a label field: tokens, aliases, case and spacing variants, junk."""
    tokens = list((vocabulary or {}).get("tokens") or ["other"])
    aliases = list(((vocabulary or {}).get("aliases") or {}).keys())
    surfaces = (
        tokens + aliases[:20] + [token.upper().replace("_", " ") for token in tokens] + ["inform", "low", ""]
    )
    return surfaces


def value_for(rng: random.Random, field_type: str, vocabulary: dict[str, Any] | None) -> Any:
    """A plausible expected value for a field type."""
    if field_type.startswith("entity_list"):
        element = field_type.split(":", 1)[1] if ":" in field_type else "name"
        pool = {
            "name": NAMES,
            "free_text": FREE_TEXT,
            "date": DATES,
            "id": IDS,
            "money": MONEY,
            "cms_doc_ref": CMS_REFS,
            "label": ["approved", "denied"],
        }.get(element, NAMES)
        items = [rng.choice(pool) for _ in range(rng.randint(0, 4))]
        if rng.random() < 0.15:
            items += [rng.choice(ROLE_WORDS)]
        if rng.random() < 0.1:
            return json.dumps(items)
        return items
    pool = {
        "date": DATES,
        "money": MONEY,
        "id": IDS,
        "name": NAMES,
        "free_text": FREE_TEXT,
        "containment": FREE_TEXT,
    }.get(field_type)
    if field_type == "label":
        pool = label_values(rng, vocabulary)
    return rng.choice(pool or NAMES)


def perturb(rng: random.Random, value: Any, field_type: str, vocabulary: dict[str, Any] | None) -> Any:
    """A prediction for an expected value: the same, a variant, another value, empty, hostile or a JSON
    text."""
    roll = rng.random()
    if roll < 0.3:
        return copy.deepcopy(value)
    if roll < 0.55:
        return value_for(rng, field_type, vocabulary)
    if roll < 0.65:
        return rng.choice([None, "", [], "null"])
    if roll < 0.72:
        return rng.choice(HOSTILE)
    if roll < 0.8 and isinstance(value, list):
        shuffled = list(value)
        rng.shuffle(shuffled)
        return shuffled + ([rng.choice(NAMES)] if rng.random() < 0.5 else [])
    if roll < 0.88 and isinstance(value, str):
        return value.upper() if rng.random() < 0.5 else f"{value} (see Exhibit A)"
    if roll < 0.94:
        return json.dumps(value) if not isinstance(value, float) else value
    return copy.deepcopy(value)


def document_text(rng: random.Random, values: list[Any]) -> str:
    """A document text holding some of ``values`` (dates in other formats), filler and stray dates."""
    parts = ["This Agreement is made between the parties named below."]
    for value in values:
        if rng.random() < 0.6:
            parts.append(str(value if not isinstance(value, list) else "; ".join(map(str, value))))
    parts += rng.sample(
        [
            "dated April 1, 2006",
            "on 3/24/2006",
            "the 18th day of August, 2014",
            "signed 08-18-14",
            "Acme Corporation of Delaware",
            "in the amount of $1,000.00",
            "provider NPI 5040521237",
            "Seller delivers the goods",
        ],
        k=rng.randint(0, 4),
    )
    return " ".join(parts)


def random_settings(rng: random.Random, allow_embedding: bool = False) -> dict[str, Any]:
    """A non-default settings spec (valid for both the old settings and a profile)."""
    choices = [
        lambda: {"ambiguous_band": rng.choice([[0.3, 0.9], [0.0, 1.0], [0.6, 0.61]])},
        lambda: {"bipartite_match_threshold": rng.choice([0.0, 0.3, 0.8, 1.0])},
        lambda: {
            "type_bands": rng.choice(
                [
                    {"name": "always"},
                    {"date": "never"},
                    {"entity_list": [0.2, 0.95]},
                    {"entity_list:name": "never", "money": [0.0, 0.5]},
                ]
            )
        },
        lambda: {"partial_gt_fields": rng.choice([[], ["parties", "signatories", "denial_reasons"]])},
        lambda: {"containment_fields": rng.choice([[], ["governing_law", "insurer", "damages_description"]])},
        lambda: {"verification_enabled": False},
        lambda: {"verification_token_coverage": rng.choice([0.0, 0.4, 1.0])},
        lambda: {"reasoning_routes_presence": False},
        lambda: {"capture_confidence": False},
        lambda: {"capture_reasoning": False},
        lambda: {"capture_confidence": False, "capture_reasoning": False},
        lambda: {"confidence_min": rng.choice([0.0, 0.5, 0.9])},
        lambda: {"confidence_band": rng.choice([[0.4, 0.8], [0.0, 1.0]])},
        lambda: {"compute_calibration_error": False},
        lambda: {"missing_confidence": rng.choice(["assume_1", "assume_0"])},
        lambda: {"missing_confidence": "assume_0", "confidence_min": 0.5, "confidence_band": [0.0, 0.3]},
    ]
    settings: dict[str, Any] = {}
    for _ in range(rng.randint(1, 3)):
        settings.update(rng.choice(choices)())
    return settings


def profile_tables() -> dict[str, Any]:
    """The mailroom profile's classes, field types and vocabularies (read with PyYAML)."""
    import yaml

    data = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    classes = {}
    for spec in data["classes"]:
        fields = {}
        vocabularies = {}
        for name, raw in spec["fields"].items():
            fields[name] = raw if isinstance(raw, str) else raw["type"]
            if isinstance(raw, dict) and raw.get("vocabulary"):
                vocabularies[name] = raw["vocabulary"]
        classes[spec["key"]] = {"fields": fields, "vocabularies": vocabularies}
    return classes


SIMILARITY_SPECS = [
    {"kind": "constant", "value": 0.95},
    {"kind": "constant", "value": 0.75},
    {"kind": "constant", "value": 0.3},
    {"kind": "none"},
    {"kind": "raises"},
    {"kind": "nan"},
]


def generated_cases(rng: random.Random, classes: dict[str, Any]) -> list[dict[str, Any]]:
    """Seeded inputs for every case function."""
    cases: list[dict[str, Any]] = []

    def add(function: str, settings: dict[str, Any] | None = None, **arguments: Any) -> None:
        """Queue one case (skip values the corpus format cannot hold; a fake backend spec is kept)."""
        with contextlib.suppress(Unencodable):
            cases.append(
                {"function": function, "args": encode_case_arguments(arguments), "settings": settings or {}}
            )

    element_types = ["name", "free_text", "date", "id", "money", "label", "cms_doc_ref", "containment"]
    # Entity lists: pools, role words, duplicates, empties, JSON texts, hostile items, permutations.
    for _ in range(700):
        element = rng.choice(element_types)
        predicted = value_for(rng, f"entity_list:{element}", None)
        expected = value_for(rng, f"entity_list:{element}", None)
        if rng.random() < 0.2:
            expected = [rng.choice(ROLE_WORDS) for _ in range(rng.randint(1, 3))] + (
                [rng.choice(NAMES)] if rng.random() < 0.5 else []
            )
        if rng.random() < 0.15 and isinstance(predicted, list) and predicted:
            predicted = predicted + [predicted[0]]
        if rng.random() < 0.08:
            predicted = rng.choice(HOSTILE)
        if rng.random() < 0.05:
            expected = rng.choice(HOSTILE)
        settings = random_settings(rng) if rng.random() < 0.15 else {}
        add(
            "score_entity_list",
            settings,
            element_type=element,
            pred=predicted,
            exp=expected,
            partial_gt=rng.random() < 0.4,
        )
    words = ["alpha", "beta", "gamma", "delta"]
    for _ in range(100):
        predicted = [
            " ".join(rng.choice(words) for _ in range(rng.randint(1, 3))) for _ in range(rng.randint(2, 3))
        ]
        expected = [
            " ".join(rng.choice(words) for _ in range(rng.randint(1, 3))) for _ in range(rng.randint(2, 3))
        ]
        for order in range(3):
            shuffled = list(predicted)
            random.Random(order).shuffle(shuffled)
            add(
                "score_entity_list",
                element_type=rng.choice(["free_text", "name"]),
                pred=shuffled,
                exp=expected,
                partial_gt=False,
            )
    clause_items = [
        "Seller shall deliver the goods",
        "the Seller shall deliver the goods within thirty days",
        "Buyer pays the price",
        "governed by Delaware law",
    ]
    for _ in range(100):
        predicted = rng.sample(clause_items, k=rng.randint(1, 3))
        expected = [
            rng.choice(
                [
                    "deliver the goods within",
                    "Seller shall deliver",
                    "pays the price",
                    "Seller",
                    "the Seller",
                    "Delaware law",
                ]
            )
            for _ in range(rng.randint(1, 3))
        ]
        add(
            "score_entity_list",
            element_type=rng.choice(["free_text", "name"]),
            pred=predicted,
            exp=expected,
            partial_gt=True,
        )
    for spec in SIMILARITY_SPECS:
        for _ in range(10):
            element = rng.choice(["name", "free_text"])
            add(
                "score_entity_list",
                {"embedding_enabled": True},
                element_type=element,
                pred=value_for(rng, f"entity_list:{element}", None),
                exp=value_for(rng, f"entity_list:{element}", None),
                partial_gt=rng.random() < 0.3,
                embedding={"$similarity": spec},
            )

    # score_field and field_is_ambiguous.
    scalar_types = [
        "name",
        "free_text",
        "date",
        "id",
        "money",
        "label",
        "containment",
        "cms_doc_ref",
        "entity_list",
        "entity_list:name",
        "entity_list:free_text",
        "entity_list:cms_doc_ref",
    ]
    for _ in range(250):
        field_type = rng.choice(scalar_types)
        expected = value_for(rng, field_type, None)
        add(
            "score_field",
            random_settings(rng) if rng.random() < 0.1 else {},
            field_type=field_type,
            pred=perturb(rng, expected, field_type, None),
            exp=expected,
            partial_gt=rng.random() < 0.3,
        )
    for _ in range(200):
        field_type = rng.choice(scalar_types + ["widget", "entity_list:widget"])
        score = rng.choice([0.0, 0.2, 0.5, 0.6, 0.849, 0.85, 0.95, 1.0, 0.4999])
        add(
            "field_is_ambiguous",
            random_settings(rng) if rng.random() < 0.5 else {},
            field_type=field_type,
            score=score,
        )

    # Audits.
    for _ in range(250):
        element = rng.choice(element_types)
        predicted = value_for(rng, f"entity_list:{element}", None)
        expected = value_for(rng, f"entity_list:{element}", None)
        text = rng.choice([None, "", document_text(rng, [predicted, expected]), "No content here at all."])
        add(
            "audit_list_field",
            random_settings(rng) if rng.random() < 0.1 else {},
            element_type=element,
            pred_items=predicted,
            exp_items=expected,
            doc_text=text,
            token_coverage=rng.choice([None, None, 0.5, 1.0]),
        )
    for _ in range(450):
        field_type = rng.choice(["date", "date", "name", "free_text", "id", "money", "label", "containment"])
        expected = value_for(rng, field_type, {"tokens": ["approved", "denied"]})
        predicted = perturb(rng, expected, field_type, None)
        text = rng.choice(
            [
                None,
                "",
                document_text(rng, [predicted]),
                document_text(rng, [expected]),
                "Signed on the 18th day of August, 2014 by Acme.",
                "Dated 08/18/2014.",
                "Effective September 18, 2014.",
            ]
        )
        add(
            "audit_scalar_field",
            random_settings(rng) if rng.random() < 0.1 else {},
            field_type=field_type,
            pred=predicted,
            exp=rng.choice([expected, None, ""]),
            doc_text=text,
        )
    for _ in range(150):
        items = [rng.choice(NAMES + FREE_TEXT + HOSTILE[:6]) for _ in range(rng.randint(0, 4))]
        add(
            "verify_list_items",
            items=items,
            doc_text=document_text(rng, items[:2]),
            token_coverage=rng.choice([None, 0.0, 0.5, 0.7, 1.0]),
        )
    for _ in range(150):
        value = rng.choice(DATES + NAMES[:4] + MONEY[:4])
        add("verification_tokens", value=value, field_type=rng.choice(["date", "name", "money", "id"]))
    for _ in range(200):
        parts = rng.choice(
            [
                [2014, 8, 18],
                [2014, 8, None],
                [None, 8, 18],
                [2014, None, None],
                [2006, 4, 1],
                [2006, 3, 24],
                [2015, 1, 1],
                [None, None, None],
            ]
        )
        predicted_date = {"$date_parts": parts} if rng.random() < 0.7 else {"$date": "2014-08-18"}
        if rng.random() < 0.05:
            predicted_date = None
        cases.append(
            {
                "function": "date_grounded_in_document",
                "settings": {},
                "args": {
                    "pred_date": predicted_date,
                    "doc_text": rng.choice(
                        [
                            document_text(rng, []),
                            "",
                            "dated 08.18.2014",
                            "18 day of August 2014",
                            "Aug. 18, 2014 and Sept 1, 2015",
                            None,
                        ]
                    ),
                },
            }
        )
    for _ in range(200):
        element = rng.choice(element_types)
        expected = value_for(rng, element, None)
        add(
            "element_similarity",
            element_type=element,
            item=str(perturb(rng, expected, element, None)),
            exp=str(expected),
        )

    # Clause spans and presence.
    clauses = [
        "The Seller shall indemnify the Buyer. The Buyer shall pay within 30 days.",
        "Non-compete applies for two years; non-solicit for one year",
        "Governed by the laws of the U.S. and Delaware.\nVenue: Wilmington",
        "Price is $5.00 per unit. See Section 1.2 for adjustments.",
        "Short. Tiny. Ok.",
        "Either party may terminate on notice (see 3.1). Termination for convenience is allowed!",
        "",
        None,
        ["a list item. Another Sentence here please", "dup", "dup"],
    ]
    for clause in clauses:
        add("split_clause_spans", text=clause)
        for minimum in (1, 2, 4, 6):
            add(
                "disaggregate_clause_spans",
                items=clause if isinstance(clause, list) else [clause, clause],
                min_span_tokens=minimum,
            )
    answers = {
        "Non-Compete": "non-compete applies for two years",
        "Governing Law": "laws of Delaware",
        "Indemnification": "The Seller shall indemnify the Buyer",
        "Audit Rights": "right to audit the books",
        "Termination For Convenience": "Termination for convenience is allowed",
    }
    for _ in range(300):
        categories = rng.sample(sorted(answers), k=rng.randint(0, 4))
        expectations = {}
        for category in categories:
            expectation = {"expected": rng.random() < 0.75, "answer": answers[category]}
            if rng.random() < 0.7:
                expectation["field"] = rng.choice(["cuad_clauses", "key_terms"])
            if rng.random() < 0.05:
                expectation["answer"] = rng.choice(["", None, "   "])
            expectations[category] = expectation
        predicted: Any = {"cuad_clauses": rng.sample(clauses[:6], k=rng.randint(0, 3))}
        if rng.random() < 0.3:
            predicted["key_terms"] = rng.choice(
                [
                    ["right to audit the books and records"],
                    "Delaware law governs",
                    json.dumps(["termination for convenience is allowed"]),
                ]
            )
        roll = rng.random()
        if roll < 0.3:
            routed = rng.sample(sorted(answers), k=2)
            predicted["reasoning"] = {
                "entries": [
                    {
                        "field": rng.choice([routed[0], f" {routed[1]} "]),
                        "evidence": rng.choice([answers[routed[0]], ""]),
                        "section_ref": rng.choice([None, "4.2"]),
                    },
                    {"field": routed[1], "section_ref": answers[routed[1]]},
                ]
            }
        elif roll < 0.35:
            predicted["reasoning"] = rng.choice(["a string reasoning", ["entry"], None, {"entries": None}])
        if rng.random() < 0.03:
            predicted = rng.choice([None, {}])
        settings = {"reasoning_routes_presence": False} if rng.random() < 0.2 else {}
        add(
            "score_category_presence",
            settings,
            predicted=predicted,
            presence_expectations=expectations,
            field_types={},
        )
    for spec in SIMILARITY_SPECS:
        for _ in range(8):
            categories = rng.sample(sorted(answers), k=2)
            add(
                "score_category_presence",
                {"embedding_enabled": True},
                predicted={"cuad_clauses": [rng.choice(clauses[:6]), "unrelated words entirely"]},
                presence_expectations={
                    category: {"expected": True, "answer": answers[category], "field": "cuad_clauses"}
                    for category in categories
                },
                field_types={},
                embedding={"$similarity": spec},
            )

    # Trace capture.
    for _ in range(600):
        predicted = {}
        if rng.random() < 0.85:
            predicted["confidence"] = rng.choice(CONFIDENCES)
        if rng.random() < 0.85:
            predicted["reasoning"] = rng.choice(REASONINGS)
        if rng.random() < 0.05:
            predicted = rng.choice([None, {}, [("confidence", 0.9)]])
        add(
            "capture_trace",
            random_settings(rng) if rng.random() < 0.5 else {},
            predicted=predicted,
            correctness=rng.choice([None, 0.0, 0.5, 0.875, 1.0, 1.5, -0.2]),
        )
    for value in CONFIDENCES + ["1e-5", "  0.5 ", 0.12345, 99.99999]:
        add("parse_confidence", value=value)
    for value in REASONINGS + [{"entries": "not a list"}, {"summary": 7}, "\n"]:
        add("parse_reasoning", value=value)
    for _ in range(150):
        add(
            "confidence_gate",
            random_settings(rng),
            confidence=rng.choice([None, 0.0, 0.3, 0.5, 0.79, 0.8, 1.0]),
        )
    for _ in range(120):
        add(
            "confidence_calibration_error",
            confidence=rng.choice(CONFIDENCES[:12] + ["0.3"]),
            correctness=rng.choice([None, 0, 0.5, 1, 1.2, "0.5", "x"]),
        )

    # Audit support.
    for _ in range(100):
        audits = {}
        for index in range(rng.randint(0, 4)):
            audit = {
                "n_predicted": rng.choice([0, 1, 3, None]),
                "verified_precision": rng.choice([0.0, 0.3333, 1.0]),
                "hallucination_rate": rng.choice([0.0, 0.6667, 1.0]),
            }
            if rng.random() < 0.05:
                audit.pop("hallucination_rate")
            audits[f"field_{index}"] = audit
        add("field_audit_support", audits=audits)

    # Row-variant helpers.
    for _ in range(200):
        expected = {}
        if rng.random() < 0.85:
            expected["cms_source_table"] = rng.choice(
                CMS_TABLES + ["PDE", " Carrier ", '"pde"', "auto", "", None]
            )
        if rng.random() < 0.7:
            expected["claim_type"] = rng.choice(["health", "Health ", "", None, "auto", "null", "[]", 0])
        doc_class = rng.choice(["insurance_claim", "insurance_claim", "contract", "Insurance_Claim", None])
        add(
            "expected_cms_source_table",
            doc_class=doc_class,
            expected=rng.choice([expected, expected, None, []]),
        )
        predicted = {}
        if rng.random() < 0.6:
            predicted["cms_source_table"] = rng.choice(CMS_TABLES + ["", None, "[]", "other"])
        if rng.random() < 0.6:
            predicted["claim_type"] = rng.choice(CMS_TABLES + ["health", "PDE ", "", None])
        add(
            "is_unstated_cms_marker",
            key=rng.choice(["cms_source_table", "claim_type"]),
            cms_table=rng.choice(CMS_TABLES + [None]),
            predicted=rng.choice([predicted, predicted, None]),
        )
    for _ in range(80):
        add(
            "is_parse_compat_key",
            doc_class=rng.choice(["contract", " Contract", "CONTRACT ", "merger_agreement", None, ""]),
            key=rng.choice(["merger_consideration", "maud_clauses", "parties"]),
            field_types=rng.choice([None, {}, {"maud_clauses": "entity_list:free_text"}]),
        )

    # Whole extractions.
    class_keys = sorted(classes) + CLASSES_EXTRA
    for _ in range(420):
        class_key = rng.choice(class_keys)
        known = (
            classes.get(class_key)
            or classes.get(class_key.strip().lower())
            or {"fields": {}, "vocabularies": {}}
        )
        fields, vocabularies = known["fields"], known["vocabularies"]
        if not fields:
            fields = {"finding": "free_text", "severity": "label", "risk": "name", "summary": "free_text"}
        names = list(fields)
        chosen = rng.sample(names, k=min(len(names), rng.randint(1, 8)))
        expected = {name: value_for(rng, fields[name], vocabularies.get(name)) for name in chosen}
        for name in list(expected):
            if rng.random() < 0.08:
                expected[name] = rng.choice([None, "", "null", "[]", "N/A"])
        if rng.random() < 0.25:
            expected[rng.choice(UNMAPPED_KEYS)] = rng.choice(DATES + MONEY + NAMES + [["a", "b"]])
        if rng.random() < 0.15:
            expected[rng.choice(NEVER_SCORED)] = rng.choice(["x", 0.9, {"entries": []}])
        if rng.random() < 0.1:
            expected[rng.choice(RETIRED)] = ["obligation one"]
        if rng.random() < 0.1:
            expected[rng.choice(["merger_consideration", "maud_clauses"])] = rng.choice(
                ["all_cash", ["clause"]]
            )
        predicted: Any = {
            name: perturb(rng, value, fields.get(name, "name"), vocabularies.get(name))
            for name, value in expected.items()
            if rng.random() < 0.85
        }
        if rng.random() < 0.3:
            extra = rng.choice(names)
            predicted[extra] = value_for(rng, fields[extra], vocabularies.get(extra))
        if rng.random() < 0.3:
            predicted["confidence"] = rng.choice(CONFIDENCES)
        if rng.random() < 0.2:
            predicted["reasoning"] = rng.choice(REASONINGS)
        if rng.random() < 0.04:
            predicted = rng.choice([None, {}])
        if rng.random() < 0.03:
            expected = rng.choice([None, {}])
        roll = rng.random()
        if class_key in classes and roll < 0.45:
            field_types = None
        elif roll < 0.6:
            field_types = dict(fields)
        elif roll < 0.72:
            field_types = {name: fields[name] for name in rng.sample(names, k=rng.randint(0, len(names)))}
        elif roll < 0.8:
            field_types = {}
        elif roll < 0.9:
            field_types = {
                **fields,
                rng.choice(RETIRED): "entity_list:free_text",
                "merger_consideration": "label",
            }
        else:
            field_types = {name: fields[name] for name in names[:3]} | {
                "total_value": "money",
                "case_number": "id",
            }
        text = None
        if rng.random() < 0.4:
            text = document_text(rng, list((expected or {}).values()) + list((predicted or {}).values()))
        audit_predicted = None
        if rng.random() < 0.08 and isinstance(predicted, dict):
            audit_predicted = {**predicted, rng.choice(names): value_for(rng, "name", None)}
        settings = random_settings(rng) if rng.random() < 0.12 else {}
        add(
            "score_extraction",
            settings,
            doc_class=class_key,
            field_types=field_types,
            predicted=predicted,
            expected=expected,
            doc_text=text,
            audit_predicted=audit_predicted,
            cms_aware=rng.random() < 0.85,
        )

    # CMS rows of insurance claims.
    insurance = classes["insurance_claim"]
    for _ in range(170):
        expected = {"claim_number": rng.choice(IDS), "claimed_amount": rng.choice(MONEY)}
        if rng.random() < 0.9:
            expected["cms_source_table"] = rng.choice(CMS_TABLES + ["PDE", '"carrier"', "auto", ""])
        if rng.random() < 0.75:
            expected["claim_type"] = rng.choice(["health", "health", "", None, "auto", "Health"])
        if rng.random() < 0.5:
            expected["supporting_documents"] = rng.sample(CMS_REFS, k=rng.randint(0, 3))
        for absent in ("adjuster", "denial_reasons"):
            if rng.random() < 0.4:
                expected[absent] = rng.choice(["", None, [], "null", "Bob Adjuster", ["late filing"]])
        predicted: Any = {}
        if rng.random() < 0.6:
            predicted["cms_source_table"] = rng.choice(CMS_TABLES + ["", None, "Inpatient", "health"])
        if rng.random() < 0.7:
            predicted["claim_type"] = rng.choice(
                CMS_TABLES + ["health", "auto", "", None, "PDE", "Health insurance"]
            )
        if rng.random() < 0.5:
            predicted["adjuster"] = rng.choice(["", None, "Bob", [], "null"])
        if rng.random() < 0.5:
            predicted["denial_reasons"] = rng.choice([[], [""], [None, ""], ["late"], "[]", None])
        if rng.random() < 0.6:
            predicted["supporting_documents"] = rng.sample(CMS_REFS, k=rng.randint(0, 3))
        predicted["claim_number"] = perturb(rng, expected["claim_number"], "id", None)
        if rng.random() < 0.05:
            predicted = rng.choice([None, {}])
        roll = rng.random()
        doc_class = rng.choice(["insurance_claim"] * 6 + ["contract", "Insurance_Claim"])
        field_types = (
            None
            if roll < 0.5 and doc_class in classes
            else (
                dict(insurance["fields"])
                if roll < 0.8
                else rng.choice([{}, {"claim_number": "id", "adjuster": "name"}])
            )
        )
        add(
            "score_extraction",
            random_settings(rng) if rng.random() < 0.05 else {},
            doc_class=doc_class,
            field_types=field_types,
            predicted=predicted,
            expected=expected,
            doc_text=document_text(rng, list(predicted.values()) if isinstance(predicted, dict) else [])
            if rng.random() < 0.3
            else None,
            audit_predicted=None,
            cms_aware=rng.random() < 0.8,
        )

    # Extractions with an embedding backend.
    for spec in SIMILARITY_SPECS:
        for _ in range(12):
            class_key = rng.choice(["contract", "correspondence", "merger_agreement", "insurance_claim"])
            fields = classes[class_key]["fields"]
            chosen = [
                name
                for name in fields
                if fields[name] in ("name", "free_text", "entity_list:name", "entity_list:free_text")
            ][:6]
            expected = {name: value_for(rng, fields[name], None) for name in chosen}
            predicted = {name: value_for(rng, fields[name], None) for name in chosen}
            add(
                "score_extraction",
                {"embedding_enabled": True},
                doc_class=class_key,
                field_types=None,
                predicted=predicted,
                expected=expected,
                doc_text=document_text(rng, list(expected.values())) if rng.random() < 0.3 else None,
                audit_predicted=None,
                cms_aware=True,
                embedding={"$similarity": spec},
            )
    return cases


# ---------------------------------------------------------------------------
# Phase 3: old outputs
# ---------------------------------------------------------------------------


class FakeSimilarity:
    """A deterministic stand-in for an embedding backend (the same specs the replay test builds)."""

    def __init__(self, spec: dict[str, Any]) -> None:
        """Hold the spec: ``constant`` (with ``value``), ``none``, ``raises`` or ``nan``."""
        self.spec = spec

    def similarity(self, first: Any, second: Any) -> Any:
        """Answer according to the spec."""
        kind = self.spec["kind"]
        if kind == "constant":
            return self.spec["value"]
        if kind == "none":
            return None
        if kind == "nan":
            return float("nan")
        raise RuntimeError("fake similarity backend failure")


@contextlib.contextmanager
def old_settings(settings: dict[str, Any], similarity: dict[str, Any] | None):
    """Apply a settings spec (and a fake embedding backend) to the old process-wide settings, then restore
    them."""
    from llm_dojo_scoring.config import get_settings
    from llm_dojo_scoring.scoring import field_scoring

    current = get_settings()
    saved = (copy.deepcopy(current.field_scoring), copy.deepcopy(current.trace_knobs))
    saved_embedding = field_scoring._get_embedding
    try:
        for name, value in settings.items():
            if name in ("ambiguous_band", "confidence_band") and value is not None:
                value = tuple(value)
            if name == "type_bands":
                value = {
                    key: (band,) if isinstance(band, str) else tuple(band) for key, band in value.items()
                }
            if name in ("partial_gt_fields", "containment_fields"):
                value = set(value)
            section = current.field_scoring if name in FIELD_SETTINGS else current.trace_knobs
            if not hasattr(section, name):
                raise KeyError(name)
            setattr(section, name, value)
        if similarity is not None:
            backend = FakeSimilarity(similarity)
            field_scoring._get_embedding = lambda: backend
        yield
    finally:
        current.field_scoring, current.trace_knobs = saved
        field_scoring._get_embedding = saved_embedding


def old_call(function: str, arguments: dict[str, Any], classes: dict[str, Any]) -> Any:
    """Call the old function a case names and return its result in corpus form."""
    from llm_dojo_scoring.scoring import field_scoring, field_support, trace_knobs

    arguments = dict(arguments)
    arguments.pop("embedding", None)
    if function == "score_extraction":
        field_types = arguments["field_types"]
        if field_types is None:
            field_types = dict(classes[arguments["doc_class"]]["fields"])
        result = field_scoring.score_extraction(
            arguments["doc_class"],
            field_types,
            arguments["predicted"],
            arguments["expected"],
            arguments.get("doc_text"),
            audit_predicted=arguments.get("audit_predicted"),
            cms_aware=arguments.get("cms_aware", True),
        )
        return {
            "to_dict": result.to_dict(),
            "audit_support": result.audit_support,
            "needs_judge_review": result.needs_judge_review,
            "overall_verified_precision": result.overall_verified_precision,
        }
    if function == "score_entity_list":
        result = field_scoring.score_entity_list(
            arguments["element_type"],
            arguments["pred"],
            arguments["exp"],
            partial_gt=arguments.get("partial_gt", False),
        )
        return {"to_dict": result.to_dict(), "score": result.score}
    if function == "score_field":
        result = field_scoring.score_field(
            arguments["field_type"],
            arguments["pred"],
            arguments["exp"],
            partial_gt=arguments.get("partial_gt", False),
        )
        if isinstance(result, field_scoring.EntityListScore):
            return {"list": result.to_dict(), "score": result.score}
        return result
    if function == "date_grounded_in_document" and isinstance(arguments["pred_date"], dict):
        arguments["pred_date"] = field_scoring._DateParts(*arguments["pred_date"]["$date_parts"])
    direct = {
        "verify_list_items": field_scoring.verify_list_items,
        "audit_list_field": field_scoring.audit_list_field,
        "audit_scalar_field": field_scoring.audit_scalar_field,
        "verification_tokens": field_scoring._verification_tokens,
        "date_grounded_in_document": field_scoring._date_grounded_in_doc,
        "element_similarity": field_scoring._element_similarity,
        "split_clause_spans": field_scoring._split_clause_spans,
        "disaggregate_clause_spans": field_scoring.disaggregate_clause_spans,
        "score_category_presence": field_scoring.score_category_presence,
        "field_is_ambiguous": field_scoring.field_is_ambiguous,
        "expected_cms_source_table": field_scoring.expected_cms_source_table,
        "is_unstated_cms_marker": field_scoring.is_unstated_cms_marker,
        "is_parse_compat_key": field_scoring.is_parse_compat_key,
        "field_audit_support": field_support.field_audit_support,
        "capture_trace": trace_knobs.capture_trace_knobs,
        "parse_confidence": trace_knobs.parse_confidence,
        "parse_reasoning": trace_knobs.parse_reasoning,
        "confidence_gate": trace_knobs.confidence_gate,
        "confidence_calibration_error": trace_knobs.confidence_calibration_error,
    }
    return direct[function](**arguments)


def run_case(case: dict[str, Any], classes: dict[str, Any]) -> dict[str, Any]:
    """Run one case on the old code and return the encoded outcome."""
    arguments = decode(case["args"])
    embedding = arguments.get("embedding")
    similarity = embedding["$similarity"] if isinstance(embedding, dict) else None
    with old_settings(case.get("settings") or {}, similarity):
        try:
            result = old_call(case["function"], arguments, classes)
        except Exception as error:  # the exception type is the recorded outcome
            return {"raises": type(error).__name__}
    return {"result": encode(result)}


def canonical_sha256(value: Any) -> str:
    """The sha256 of a value's canonical JSON."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_paths(tree: Any, target: Any, path: str = "") -> list[str]:
    """Every JSON-pointer-like path in ``tree`` whose value equals ``target``."""
    if tree == target:
        return [path or "/"]
    found: list[str] = []
    if isinstance(tree, dict):
        for key, value in tree.items():
            found += json_paths(value, target, f"{path}/{key}")
    elif isinstance(tree, list):
        for index, value in enumerate(tree):
            found += json_paths(value, target, f"{path}/{index}")
    return found


#: Helper functions the old tests reach mostly from inside score_extraction (once per key);
#: their captured calls are sampled down to this many, the rest add size, not coverage.
HELPER_CALL_SAMPLE = 80
SAMPLED_HELPERS = (
    "is_unstated_cms_marker",
    "is_parse_compat_key",
    "expected_cms_source_table",
    "element_similarity",
    "verification_tokens",
    "field_is_ambiguous",
)


def sample_helper_calls(records: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    """Keep every captured call, except a seeded sample of the unique calls of each sampled helper."""
    kept: list[dict[str, Any]] = []
    helpers: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records:
        if record["function"] in SAMPLED_HELPERS:
            key = json.dumps([record["args"], record["settings"]], sort_keys=True, ensure_ascii=True)
            helpers.setdefault(record["function"], {}).setdefault(key, record)
        else:
            kept.append(record)
    rng = random.Random(seed)
    for function in sorted(helpers):
        unique = [helpers[function][key] for key in sorted(helpers[function])]
        kept += rng.sample(unique, k=min(HELPER_CALL_SAMPLE, len(unique)))
    return kept


def _child(old_dojo: Path, flag: str, scratch: str) -> dict[str, Any]:
    """Run this script in a child process for one collection phase and return what it wrote."""
    output = Path(scratch) / f"{flag.strip('-')}.json"
    environment = {key: value for key, value in os.environ.items() if key != "LLM_DOJO_SCORING_CONFIG"}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(
        [sys.executable, __file__, "--old-dojo", str(old_dojo), flag, str(output)],
        check=True,
        cwd=scratch,
        env=environment,
        stdout=subprocess.DEVNULL,
    )
    return json.loads(output.read_text())


def main() -> int:
    """Capture, generate, compute and write the corpus; print a digest."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--old-dojo", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--collect-test-inputs", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--collect-golden-calls", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    old_dojo = options.old_dojo.resolve()
    sys.path.insert(0, str(old_dojo))
    if options.collect_test_inputs:
        collect_test_inputs(old_dojo, options.collect_test_inputs)
        return 0
    if options.collect_golden_calls:
        collect_golden_calls(old_dojo, options.collect_golden_calls)
        return 0
    if options.output is None:
        parser.error("--output is required")
    output = options.output.resolve()
    if old_dojo in output.parents:
        parser.error("refusing to write inside the old Dojo checkout")
    commit = subprocess.run(
        ["git", "-C", str(old_dojo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if commit != EXPECTED_OLD_COMMIT:
        print(f"warning: old Dojo is at {commit}, expected {EXPECTED_OLD_COMMIT}", file=sys.stderr)

    with tempfile.TemporaryDirectory() as scratch:
        captured = _child(old_dojo, "--collect-test-inputs", scratch)
        golden_calls = _child(old_dojo, "--collect-golden-calls", scratch)["records"]
    if captured["exit_code"] != 0:
        print(f"old tests exited {captured['exit_code']}; refusing to freeze", file=sys.stderr)
        return 1

    os.environ.pop("LLM_DOJO_SCORING_CONFIG", None)
    import llm_dojo_scoring
    from llm_dojo_scoring.config import clear_settings_cache

    clear_settings_cache()
    golden_directory = old_dojo / "tests" / "fixtures" / "golden"
    scores_text = (golden_directory / "scores.json").read_bytes()
    inputs_text = (golden_directory / "inputs.json").read_bytes()
    scores = json.loads(scores_text)
    classes = profile_tables()

    golden_cases = []
    for record in golden_calls:
        field_scores = decode(record["in_run_result"])["field_scores"]
        paths = json_paths(scores, field_scores)
        if not paths:
            print(f"golden {record['case']}: field_scores not found in scores.json", file=sys.stderr)
            return 1
        golden_cases.append(
            {
                "function": "score_extraction",
                "args": record["args"],
                "settings": {},
                "golden": {
                    "case": record["case"],
                    "scores_json_paths": paths,
                    "field_scores_sha256": canonical_sha256(field_scores),
                },
                "in_run_result": record["in_run_result"],
            }
        )

    sources = [("golden", case) for case in golden_cases]
    sources += [("old_tests", record) for record in sample_helper_calls(captured["records"], options.seed)]
    sources += [("generated", case) for case in generated_cases(random.Random(options.seed), classes)]

    seen: set[str] = set()
    lines: list[str] = []
    counts: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for source, case in sources:
        if isinstance(case["args"].get("field_types"), dict):
            # The old golden run builds some maps from sets; their order varies between runs and
            # never changes an output (scoring looks types up by name, the audit sorts them).
            case["args"]["field_types"] = dict(sorted(case["args"]["field_types"].items()))
        identity = [case["function"], case["args"], case["settings"], case.get("golden", {}).get("case")]
        key = json.dumps(identity, sort_keys=True, ensure_ascii=True)
        if key in seen:
            continue
        seen.add(key)
        outcome = run_case(case, classes)
        if source == "golden":
            in_run = case["in_run_result"]
            if outcome.get("result", {}).get("to_dict") != in_run:
                print(
                    f"golden {case['golden']['case']}: recomputed output differs from the golden run",
                    file=sys.stderr,
                )
                return 1
        entry = {
            "id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
            "source": source,
            "function": case["function"],
            "args": case["args"],
            "settings": case["settings"],
            "outcome": outcome,
        }
        if source == "golden":
            entry["golden"] = case["golden"]
        lines.append(json.dumps(entry, ensure_ascii=True, separators=(",", ":")))
        counts[case["function"]] = counts.get(case["function"], 0) + 1
        by_source[source] = by_source.get(source, 0) + 1

    header = {
        "header": {
            "description": (
                "llm-dojo-scoring outputs for the extraction scoring layers (differential parity corpus)"
            ),
            "old_commit": commit,
            "old_version": llm_dojo_scoring.__version__,
            "seed": options.seed,
            "golden_inputs_json_sha256": hashlib.sha256(inputs_text).hexdigest(),
            "golden_scores_json_sha256": hashlib.sha256(scores_text).hexdigest(),
            "golden_calls": len(golden_calls),
            "captured_test_files": list(OLD_TEST_FILES),
            "captured_calls": len(captured["records"]),
            "captured_skipped": captured["skipped"],
            "cases": len(lines),
            "cases_by_function": dict(sorted(counts.items())),
            "cases_by_source": dict(sorted(by_source.items())),
            "dependencies": {
                name: importlib.metadata.version(name)
                for name in ("python-dateutil", "jellyfish", "scipy", "numpy", "PyYAML")
            },
            "python": sys.version.split()[0],
            "script": "packages/dojo/scripts/capture_old_dojo_extraction_cases.py",
        }
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join([json.dumps(header, sort_keys=True, ensure_ascii=True)] + lines) + "\n")
    print(f"old commit {commit}, version {llm_dojo_scoring.__version__}")
    print(
        f"golden calls {len(golden_calls)}; captured {len(captured['records'])} test calls; "
        f"wrote {len(lines)} unique cases to {output} ({output.stat().st_size} bytes)"
    )
    for name, count in sorted(counts.items()):
        print(f"  {count:6d} {name}")
    print("by source:", by_source)
    raises: dict[str, int] = {}
    for line in lines:
        case = json.loads(line)
        if "raises" in case["outcome"]:
            label = f"{case['function']}:{case['outcome']['raises']}"
            raises[label] = raises.get(label, 0) + 1
    print("old outcomes that are exceptions:", dict(sorted(raises.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
