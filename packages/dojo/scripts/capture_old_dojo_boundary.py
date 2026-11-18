"""Freeze the old Dojo's golden boundary calls and its classification and Hub-row outputs.

Writes three fixtures under ``packages/dojo/tests/fixtures/golden/``:

``old_dojo_boundary_calls.json``
    Every call that crosses INTO the old in-scope modules (field scoring,
    classification, paired lengths, GT metadata, scored enums, intents) from an
    out-of-scope caller while the old Dojo regenerates its golden baseline
    (``scripts/freeze_goldens.py``), with its arguments (copied at call time) and its
    result. Each ``score_extraction`` call also names the paths in the old
    ``scores.json`` where its ``field_scores`` appear, with their hash. The header
    names the old commit and version and the sha256 of the old ``inputs.json`` and
    ``scores.json``; the run is refused unless the regenerated baseline equals the
    committed ``scores.json`` byte for byte.

``classification_cases.jsonl``
    Differential cases for the classification layer: every classification call
    made while the old ``tests/test_classification.py`` and
    ``tests/test_paired_cardinality.py`` run, plus seeded generated inputs (sorter
    outputs as JSON objects, fenced and quoted text, mixed case, near-miss labels,
    error rows, None, numbers, unicode; paired label lists; raw binary flags;
    F-beta edge values; length mismatches).

``hub_row_cases.jsonl``
    Differential cases for the Hub-row reference adapter: every GT-shaping call made
    while the old ``tests/test_gt_metadata.py`` runs, plus seeded generated Hub rows.

Case lines are ``{"id", "source", "function", "args", "outcome"}`` with the tagged
JSON of ``capture_old_dojo_scalar_cases.py``; ``outcome`` is ``{"result": ...}`` or
``{"raises": "<ExceptionType>", "message": "..."}``. Outputs are computed in this
process with the old Dojo's default settings and no test monkeypatching.

Run it only with an interpreter that can import the old Dojo, never in CI::

    PYTHONDONTWRITEBYTECODE=1 <old-venv>/bin/python packages/dojo/scripts/capture_old_dojo_boundary.py \\
        --old-dojo <llm-dojo-scoring checkout at 23f8234c> \\
        --output-directory packages/dojo/tests/fixtures/golden

It reads the old checkout and writes nothing inside it (children run from a
temporary directory, pytest with its cache plugin off).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

EXPECTED_OLD_COMMIT = "23f8234c3c548bdc203033388bb316e52e588a2c"
DEFAULT_SEED = 20261118
SCRIPT_DIRECTORY = Path(__file__).resolve().parent

#: Old modules whose functions are "in scope" for the boundary capture.
IN_SCOPE_MODULES = (
    "scoring/field_scoring.py",
    "scoring/classification.py",
    "scoring/_paired.py",
    "schemas/gt_metadata.py",
    "schemas/scored_enums.py",
    "schemas/intents.py",
)

#: Old direct test files whose calls feed each differential corpus.
CLASSIFICATION_TEST_FILES = ("tests/test_classification.py", "tests/test_paired_cardinality.py")
HUB_ROW_TEST_FILES = ("tests/test_gt_metadata.py",)

#: (old module file, old function name) -> case function name, per corpus.
CLASSIFICATION_TARGETS: dict[tuple[str, str], str] = {
    ("scoring/classification.py", name): name
    for name in (
        "normalize_label", "exact_match", "failure", "accuracy", "fbeta", "per_class_stats",
        "macro_accuracy", "confusion_matrix", "binary_metrics", "macro_prf", "confusion_accuracy",
        "top_confusions", "class_distribution",
    )
}
CLASSIFICATION_TARGETS[("scoring/_paired.py", "require_equal_lengths")] = "require_equal_lengths"
HUB_ROW_TARGETS: dict[tuple[str, str], str] = {
    ("schemas/gt_metadata.py", "parse_gt_fields"): "parse_hub_fields",
    ("schemas/gt_metadata.py", "gt_presence_map"): "hub_presence_statuses",
    ("schemas/gt_metadata.py", "normalize_field_values"): "parse_field_values",
    ("schemas/gt_metadata.py", "scoring_gt_fields"): "scoring_fields_from_hub_row",
    ("schemas/gt_metadata.py", "presence_expectations_from_cuad_labels"): "presence_expectations_from_clause_labels",
    ("schemas/gt_metadata.py", "derive_presence_from_gt"): "presence_expectations_from_hub_row",
}


def scalar_capture_script() -> ModuleType:
    """Load the scalar capture script, whose tagged JSON rules every Dojo corpus shares."""
    path = SCRIPT_DIRECTORY / "capture_old_dojo_scalar_cases.py"
    spec = importlib.util.spec_from_file_location("capture_old_dojo_scalar_cases", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TAGGED_JSON = scalar_capture_script()
encode = TAGGED_JSON.encode
decode = TAGGED_JSON.decode
Unencodable = TAGGED_JSON.Unencodable


def canonical_text(value: Any) -> str:
    """Canonical JSON text: sorted keys, ASCII, compact."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def sha256_of(value: Any) -> str:
    """sha256 of a value's canonical JSON text."""
    return hashlib.sha256(canonical_text(value).encode("utf-8")).hexdigest()


def plain(value: Any) -> Any:
    """Turn a result object into plain data (a dataclass with ``to_dict`` becomes its dict)."""
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict) and not isinstance(value, (dict, list, tuple, set, frozenset, str, bytes)):
        return to_dict()
    return value


# ---------------------------------------------------------------------------
# Child: the golden boundary capture
# ---------------------------------------------------------------------------


def collect_boundary(old_dojo: Path, output: Path) -> None:
    """Regenerate the old goldens under a profile hook and write every boundary call."""
    package_root = str(old_dojo / "llm_dojo_scoring") + os.sep
    spec = importlib.util.spec_from_file_location("freeze_goldens", old_dojo / "scripts" / "freeze_goldens.py")
    goldens = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(goldens)

    def module_of(filename: str) -> str | None:
        """The in-scope module a file is, or None."""
        if not filename.startswith(package_root):
            return None
        relative = filename[len(package_root):]
        return relative if relative in IN_SCOPE_MODULES else None

    current_case: list[str | None] = [None]
    open_calls: dict[int, tuple[str, str, dict[str, Any], str]] = {}
    records: list[dict[str, Any]] = []
    excluded: dict[str, int] = {}

    def profile(frame, event, arg):  # noqa: ANN001 - sys.setprofile signature
        """Record public in-scope calls whose caller is outside the in-scope modules."""
        if current_case[0] is None:
            return
        module = module_of(frame.f_code.co_filename)
        if module is None:
            return
        name = frame.f_code.co_name
        if event == "call":
            caller = frame.f_back
            if caller is None or module_of(caller.f_code.co_filename) is not None:
                return
            if name.startswith("_") or name.startswith("<"):
                return
            code = frame.f_code
            arguments = {
                variable: copy.deepcopy(frame.f_locals.get(variable))
                for variable in code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
            }
            caller_name = f"{caller.f_code.co_filename.rsplit('llm_dojo_scoring' + os.sep, 1)[-1]}:{caller.f_code.co_name}"
            open_calls[id(frame)] = (module, name, arguments, caller_name)
        elif event == "return" and id(frame) in open_calls:
            module, name, arguments, caller_name = open_calls.pop(id(frame))
            try:
                record = {
                    "case": current_case[0],
                    "module": module,
                    "function": name,
                    "caller": caller_name,
                    "args": encode(arguments),
                    "result": encode(plain(arg)),
                }
            except Unencodable as error:
                key = f"{module}:{name}:{error}"
                excluded[key] = excluded.get(key, 0) + 1
                return
            records.append(record)

    inputs = goldens.load_inputs()
    goldens._ensure_default_settings()
    payload: dict[str, Any] = {"task_kinds": {}, "suites": {}}
    sys.setprofile(profile)
    try:
        for task in sorted(goldens.TASK_KINDS):
            current_case[0] = "task:" + task
            payload["task_kinds"][task] = goldens._collect_task_case(task, inputs["task_cases"][task])
        for name in sorted(goldens.list_suites(live_only=True)):
            current_case[0] = "suite:" + name
            payload["suites"][name] = goldens._collect_suite_case(name, inputs["suite_cases"][name])
    finally:
        current_case[0] = None
        sys.setprofile(None)
    regenerated_identical = goldens.render_goldens(payload) == goldens.SCORES_PATH.read_text(encoding="utf-8")
    output.write_text(json.dumps({"records": records, "excluded": excluded,
                                  "regenerated_identical": regenerated_identical}))


def scores_json_paths(scores: Any, target: Any) -> list[str]:
    """Every path in ``scores`` (``/suites/x/score/field_scores``) whose canonical JSON equals ``target``'s."""
    wanted = canonical_text(target)
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        """Depth-first search in key order."""
        if canonical_text(node) == wanted:
            found.append(path)
            return
        if isinstance(node, dict):
            for key in sorted(node):
                walk(node[key], f"{path}/{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}/{index}")

    walk(scores, "")
    return found


# ---------------------------------------------------------------------------
# Child: the old direct tests' calls
# ---------------------------------------------------------------------------


def collect_test_inputs(old_dojo: Path, output: Path, targets_name: str) -> None:
    """Run the old direct tests of one corpus under a profile hook and write every targeted call's arguments."""
    targets = CLASSIFICATION_TARGETS if targets_name == "classification" else HUB_ROW_TARGETS
    test_files = CLASSIFICATION_TEST_FILES if targets_name == "classification" else HUB_ROW_TEST_FILES
    package_root = str(old_dojo / "llm_dojo_scoring") + os.sep
    records: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    def profile(frame, event, arg):  # noqa: ANN001 - sys.setprofile signature
        """Record the arguments of a targeted function at call time."""
        if event != "call" or not frame.f_code.co_filename.startswith(package_root):
            return
        name = targets.get((frame.f_code.co_filename[len(package_root):], frame.f_code.co_name))
        if name is None:
            return
        code = frame.f_code
        arguments = {
            variable: frame.f_locals.get(variable)
            for variable in code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
        }
        if arguments.get("valid") is not None:
            skipped[f"{name}:custom_valid_table"] = skipped.get(f"{name}:custom_valid_table", 0) + 1
            return
        arguments.pop("valid", None)
        try:
            records.append({"function": name, "args": encode(copy.deepcopy(arguments))})
        except Unencodable as error:
            skipped[f"{name}:{error}"] = skipped.get(f"{name}:{error}", 0) + 1
        except TypeError as error:  # an iterator argument cannot be copied or replayed
            skipped[f"{name}:{type(error).__name__}"] = skipped.get(f"{name}:{type(error).__name__}", 0) + 1

    import pytest

    sys.setprofile(profile)
    try:
        exit_code = pytest.main(
            ["-q", "-p", "no:cacheprovider", "--rootdir", str(old_dojo)] + [str(old_dojo / path) for path in test_files]
        )
    finally:
        sys.setprofile(None)
    output.write_text(json.dumps({"exit_code": int(exit_code), "records": records, "skipped": skipped}))


# ---------------------------------------------------------------------------
# Generated inputs
# ---------------------------------------------------------------------------

LIVE_CLASSES = ("merger_agreement", "corporate_record", "correspondence", "insurance_claim", "contract")
RETIRED_CLASSES = ("compliance_filing", "court_opinion", "due_diligence")
FOREIGN_LABELS = ("license", "other", "unknown", "invoice", "auto_policy", "a", "b", "yes", "no")


def label_surfaces(label: str) -> list[str]:
    """Written forms of one label a sorter might emit."""
    spaced = label.replace("_", " ")
    return [
        label, label.upper(), spaced, spaced.title(), f"  {spaced}  ", label.replace("_", "-"),
        label.replace("_", ""), f"{label}s", f"{spaced}s", label[:-1], f"{label}x", f"x{label}",
        spaced.replace(" ", "  "), spaced.replace(" ", "\t"), f"This document is a {spaced}.",
        f"not a {spaced}", f"**{spaced}**", f"`{label}`", f'"{label}"', f"'{label}'", f"_{label}_",
        f'{{"doc_type": "{spaced.title()}"}}', f'{{"doc_type": "{label}", "confidence": 0.91}}',
        f'{{"DOC_TYPE": "{label.upper()}"}}', f'  {{"doc_type": "{label}"}}  ', f'{{"doc_type": "{label}"',
        f'```json\n{{"doc_type": "{label}"}}\n```', f"```\n{label}\n```", f'["{label}"]',
        f'{{"label": "{label}"}}', f'{{"doc_type": "", "label": "{label}"}}', f"ERROR: {label}",
        f"Error: {label}", f" ERROR: {label}", f"ERROR:{label}", f"Answer: {spaced}", f"{spaced}?",
        f"{spaced} or contract", f"contract or {spaced}", f"{label}_agreement", f"{label}.pdf",
    ]


def sorter_outputs(rng: random.Random) -> list[Any]:
    """Sorter outputs: every label surface, hostile values and random mixtures."""
    outputs: list[Any] = [
        None, "", " ", "\t\n", 0, 1, -1, 1.5, float("nan"), float("inf"), True, False, [], ["contract"],
        {"doc_type": "contract"}, ("contract",), "None", "null", "{}", "{ }", '{"doc_type": null}',
        '{"doc_type": ""}', '{"doc_type": 0}', '{"doc_type": 7}', '{"doc_type": true}',
        '{"doc_type": ["contract"]}', '{"doc_type": {"kind": "contract"}}', '{"doc_type": "\\u0043ontract"}',
        "{bad json}", "{'doc_type': 'contract'}", '[{"doc_type": "contract"}]', '{"a": ' * 50 + "1" + "}" * 50,
        "日本語", "契約 contract", "ＣＯＮＴＲＡＣＴ", "contract​", "con­tract", "cóntract",
        "İnsurance claim", "insurance claim", "merger agreement", "Merger_Agreement",
        "MERGER AGREEMENT", "mergeragreement", "merger__agreement", "merger - agreement",
        "corporate records", "correspondences", "insurance claims", "contracts", "subcontract",
        "contract-based", "contract_", "_contract", "re: contract", "email correspondence",
        "correspondence about an insurance claim", "insurance claim correspondence",
        "a merger agreement amending a contract", "contract amendment to a merger agreement",
        "corporate record of a merger agreement", "compliance filing", "court opinion",
        "due diligence", "court_opinion", "compliance_filing", "due_diligence",
        "x" * 5000 + " contract", "contract " * 400, "\ud800contract", "ERROR: ", "ERROR", "error: x",
    ]
    for label in LIVE_CLASSES + RETIRED_CLASSES + FOREIGN_LABELS:
        outputs += label_surfaces(label)
    pool = [value for value in outputs if isinstance(value, str)]
    for _ in range(150):
        first, second = rng.choice(pool), rng.choice(pool)
        outputs.append(rng.choice([f"{first} {second}", f"{first}\n{second}", first.upper() + second]))
    return outputs


def flag_values() -> list[Any]:
    """Raw binary flags: strings, numbers, booleans and hostile values."""
    return [True, False, 1, 0, 2, -1, 1.0, 0.0, float("nan"), "yes", "Y", " true ", "TRUE", "1", "1.0", "1.00",
            "no", "", " ", "on", "0", "false", None, [], ["yes"], "ERROR: x", "contract"]


def generated_classification_cases(rng: random.Random) -> list[dict[str, Any]]:
    """Every generated classification case as ``{"function", "args"}``."""
    cases: list[dict[str, Any]] = []

    def add(function: str, **arguments: Any) -> None:
        """Append a case, skipping arguments the format cannot hold."""
        try:
            cases.append({"function": function, "args": encode(arguments)})
        except Unencodable:
            pass

    every_output = sorter_outputs(rng)
    for value in every_output:
        add("normalize_label", value=value)
        add("failure", output=value, expected="contract")
    outputs = [value for value in every_output if not (isinstance(value, str) and len(value) > 200)]
    for value in outputs:
        add("class_distribution", labels=[value, value, "contract"])
    expected_pool: list[Any] = list(LIVE_CLASSES) + list(RETIRED_CLASSES) + ["license", "a", "b", "", None,
                                                                             "Contract", "corporate record",
                                                                             "ERROR: expected", 3]
    for _ in range(600):
        add("exact_match", output=rng.choice(outputs), expected=rng.choice(expected_pool))
    for _ in range(450):
        size = rng.randint(0, 9)
        expected = [rng.choice(expected_pool) for _ in range(size)]
        predicted = [rng.choice(outputs + expected_pool) for _ in range(size)]
        add("accuracy", expected=expected, predicted=predicted)
        add("per_class_stats", expected=expected, predicted=predicted)
        add("macro_accuracy", expected=expected, predicted=predicted)
        explicit = rng.choice([None, [], list(LIVE_CLASSES), ["b", "a"], ["contract", "contract"]])
        add("confusion_matrix", expected=expected, predicted=predicted, labels=explicit)
        add("macro_prf", expected=expected, predicted=predicted, normalize=rng.random() < 0.7)
        add("binary_metrics", expected=expected, predicted=predicted,
            positive=rng.choice(list(LIVE_CLASSES) + ["license", "true", ""]), normalize=True)
        add("class_distribution", labels=expected + predicted)
    flags = flag_values()
    for _ in range(300):
        size = rng.randint(0, 8)
        add("binary_metrics", expected=[rng.choice(flags) for _ in range(size)],
            predicted=[rng.choice(flags) for _ in range(size)], positive=rng.choice(["yes", "true", "contract"]),
            normalize=rng.random() < 0.3)
        add("macro_prf", expected=[rng.choice(flags) for _ in range(size)],
            predicted=[rng.choice(flags) for _ in range(size)], normalize=False)
    rates = [0.0, 1e-9, 0.0001, 0.25, 0.3333, 0.5, 0.6667, 0.75, 1.0, -0.5, 1.5, float("nan"), float("inf")]
    for precision in rates:
        for recall in rates:
            for beta in (1.0, 2.0, 0.5, 0.0, -1.0):
                add("fbeta", precision=precision, recall=recall, beta=beta)
    for _ in range(200):
        side = rng.randint(0, 5)
        matrix = [[rng.choice([0, 0, 1, 2, 3, 7]) for _ in range(side)] for _ in range(side)]
        labels = [f"class_{index}" for index in range(side)]
        add("confusion_accuracy", matrix=matrix)
        add("top_confusions", matrix=matrix, labels=labels, k=rng.choice([0, 1, 2, 3, 5, 100, -1]))
    for matrix in ([[1, 2]], [[1], [2]], [[1, 2], [3]], [], [[]], [[0, 0], [0, 0]], [[5]]):
        add("confusion_accuracy", matrix=matrix)
        add("top_confusions", matrix=matrix, labels=["a", "b"], k=5)
    add("top_confusions", matrix=[[0, 3], [3, 0]], labels=["a"], k=5)
    for expected_size, predicted_size in ((2, 1), (1, 2), (0, 1), (1, 0), (3, 5), (0, 0)):
        expected = ["contract"] * expected_size
        predicted = ["ERROR: unavailable"] * predicted_size
        for function in ("accuracy", "per_class_stats", "macro_accuracy", "confusion_matrix", "macro_prf",
                         "require_equal_lengths"):
            add(function, expected=expected, predicted=predicted)
        add("binary_metrics", expected=[True] * expected_size, predicted=[False] * predicted_size,
            positive="yes", normalize=False)
    return cases


PRESENCE_STATUSES = ("populated", "not_applicable", "schema_documented_absence", "pending_annotation", "unknown", "")
HUB_VALUES: tuple[Any, ...] = (
    "Acme Corp", "", " ", "[]", "{}", '["Bylaws", "Charter"]', '{"a": 1}', "41", "true", "null", "N/A", "nan",
    None, 0, 3.5, False, ["x"], [], {"k": "v"}, "[1, 2", "Ab\ud800c",
)
HUB_FIELD_NAMES = (
    "claim_number", "adjuster", "denial_reasons", "claim_type", "cms_source_table", "supporting_documents",
    "parties", "effective_date", "governing_law", "intent", "subject_matter", "keywords", "sender",
    "token_estimate", "intent_status", "expected_subclass", "label_evidence", "unmapped_extra",
)
CLASS_FIELD_TYPES: dict[str, dict[str, str]] = {
    "insurance_claim": {"claim_number": "id", "adjuster": "name", "denial_reasons": "entity_list:free_text",
                        "claim_type": "label", "supporting_documents": "entity_list", "intent": "label",
                        "subject_matter": "free_text", "keywords": "entity_list:name"},
    "contract": {"parties": "entity_list:name", "effective_date": "date", "governing_law": "name",
                 "intent": "label", "keywords": "entity_list:name"},
    "correspondence": {"sender": "name", "intent": "label", "subject_matter": "free_text"},
}


def generated_hub_rows(rng: random.Random) -> list[dict[str, Any]]:
    """Random Hub ``gt_fields`` rows with presence maps, stringified containers and annotation keys."""
    rows: list[dict[str, Any]] = []
    for _ in range(260):
        names = rng.sample(HUB_FIELD_NAMES, rng.randint(0, 8))
        row: dict[str, Any] = {name: rng.choice(HUB_VALUES) for name in names}
        presence = {name: rng.choice(PRESENCE_STATUSES) for name in rng.sample(HUB_FIELD_NAMES, rng.randint(0, 6))}
        form = rng.randrange(5)
        if form == 0:
            row["gt_presence"] = json.dumps(presence)
        elif form == 1:
            row["gt_presence"] = presence
        elif form == 2:
            row["gt_presence"] = rng.choice(["not json", "[]", "null", 5, ""])
        rows.append(row)
    return rows


def clause_label_maps(rng: random.Random) -> list[Any]:
    """CUAD-style category -> spans maps, stringified or not, with placeholders and malformed spans."""
    span_choices: list[Any] = [
        {"start": 0, "text": "The Agreement shall terminate."}, {"start": 3, "text": "[*]"}, {"start": 1, "text": "____"},
        {"text": "."}, {"start": 2}, {"text": ""}, {"text": 42}, "loose text", None, {"text": "Ünïcode clause 9"},
    ]
    maps: list[Any] = [None, "", "{}", "[]", "not json", 5, [], {}, {"Governing Law": []}, {"Governing Law": "[]"},
                       {"Governing Law": "not a list"}, {"Governing Law": None}]
    categories = ["Governing Law", "Termination For Convenience", "Exclusivity", "Cap On Liability", "Parties"]
    for _ in range(120):
        labels = {
            category: [rng.choice(span_choices) for _ in range(rng.randint(0, 3))]
            for category in rng.sample(categories, rng.randint(1, 4))
        }
        maps.append(json.dumps(labels) if rng.random() < 0.3 else labels)
    return maps


def generated_hub_row_cases(rng: random.Random) -> list[dict[str, Any]]:
    """Every generated Hub-row case as ``{"function", "args"}``."""
    cases: list[dict[str, Any]] = []

    def add(function: str, **arguments: Any) -> None:
        """Append a case, skipping arguments the format cannot hold."""
        try:
            cases.append({"function": function, "args": encode(arguments)})
        except Unencodable:
            pass

    rows = generated_hub_rows(rng)
    raw_forms: list[Any] = ["", "  ", "not a dict", "[1, 2]", "41", "{'a': 1}", '{"a": "[]"}', None, 5, [], ("a",)]
    for row in rows[:60]:
        safe = {key: value for key, value in row.items() if isinstance(value, (str, int, float, bool, type(None)))}
        raw_forms += [row, json.dumps(safe), repr(safe)]
    for raw in raw_forms:
        add("parse_hub_fields", raw=raw)
        add("parse_field_values", record=raw)
        add("hub_presence_statuses", fields=raw if isinstance(raw, dict) or raw is None else {"gt_presence": raw})
    for row in rows:
        doc_type = rng.choice([None, "insurance_claim", "contract", "correspondence", "Insurance_Claim", "unknown"])
        field_types = rng.choice([None, {}] + list(CLASS_FIELD_TYPES.values()))
        add(
            "scoring_fields_from_hub_row",
            fields=row,
            field_types=field_types,
            extra_keys=rng.choice([(), ("unmapped_extra",), ("gt_presence", "token_estimate")]),
            drop_unmapped=rng.random() < 0.8,
            doc_type=doc_type,
            subclass=rng.choice([None, "", "carrier", " PDE ", "inpatient", "outpatient", "auto", "property", 5]),
        )
    for labels in clause_label_maps(rng):
        add("presence_expectations_from_clause_labels", labels=labels, field=rng.choice(["cuad_clauses", "clauses"]))
        status = rng.choice(PRESENCE_STATUSES)
        row = {"cuad_clause_labels": labels, "gt_presence": json.dumps({"cuad_clause_labels": status})}
        add("presence_expectations_from_hub_row", fields=row, field="cuad_clauses")
    add("presence_expectations_from_hub_row", fields=None, field="cuad_clauses")
    add("presence_expectations_from_hub_row", fields={}, field="cuad_clauses")
    return cases


# ---------------------------------------------------------------------------
# Old outputs
# ---------------------------------------------------------------------------


def old_functions() -> dict[str, Any]:
    """Map each case function name to the old callable (fresh import, default settings)."""
    from llm_dojo_scoring.config import clear_settings_cache
    from llm_dojo_scoring.schemas import gt_metadata
    from llm_dojo_scoring.scoring import _paired, classification

    clear_settings_cache()
    modules = {
        "scoring/classification.py": classification,
        "scoring/_paired.py": _paired,
        "schemas/gt_metadata.py": gt_metadata,
    }
    functions: dict[str, Any] = {}
    for targets in (CLASSIFICATION_TARGETS, HUB_ROW_TARGETS):
        for (module_file, old_name), new_name in targets.items():
            functions[new_name] = getattr(modules[module_file], old_name)
    return functions


def run_case(functions: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Call the old function on a deep copy of the arguments and return the encoded outcome."""
    arguments = decode(copy.deepcopy(case["args"]))
    try:
        result = functions[case["function"]](**arguments)
    except Exception as error:  # the exception is the recorded outcome
        return {"raises": type(error).__name__, "message": str(error)}
    return {"result": encode(result)}


def capture_child(old_dojo: Path, flag: str, extra: list[str]) -> dict[str, Any]:
    """Run one child phase of this script in a temporary directory and return its JSON output."""
    environment = {key: value for key, value in os.environ.items() if key != "LLM_DOJO_SCORING_CONFIG"}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch) / "captured.json"
        subprocess.run(
            [sys.executable, __file__, "--old-dojo", str(old_dojo), flag, str(output), *extra],
            check=True, cwd=scratch, env=environment, stdout=subprocess.DEVNULL,
        )
        return json.loads(output.read_text())


def write_corpus(
    path: Path, description: str, sources: list[tuple[str, dict[str, Any]]], functions: dict[str, Any],
    header_extra: dict[str, Any],
) -> dict[str, int]:
    """Compute old outcomes for unique cases and write a JSONL corpus; return counts per function."""
    seen: set[str] = set()
    lines: list[str] = []
    counts: dict[str, int] = {}
    for source, case in sources:
        key = json.dumps([case["function"], case["args"]], sort_keys=True, ensure_ascii=True)
        if key in seen:
            continue
        seen.add(key)
        outcome = run_case(functions, case)
        lines.append(canonical_text({
            "id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
            "source": source,
            "function": case["function"],
            "args": case["args"],
            "outcome": outcome,
        }))
        counts[case["function"]] = counts.get(case["function"], 0) + 1
    header = {"header": {"description": description, "cases": len(lines),
                         "cases_by_function": dict(sorted(counts.items())), **header_extra}}
    path.write_text("\n".join([json.dumps(header, sort_keys=True, ensure_ascii=True)] + lines) + "\n")
    return counts


def main() -> int:
    """Capture, generate, compute and write the three fixtures; print a digest."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--old-dojo", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--collect-boundary", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--collect-test-inputs", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--targets", choices=("classification", "hub_rows"), help=argparse.SUPPRESS)
    options = parser.parse_args()
    old_dojo = options.old_dojo.resolve()
    sys.path.insert(0, str(old_dojo))
    if options.collect_boundary:
        collect_boundary(old_dojo, options.collect_boundary)
        return 0
    if options.collect_test_inputs:
        collect_test_inputs(old_dojo, options.collect_test_inputs, options.targets)
        return 0
    if options.output_directory is None:
        parser.error("--output-directory is required")
    output_directory = options.output_directory.resolve()
    if old_dojo == output_directory or old_dojo in output_directory.parents:
        parser.error("refusing to write inside the old Dojo checkout")
    commit = subprocess.run(
        ["git", "-C", str(old_dojo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if commit != EXPECTED_OLD_COMMIT:
        print(f"warning: old Dojo is at {commit}, expected {EXPECTED_OLD_COMMIT}", file=sys.stderr)

    boundary = capture_child(old_dojo, "--collect-boundary", [])
    if not boundary["regenerated_identical"]:
        print("the regenerated old golden baseline differs from scores.json; refusing to freeze", file=sys.stderr)
        return 1
    classification_inputs = capture_child(old_dojo, "--collect-test-inputs", ["--targets", "classification"])
    hub_row_inputs = capture_child(old_dojo, "--collect-test-inputs", ["--targets", "hub_rows"])
    for name, captured in (("classification", classification_inputs), ("hub_rows", hub_row_inputs)):
        if captured["exit_code"] != 0:
            print(f"old {name} tests exited {captured['exit_code']}; refusing to freeze", file=sys.stderr)
            return 1

    os.environ.pop("LLM_DOJO_SCORING_CONFIG", None)
    import llm_dojo_scoring

    golden_directory = old_dojo / "tests" / "fixtures" / "golden"
    inputs_bytes = (golden_directory / "inputs.json").read_bytes()
    scores_bytes = (golden_directory / "scores.json").read_bytes()
    scores = json.loads(scores_bytes)
    common_header = {
        "old_commit": commit,
        "old_version": llm_dojo_scoring.__version__,
        "old_inputs_json_sha256": hashlib.sha256(inputs_bytes).hexdigest(),
        "old_scores_json_sha256": hashlib.sha256(scores_bytes).hexdigest(),
        "python": sys.version.split()[0],
        "script": "packages/dojo/scripts/capture_old_dojo_boundary.py",
    }

    calls = []
    for index, record in enumerate(boundary["records"]):
        call = {"index": index, **record}
        if record["function"] == "score_extraction":
            field_scores = decode(record["result"])["field_scores"]
            call["scores_json_paths"] = scores_json_paths(scores, field_scores)
            call["field_scores_sha256"] = sha256_of(field_scores)
        calls.append(call)
    unmatched = [call["index"] for call in calls if call["function"] == "score_extraction"
                 and not call["scores_json_paths"]]
    if unmatched:
        print(f"score_extraction calls not found in scores.json: {unmatched}; refusing to freeze", file=sys.stderr)
        return 1
    counts: dict[str, int] = {}
    for call in calls:
        counts[call["function"]] = counts.get(call["function"], 0) + 1
    boundary_document = {
        "header": {
            "description": "calls crossing into the old in-scope scoring modules during the old golden run",
            **common_header,
            "in_scope_modules": list(IN_SCOPE_MODULES),
            "calls": len(calls),
            "calls_by_function": dict(sorted(counts.items())),
            "excluded_unencodable": boundary["excluded"],
            "regenerated_baseline_identical": True,
        },
        "calls": calls,
    }
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "old_dojo_boundary_calls.json").write_text(
        json.dumps(boundary_document, sort_keys=True, ensure_ascii=True, indent=1) + "\n"
    )

    functions = old_functions()
    rng = random.Random(options.seed)
    classification_counts = write_corpus(
        output_directory / "classification_cases.jsonl",
        "llm-dojo-scoring outputs for the classification layer (differential parity corpus)",
        [("old_tests", record) for record in classification_inputs["records"]]
        + [("generated", case) for case in generated_classification_cases(rng)],
        functions,
        {**common_header, "seed": options.seed, "captured_test_files": list(CLASSIFICATION_TEST_FILES),
         "captured_calls": len(classification_inputs["records"]), "captured_skipped": classification_inputs["skipped"]},
    )
    hub_row_counts = write_corpus(
        output_directory / "hub_row_cases.jsonl",
        "llm-dojo-scoring outputs for the Hub-row GT shaping helpers (differential parity corpus)",
        [("old_tests", record) for record in hub_row_inputs["records"]]
        + [("generated", case) for case in generated_hub_row_cases(rng)],
        functions,
        {**common_header, "seed": options.seed, "captured_test_files": list(HUB_ROW_TEST_FILES),
         "captured_calls": len(hub_row_inputs["records"]), "captured_skipped": hub_row_inputs["skipped"]},
    )
    print(f"old commit {commit}, version {llm_dojo_scoring.__version__}")
    print(f"boundary calls: {len(calls)} (excluded {sum(boundary['excluded'].values())} unencodable)")
    print("every score_extraction result was found in scores.json")
    for name, corpus_counts in (("classification", classification_counts), ("hub rows", hub_row_counts)):
        print(f"{name} corpus: {sum(corpus_counts.values())} cases")
        for function, count in sorted(corpus_counts.items()):
            print(f"  {count:6d} {function}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
