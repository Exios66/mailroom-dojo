"""Freeze the old Dojo's outputs for the scalar layers as a differential parity corpus.

Writes ``packages/dojo/tests/fixtures/differential/scalar_cases.jsonl``: a header
line (old commit, old version, seed, counts, dependency versions), then one case per
line ``{"id", "source", "function", "args", "outcome"}``. ``outcome`` is
``{"result": ...}`` or ``{"raises": "<ExceptionType>"}``. The new package replays
every case in ``tests/test_differential_scalar_parity.py``.

Inputs come from two places:

1. Every call to a targeted old function made while the old Dojo's own direct tests
   run (captured with ``sys.setprofile`` in a child process). Calls that pass an
   embedding object or a settings object are skipped.
2. Seeded generated inputs: dates in many formats, money strings, names with
   suffixes, CMS identifiers, label surfaces for every vocabulary, blanks, unicode
   and hostile types, and embedding rescue with deterministic fake backends.

Every output is computed afterwards in this process, with the old Dojo's default
settings and no test monkeypatching, so it is the old behaviour itself.

Run it only with an interpreter that can import the old Dojo, never in CI::

    PYTHONDONTWRITEBYTECODE=1 <old-venv>/bin/python packages/dojo/scripts/capture_old_dojo_scalar_cases.py \\
        --old-dojo <llm-dojo-scoring checkout at 23f8234c> \\
        --output packages/dojo/tests/fixtures/differential/scalar_cases.jsonl

It reads the old checkout and writes nothing inside it (pytest runs from a temporary
directory with its cache plugin off).
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime
import decimal
import hashlib
import importlib.metadata
import json
import math
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EXPECTED_OLD_COMMIT = "23f8234c3c548bdc203033388bb316e52e588a2c"
DEFAULT_SEED = 20261118

#: Old direct test files whose calls are captured.
OLD_TEST_FILES = (
    "tests/test_field_scoring.py",
    "tests/test_gt_metadata.py",
    "tests/test_scored_enums.py",
    "tests/test_intents.py",
    "tests/test_scoring_fixes_127.py",
    "tests/test_v026_truth_fixes.py",
    "tests/test_scoring_fixes_133_132.py",
    "tests/test_parse_compat_enum_key.py",
    "tests/test_equivalences.py",
    "tests/test_corpus.py",
    "tests/test_cuad_date_fields.py",
)

#: (old module file, old function name) -> case function name (the new name).
TARGETS: dict[tuple[str, str], str] = {
    ("scoring/field_scoring.py", "normalize_text"): "normalize_text",
    ("scoring/field_scoring.py", "_tokenize"): "tokenize",
    ("scoring/field_scoring.py", "_containment_tokens"): "containment_tokens",
    ("scoring/field_scoring.py", "_seq_ratio"): "sequence_ratio",
    ("scoring/field_scoring.py", "_token_set_ratio"): "token_set_ratio",
    ("scoring/field_scoring.py", "_token_f1"): "token_f1",
    ("scoring/field_scoring.py", "_jaro_winkler"): "jaro_winkler",
    ("scoring/field_scoring.py", "_parse_date_parts"): "parse_date_parts",
    ("scoring/field_scoring.py", "_parse_date"): "parse_date",
    ("scoring/field_scoring.py", "parse_date"): "parse_date",
    ("scoring/field_scoring.py", "_parse_money"): "parse_money",
    ("scoring/field_scoring.py", "parse_money"): "parse_money",
    ("scoring/field_scoring.py", "_stated_years"): "stated_years",
    ("scoring/field_scoring.py", "_integral_float_as_text"): "integral_float_as_text",
    ("scoring/field_scoring.py", "_date_expected_is_relative"): "expected_date_is_relative",
    ("scoring/field_scoring.py", "_date_expected_is_null"): "expected_date_is_null",
    ("scoring/field_scoring.py", "_date_is_blank_or_bare_label"): "date_is_blank_or_bare_label",
    ("scoring/field_scoring.py", "_date_phrase_contained"): "date_phrase_contained",
    ("scoring/field_scoring.py", "_contains_phrase"): "contains_phrase",
    ("scoring/field_scoring.py", "score_id_field"): "score_id_field",
    ("scoring/field_scoring.py", "score_money_field"): "score_money_field",
    ("scoring/field_scoring.py", "score_name_field"): "score_name_field",
    ("scoring/field_scoring.py", "score_label_field"): "score_label_field",
    ("scoring/field_scoring.py", "score_free_text_field"): "score_free_text_field",
    ("scoring/field_scoring.py", "score_containment_field"): "score_containment_field",
    ("scoring/field_scoring.py", "score_date_field"): "score_date_field",
    ("scoring/field_scoring.py", "_date_year_unverified"): "date_year_unverified",
    ("scoring/field_scoring.py", "_date_match_similarity"): "date_match_similarity",
    ("scoring/field_scoring.py", "_cms_doc_signature"): "cms_document_signature",
    ("scoring/field_scoring.py", "score_cms_doc_ref"): "score_cms_doc_ref",
    ("scoring/field_scoring.py", "_heuristic_field_type"): "heuristic_field_type",
    ("scoring/field_scoring.py", "is_entity_list"): "is_entity_list",
    ("schemas/gt_metadata.py", "parse_json_container"): "parse_json_container",
    ("schemas/gt_metadata.py", "is_empty_value"): "is_empty_value",
    ("schemas/scored_enums.py", "canonical_label"): "canonical_label",
    ("schemas/intents.py", "normalize_intent"): "normalize_intent",
    ("schemas/equivalences.py", "normalize_subtype"): "normalize_subtype",
    ("schemas/equivalences.py", "normalize_doc_subclass"): "normalize_doc_subclass",
    ("schemas/corpus.py", "normalize_corpus_subclass"): "normalize_corpus_subclass",
}

SCORERS_WITH_EMBEDDING = (
    "score_id_field",
    "score_money_field",
    "score_name_field",
    "score_label_field",
    "score_free_text_field",
    "score_containment_field",
    "score_date_field",
    "date_match_similarity",
    "score_cms_doc_ref",
)


# ---------------------------------------------------------------------------
# Tagged JSON: the values JSON cannot hold directly
# ---------------------------------------------------------------------------


class Unencodable(Exception):
    """A value the corpus format cannot hold (the case is skipped and counted)."""


def encode(value: Any) -> Any:
    """Encode a value as tagged JSON (non-finite floats, decimals, tuples, sets, dates, date parts)."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"$float": "nan"}
        if math.isinf(value):
            return {"$float": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, decimal.Decimal):
        return {"$decimal": str(value)}
    if type(value).__name__ in ("_DateParts", "DateParts"):
        return {"$date_parts": [encode(part) for part in value]}
    if isinstance(value, tuple):
        return {"$tuple": [encode(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        return {
            "$set": sorted(
                (encode(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True)
            )
        }
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return {"$date": value.isoformat()}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise Unencodable("dict with non-string keys")
        if any(key.startswith("$") for key in value):
            raise Unencodable("dict key starting with $")
        return {key: encode(item) for key, item in value.items()}
    raise Unencodable(type(value).__name__)


def decode(value: Any) -> Any:
    """Invert :func:`encode` (fake similarity specs are left as tagged dicts)."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if isinstance(value, dict):
        if "$float" in value:
            return float(value["$float"])
        if "$decimal" in value:
            return decimal.Decimal(value["$decimal"])
        if "$tuple" in value:
            return tuple(decode(item) for item in value["$tuple"])
        if "$set" in value:
            return set(decode(item) for item in value["$set"])
        if "$date" in value:
            return datetime.date.fromisoformat(value["$date"])
        if "$similarity" in value:
            return value
        return {key: decode(item) for key, item in value.items()}
    return value


# ---------------------------------------------------------------------------
# Phase 1 (child process): capture the old tests' calls
# ---------------------------------------------------------------------------


def collect_test_inputs(old_dojo: Path, output: Path) -> None:
    """Run the old direct tests under a profile hook and write every targeted call's arguments."""
    package_root = str(old_dojo / "llm_dojo_scoring") + os.sep
    records: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    def profile(frame, event, arg):  # noqa: ANN001 - sys.setprofile signature
        """Record the arguments of a targeted function at call time."""
        if event != "call":
            return
        filename = frame.f_code.co_filename
        if not filename.startswith(package_root):
            return
        key = (filename[len(package_root) :], frame.f_code.co_name)
        name = TARGETS.get(key)
        if name is None:
            return
        code = frame.f_code
        arguments = {
            variable: frame.f_locals.get(variable)
            for variable in code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
        }
        if arguments.get("embedding") is not None or arguments.get("settings") is not None:
            skipped[name + ":embedding_or_settings"] = skipped.get(name + ":embedding_or_settings", 0) + 1
            return
        arguments.pop("embedding", None)
        arguments.pop("settings", None)
        try:
            records.append({"function": name, "args": encode(copy.deepcopy(arguments))})
        except Unencodable as error:
            skipped[f"{name}:{error}"] = skipped.get(f"{name}:{error}", 0) + 1

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


# ---------------------------------------------------------------------------
# Phase 2: generated inputs
# ---------------------------------------------------------------------------

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
WEEKDAYS = ["Monday", "Tue.", "Wednesday,", "thu", "Friday", "Sat", "sunday"]


def date_texts(rng: random.Random, count: int) -> list[Any]:
    """Date strings in many formats, blanks, relative dates and hostile values."""
    fixed = [
        "",
        " ",
        "____ day of ________, 19__",
        "Effective Date:",
        "the date first written above",
        "nine months after the date hereof",
        "ninety (90) days after the Effective Date",
        "thirty-one days prior to closing",
        "blank",
        "N/A",
        "FY2014",
        "2014",
        "1999",
        "Q3 2014",
        "Augus 18, 2014",
        "12",
        "12th",
        "Tuesday",
        "Tuesday, December 12, 2014",
        "2014-08",
        "August",
        "May",
        "march of",
        "18 August",
        "August 18",
        "18th day of August",
        "16 day of August. 2012",
        "1st day of April, 2007 (",
        "March 12,1999",
        "August 1-3, 2014",
        "1-3 August 2014",
        "2014-08-18",
        "18-08-2014",
        "08/18/2014",
        "1/1/77",
        "1/1/50",
        "1/1/68",
        "1/1/69",
        "1/1/00",
        "March 3, 99",
        "1/1/1877",
        "February 29, 2000",
        "February 30, 2014",
        "Feb 29 2001",
        "2014-13-01",
        "31/12/2014",
        "December 31st, 1999",
        "the 3rd of May 2020",
        "Monday August 2014",
        "August 1, 2014 and August 18, 2015",
        "2014 to 2016",
        "2014.0",
        "１８ August ２０１４",
        "18 August 2014",
        "August\t18,\n2014",
        "Ab\ud800c 2014",
        "9999-12-31",
        "0001-01-01",
        "10000-01-01",
        "August 18, 2014 at 10:30 a.m.",
        "noon",
        "10:30",
        "2014-08-18T10:30:00Z",
        "seventeenth of March",
        "March 3rd",
        "3/4",
        "2014/08/18",
        "18 Aug 2014",
        "Aug. 18, 2014",
        "Sept 18, 2014",
        "le 18 août 2014",
        None,
        2014,
        2014.0,
        2014.5,
        float("nan"),
        float("inf"),
        True,
        0,
        [],
        ["August 18, 2014"],
        {"date": "2014-08-18"},
    ]
    generated: list[Any] = []
    for _ in range(count):
        year = rng.choice([1969, 1977, 1999, 2000, 2004, 2012, 2014, 2015, 2016, 2020, 2031, 2068])
        month = rng.randint(1, 12)
        day = rng.choice([1, 2, 3, 9, 12, 15, 18, 28, 29, 30, 31])
        month_name = MONTHS[month - 1]
        short = month_name[:3]
        two_digit = f"{year % 100:02d}"
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day if day < 20 else day % 10, "th")
        form = rng.randrange(20)
        text = [
            f"{year:04d}-{month:02d}-{day:02d}",
            f"{month:02d}/{day:02d}/{year}",
            f"{month}/{day}/{two_digit}",
            f"{month_name} {day}, {year}",
            f"{day} {month_name} {year}",
            f"{day}{suffix} day of {month_name}, {year}",
            f"{month_name} {year}",
            f"{month_name} {day}{suffix}",
            f"{rng.choice(WEEKDAYS)} {month_name} {day}, {year}",
            f"{month_name} {day}-{min(day + 2, 31)}, {year}",
            f"{day}-{min(day + 2, 31)} {month_name} {year}",
            f"{short}. {day}, {year}",
            f"{day}{suffix} {month_name.lower()}",
            f"the {day}{suffix} day of {month_name}",
            f"{month_name} {day},{year}",
            f"{year}",
            f"{day} {short} {two_digit}",
            f"dated {month_name} {day}, {year} (",
            f"{month_name.upper()} {day}, {year}",
            f"{day}.{month}.{year}",
        ][form]
        generated.append(text)
    return fixed + generated


def money_texts() -> list[Any]:
    """Money strings and hostile values."""
    return [
        "$1,234.56",
        "1234.56",
        "$1,234.57",
        "$1,234.55",
        "1.2M USD",
        "USD 1.2M",
        "$1.2 million",
        "1200000",
        "€500",
        "500 EUR",
        "EUR 500",
        "£3",
        "3 dollars",
        "1k",
        "1K",
        "1000",
        "2B",
        "2,000,000,000",
        "$250,001",
        "$250,000",
        "nan",
        "inf",
        "-inf",
        "",
        "  ",
        "-5",
        "$-5.00",
        "1e3",
        "1_000",
        "N/A",
        "$",
        "M",
        "USD",
        "US 12",
        "12 USD",
        "twelve dollars",
        "Twelve Thousand Dollars ($12,000)",
        "$12,000 plus expenses",
        "0",
        "0.00",
        ".5",
        "5.",
        "１２３",
        "12.345.678",
        "(1,000)",
        None,
        0,
        12,
        12.0,
        12.004,
        1e300,
        float("nan"),
        float("inf"),
        True,
        False,
        [],
        ["$5"],
        {"amount": 5},
    ]


NAME_BASES = [
    "Acme Corp",
    "ACME Corporation",
    "Acme, Inc.",
    "Acme Incorporated",
    "acme",
    "Global Technologies, Ltd",
    "Global Technologies Ltd",
    "Global Tech",
    "Société Générale S.A.",
    "Societe Generale",
    "Smith & Jones LLP",
    "Smith and Jones",
    "John A. Smith, Esq.",
    "John Smith",
    "J. Smith",
    "Jane Doe",
    "Doe, Jane",
    "The Trust Company",
    "Holdings Group PLC",
    "Inc.",
    "LLC",
    "",
    "   ",
    "!!!",
    "1234",
    "12-34",
    "Ab\ud800c Corp",
    "Ab\ud800c",
    "株式会社トヨタ",
    "Toyota",
    "Bob 🙂 Ltd",
    "Bob Ltd",
    "O'Neil",
    "ONeil",
    "Delaware",
    "the laws of the State of Delaware",
    "State of Delaware law",
    "New York",
    "New York, New York",
    None,
    0,
    1,
    1.5,
    float("nan"),
    True,
    False,
    [],
    ["Acme"],
    {"name": "Acme"},
]

FREE_TEXT_BASES = [
    "The term is three years from the Effective Date.",
    "three years",
    "3 years",
    "This Agreement shall be governed by the laws of the State of Delaware.",
    "Delaware",
    "renews automatically for successive one-year terms",
    "automatic renewal for one year",
    "payment within thirty (30) days of invoice",
    "net 30",
    "",
    " ",
    "the of and",
    "N/A",
    "none",
    "Ab\ud800c",
    "naïve café",
    "naive cafe",
    None,
    0,
    12.5,
    [],
    {"a": 1},
]

CMS_REFS = [
    "provider NPI 5040521237",
    "NPI: 5040521237",
    "npi 5040521238",
    "5040521237",
    "NDC 00703051002",
    "NDC 703051002",
    "ndc 0703-0510-02",
    "NDC 70305-102-1",
    "NDC 12345-6789-1",
    "00703051002",
    "NDC 0703-0510-2",
    "facility provider 360029",
    "Facility Provider ID: 36A029",
    "facility provider number 360029",
    "facility provider",
    "HCPCS detail (1 codes)",
    "HCPCS detail (2 codes)",
    "ICD-9 diagnosis codes",
    "diagnosis code",
    "",
    None,
    0,
    [],
    "NDC",
    "NPI 12345",
    "provider 5040521237 and NDC 00703051002",
]

HEURISTIC_NAMES = [
    "effective_date",
    "date_of_loss",
    "UPDATE_DATE",
    "contract_value",
    "total",
    "fee_schedule",
    "claim_number",
    "policy_id",
    "docket",
    "reference",
    "filing",
    "valid_id",
    "name",
    "party",
    "consideration",
    "valuation_date",
    "",
    "Price",
    "identity",
    "salary_band",
    "cost_basis_id",
]


def label_surfaces(rng: random.Random, tokens: list[str]) -> list[Any]:
    """Written forms of labels: tokens, case and spacing variants, prefixes, garbage and hostile values."""
    surfaces: list[Any] = [
        None,
        "",
        " ",
        0,
        1,
        False,
        True,
        1.5,
        float("nan"),
        [],
        ["notice"],
        {"a": 1},
        "other",
        "OTHER",
        "Other / Unspecified",
        "unknown",
        "n/a",
        "x",
        "123",
        "10k",
        "Ab\ud800c",
        "ÉMAIL",
        "e-mail",
        "E MAIL",
    ]
    for token in tokens:
        spaced = token.replace("_", " ")
        surfaces += [
            token,
            token.upper(),
            spaced,
            spaced.title(),
            f"  {token}  ",
            token.replace("_", "-"),
            f"{token} letter",
            f"the {spaced}",
            f"{token}s",
            token[: max(1, len(token) // 2)],
            f"{spaced} agreement",
            f"{spaced} agreements",
            token.replace("_", ""),
            f"{token}_v2",
        ]
    for _ in range(12):
        first, second = rng.choice(tokens), rng.choice(tokens)
        surfaces.append(f"{first} {second}")
        surfaces.append(f"{second}{first}")
    return surfaces


def generated_cases(rng: random.Random, old: dict[str, Any]) -> list[dict[str, Any]]:
    """Build every generated case as ``{"function", "args"}``."""
    cases: list[dict[str, Any]] = []

    def add(function: str, **arguments: Any) -> None:
        """Append a case, skipping arguments the format cannot hold."""
        with contextlib.suppress(Unencodable):
            cases.append({"function": function, "args": encode(arguments)})

    dates = date_texts(rng, 260)
    for text in dates:
        add("parse_date_parts", text=text)
        add("parse_date", text=text)
        add("stated_years", text=text)
        add("integral_float_as_text", value=text)
        if isinstance(text, str):
            add("expected_date_is_null", exp=text)
            add("expected_date_is_relative", exp=text)
            add("date_is_blank_or_bare_label", exp=text)
    string_dates = [text for text in dates if isinstance(text, str)]
    for _ in range(900):
        predicted, expected = rng.choice(dates), rng.choice(dates)
        add("score_date_field", pred=predicted, exp=expected)
        add("date_match_similarity", pred=predicted, exp=expected)
        add("date_year_unverified", pred=predicted, exp=expected)
    for _ in range(300):
        add("date_phrase_contained", container=rng.choice(string_dates), target=rng.choice(string_dates))
    for index in range(0, len(string_dates) - 1):
        add("score_date_field", pred=string_dates[index], exp=string_dates[index + 1])

    money = money_texts()
    for text in money:
        add("parse_money", text=text)
    for predicted in money:
        for expected in rng.sample(money, 8):
            add("score_money_field", pred=predicted, exp=expected)

    for value in NAME_BASES + FREE_TEXT_BASES:
        for function in ("normalize_text", "tokenize", "containment_tokens"):
            add(function, text=value)
    for _ in range(700):
        predicted, expected = rng.choice(NAME_BASES), rng.choice(NAME_BASES)
        for function in ("score_name_field", "score_id_field", "score_label_field"):
            add(function, pred=predicted, exp=expected)
    for _ in range(500):
        predicted, expected = rng.choice(FREE_TEXT_BASES + NAME_BASES), rng.choice(FREE_TEXT_BASES)
        add("score_free_text_field", pred=predicted, exp=expected)
        add("score_containment_field", pred=predicted, exp=expected)
    strings = [value for value in NAME_BASES + FREE_TEXT_BASES if isinstance(value, str)]
    for _ in range(300):
        first, second = rng.choice(strings), rng.choice(strings)
        add("sequence_ratio", a=first, b=second)
        add("token_set_ratio", a=first, b=second)
        add("token_f1", pred=first, exp=second)
        add("jaro_winkler", a=first, b=second)

    for predicted in CMS_REFS:
        add("cms_document_signature", text=predicted)
        for expected in CMS_REFS:
            add("score_cms_doc_ref", pred=predicted, exp=expected)

    for name in HEURISTIC_NAMES:
        for value in ("x", ["x"], [], None, 3):
            add("heuristic_field_type", field_name=name, value=value)
    for field_type in ("entity_list", "entity_list:name", "entity_list:", "entity_listx", "name", "", "list"):
        add("is_entity_list", field_type=field_type)

    hostile = [
        "",
        " ",
        "[]",
        "{}",
        "[ ]",
        '["Bylaws"]',
        '{"a": 1}',
        "[1, 2",
        "null",
        "None",
        "N/A",
        "n.a.",
        "nan",
        "NaN",
        "  NULL ",
        "41",
        "true",
        '"quoted"',
        "[null]",
        '["  "]',
        "{}x",
        None,
        0,
        0.0,
        False,
        True,
        float("nan"),
        float("inf"),
        [],
        {},
        (),
        set(),
        [None],
        [""],
        {"a": None},
        ("x",),
        "Ab\ud800c",
        " ",
        "[ ]",
    ]
    for value in hostile:
        add("is_empty_value", value=value)
        add("parse_json_container", value=value)

    enum_fields = old["SCORED_ENUM_FIELDS"]
    class_keys = list(old["DOC_TYPE_SUBCLASSES"]) + ["", "unknown_class", " Insurance_Claim "]
    for doc_type in class_keys:
        fields = sorted(enum_fields.get(doc_type.strip().lower(), {})) + ["intent", "not_a_field"]
        for field_name in fields:
            vocabulary = list(enum_fields.get(doc_type.strip().lower(), {}).get(field_name, ([],))[0])
            tokens = vocabulary or ["notice", "claim_filing", "effect_merger"]
            pool = label_surfaces(rng, tokens) + list(old["INTENT_ALIASES"]) + list(old["COVERAGE_ALIASES"])
            if not vocabulary:
                pool = rng.sample(pool, 40)
            for value in pool:
                add("canonical_label", doc_type=doc_type, field=field_name, value=value)
    for doc_type in list(old["INTENT_LABELS"]) + ["contract", "", None, "unknown_class"]:
        tokens = list(old["INTENT_LABELS"].get(doc_type, ("notice",)))
        for value in (
            label_surfaces(rng, tokens)
            + list(old["INTENT_ALIASES"])
            + ["Amendment No. 1 to Merger", "amend", "AMENDED"]
        ):
            add("normalize_intent", doc_type=doc_type, value=value)
    contract_surfaces = (
        label_surfaces(rng, list(old["DOC_TYPE_SUBCLASSES"]["contract"]))
        + list(old["RAW_SUBCLASS_SURFACES"]["contract"])
        + list(old["SUBTYPE_ALIASES"])
        + [entry["label"] for entry in old["CONTRACT_SUBTYPES"]]
    )
    for value in contract_surfaces:
        add("normalize_subtype", value=value)
    for doc_type, allowed in old["DOC_TYPE_SUBCLASSES"].items():
        surfaces = label_surfaces(rng, list(allowed) or ["other"]) + list(
            old["RAW_SUBCLASS_SURFACES"].get(doc_type, ())
        )
        for value in surfaces:
            if allowed:
                add("normalize_doc_subclass", value=value, allowed=set(allowed))
            add("normalize_corpus_subclass", doc_type=doc_type, value=value)
    for doc_type in (None, "", "unknown_class", "Contract", "contract "):
        for value in ("License_Agreements", "email", None, "other"):
            add("normalize_corpus_subclass", doc_type=doc_type, value=value)

    long_name = " ".join(f"Party{index} Holdings" for index in range(250))
    long_text = "the term of this agreement " * 100
    long_date = "dated " + "the day after " * 150 + "August 18, 2014"
    long_inputs = {
        "score_name_field": (long_name, long_name[:-9]),
        "score_id_field": (long_name, long_name),
        "score_label_field": (long_text, long_text),
        "score_free_text_field": (long_text, long_text[5:]),
        "score_containment_field": (long_text, "term agreement"),
        "score_money_field": (long_text, "$5"),
        "score_date_field": (long_date, "August 18, 2014"),
        "score_cms_doc_ref": (long_text, "NPI 5040521237"),
    }
    for function, (predicted, expected) in long_inputs.items():
        add(function, pred=predicted, exp=expected)
    add("parse_date_parts", text=long_date)
    add("parse_money", text="9" * 400)
    add("canonical_label", doc_type="correspondence", field="intent", value="payment demand " * 200)

    similarity_specs = [{"kind": "constant", "value": value} for value in (0.0, 0.3, 0.69, 0.7, 0.95, 1.0)]
    similarity_specs += [{"kind": "none"}, {"kind": "raises"}, {"kind": "nan"}]
    rescue_pairs = [
        ("Acme Corp", "Widget Industries"),
        ("John Smith", "J. Smith"),
        ("three years", "36 months"),
        ("Delaware", "the State of Delaware"),
        ("", "Acme"),
        ("Acme", " "),
        ("12 apples", "$12"),
        ("August 2014", "the eighteenth of August"),
        ("FY2014", "August 18, 2014"),
        ("cash", "money"),
    ]
    for spec in similarity_specs:
        for predicted, expected in rescue_pairs:
            for function in (
                "score_name_field",
                "score_free_text_field",
                "score_money_field",
                "score_date_field",
                "score_id_field",
                "score_containment_field",
            ):
                add(function, pred=predicted, exp=expected, embedding={"$similarity": spec})
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


def old_functions() -> dict[str, Any]:
    """Map each case function name to the old callable (fresh import, default settings)."""
    from llm_dojo_scoring.config import clear_settings_cache
    from llm_dojo_scoring.schemas import corpus, equivalences, gt_metadata, intents, scored_enums
    from llm_dojo_scoring.scoring import field_scoring

    clear_settings_cache()
    modules = {
        "scoring/field_scoring.py": field_scoring,
        "schemas/gt_metadata.py": gt_metadata,
        "schemas/scored_enums.py": scored_enums,
        "schemas/intents.py": intents,
        "schemas/equivalences.py": equivalences,
        "schemas/corpus.py": corpus,
    }
    functions: dict[str, Any] = {}
    for (module_file, old_name), new_name in TARGETS.items():
        functions.setdefault(new_name, getattr(modules[module_file], old_name))
    return functions


def old_tables() -> dict[str, Any]:
    """The old vocabularies the generator draws surfaces from."""
    from llm_dojo_scoring import config
    from llm_dojo_scoring.schemas import corpus, intents, scored_enums

    return {
        "SCORED_ENUM_FIELDS": {
            kind: dict(fields) for kind, fields in scored_enums.SCORED_ENUM_FIELDS.items()
        },
        "INTENT_LABELS": dict(intents.INTENT_LABELS),
        "INTENT_ALIASES": dict(intents.INTENT_ALIASES),
        "COVERAGE_ALIASES": dict(scored_enums._COVERAGE_ALIASES),
        "DOC_TYPE_SUBCLASSES": dict(corpus.DOC_TYPE_SUBCLASSES),
        "RAW_SUBCLASS_SURFACES": dict(corpus.RAW_CORPUS_SUBCLASS_SURFACES),
        "SUBTYPE_ALIASES": dict(config.SUBTYPE_ALIASES),
        "CONTRACT_SUBTYPES": list(config.CONTRACT_SUBTYPES),
    }


def run_case(functions: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Call the old function and return the encoded outcome."""
    arguments = decode(case["args"])
    if isinstance(arguments.get("embedding"), dict) and "$similarity" in arguments["embedding"]:
        arguments["embedding"] = FakeSimilarity(arguments["embedding"]["$similarity"])
    try:
        result = functions[case["function"]](**arguments)
    except Exception as error:  # the exception type is the recorded outcome
        return {"raises": type(error).__name__}
    return {"result": encode(result)}


def main() -> int:
    """Capture, generate, compute and write the corpus; print a digest."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--old-dojo", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--collect-test-inputs", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    old_dojo = options.old_dojo.resolve()
    sys.path.insert(0, str(old_dojo))
    if options.collect_test_inputs:
        collect_test_inputs(old_dojo, options.collect_test_inputs)
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

    environment = {key: value for key, value in os.environ.items() if key != "LLM_DOJO_SCORING_CONFIG"}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory() as scratch:
        captured_path = Path(scratch) / "captured.json"
        subprocess.run(
            [
                sys.executable,
                __file__,
                "--old-dojo",
                str(old_dojo),
                "--collect-test-inputs",
                str(captured_path),
            ],
            check=True,
            cwd=scratch,
            env=environment,
            stdout=subprocess.DEVNULL,
        )
        captured = json.loads(captured_path.read_text())
    if captured["exit_code"] != 0:
        print(f"old tests exited {captured['exit_code']}; refusing to freeze", file=sys.stderr)
        return 1

    os.environ.pop("LLM_DOJO_SCORING_CONFIG", None)
    import llm_dojo_scoring

    functions = old_functions()
    rng = random.Random(options.seed)
    sources = [("old_tests", record) for record in captured["records"]]
    sources += [("generated", case) for case in generated_cases(rng, old_tables())]

    seen: set[str] = set()
    lines: list[str] = []
    counts: dict[str, int] = {}
    for source, case in sources:
        key = json.dumps([case["function"], case["args"]], sort_keys=True, ensure_ascii=True)
        if key in seen:
            continue
        seen.add(key)
        outcome = run_case(functions, case)
        case_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        lines.append(
            json.dumps(
                {
                    "id": case_id,
                    "source": source,
                    "function": case["function"],
                    "args": case["args"],
                    "outcome": outcome,
                },
                sort_keys=True,
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        counts[case["function"]] = counts.get(case["function"], 0) + 1

    header = {
        "header": {
            "description": (
                "llm-dojo-scoring outputs for the scalar scoring layers (differential parity corpus)"
            ),
            "old_commit": commit,
            "old_version": llm_dojo_scoring.__version__,
            "seed": options.seed,
            "captured_test_files": list(OLD_TEST_FILES),
            "captured_calls": len(captured["records"]),
            "captured_skipped": captured["skipped"],
            "cases": len(lines),
            "cases_by_function": dict(sorted(counts.items())),
            "dependencies": {
                name: importlib.metadata.version(name)
                for name in ("python-dateutil", "jellyfish", "scipy", "numpy", "PyYAML")
            },
            "python": sys.version.split()[0],
            "script": "packages/dojo/scripts/capture_old_dojo_scalar_cases.py",
        }
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join([json.dumps(header, sort_keys=True, ensure_ascii=True)] + lines) + "\n")
    print(f"old commit {commit}, version {llm_dojo_scoring.__version__}")
    print(f"captured {len(captured['records'])} test calls; wrote {len(lines)} unique cases to {output}")
    for name, count in sorted(counts.items()):
        print(f"  {count:6d} {name}")
    raises = sum(1 for line in lines if '"raises"' in line)
    print(f"cases whose old outcome is an exception: {raises}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
