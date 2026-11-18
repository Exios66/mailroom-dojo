"""Freeze the old Dojo's statistics outputs (bootstrap and calibration) as a differential corpus.

Writes ``packages/dojo/tests/fixtures/differential/statistics_cases.jsonl``: a header
line (old commit, old version, seed, counts, dependency versions), then one case per
line ``{"id", "source", "function", "args", "outcome"}``. ``outcome`` is
``{"result": ...}``, ``{"raises": "<ExceptionType>", "message": "..."}`` or
``{"unencodable": "<type>"}`` (a result tagged JSON cannot hold, such as a complex
number). The new package replays every case in
``tests/test_differential_statistics_parity.py``.

Inputs come from three places:

1. Every call to a public function of the old ``analysis/bootstrap.py`` and
   ``analysis/calibration.py`` made while the old tests that reach them run
   (``sys.setprofile`` in a child process), whoever the caller is.
2. Every such call made while the old Dojo regenerates its golden baseline
   (``scripts/freeze_goldens.py``); the capture refuses to write unless the
   regenerated baseline equals the committed ``scores.json`` byte for byte.
3. Seeded generated inputs: score lists of many sizes and kinds, rates and counts at
   and past their bounds, tied and untied confidences, invalid parameters, NaN,
   infinities, booleans, numeric strings and junk.

Every output is computed afterwards in this process, so it is the old behaviour itself.

Run it only with an interpreter that can import the old Dojo, never in CI::

    PYTHONDONTWRITEBYTECODE=1 <old-venv>/bin/python \\
        packages/dojo/scripts/capture_old_dojo_statistics_cases.py \\
        --old-dojo <llm-dojo-scoring checkout at 23f8234c> \\
        --output packages/dojo/tests/fixtures/differential/statistics_cases.jsonl

It refuses to run unless the checkout is at the pinned commit and ``llm_dojo_scoring``
imports from inside it (not from another install). It reads the old checkout and writes
nothing inside it (pytest runs from a temporary
directory with its cache plugin off, and no bytecode is written). The old commit is read
from the checkout's ``.git`` files, without running git.

``--supplement`` adds the generated cases a committed corpus lacks (after new inputs were
appended to the generator) without re-running the old tests or golden run: it loads the
old ``analysis/bootstrap.py`` and ``analysis/calibration.py`` by file path (they need
only the standard library, NumPy and SciPy), computes each missing case's old outcome,
appends it, and records the run in the header's ``supplements`` list::

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \\
        packages/dojo/scripts/capture_old_dojo_statistics_cases.py --supplement \\
        --old-dojo <llm-dojo-scoring checkout at 23f8234c> \\
        --output packages/dojo/tests/fixtures/differential/statistics_cases.jsonl
"""

from __future__ import annotations

import argparse
import copy
import decimal
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

EXPECTED_OLD_COMMIT = "23f8234c3c548bdc203033388bb316e52e588a2c"
DEFAULT_SEED = 20261118
SCRIPT_DIRECTORY = Path(__file__).resolve().parent

#: Old test files whose calls into the targets are captured.
OLD_TEST_FILES = (
    "tests/test_bootstrap.py",
    "tests/test_calibration.py",
    "tests/test_reviewer_scoring.py",
    "tests/test_pr104_review_fixes.py",
    "tests/test_review_fixes_sandbox_238.py",
    "tests/test_v030_followups.py",
    "tests/behavior/test_ml_contract.py",
    "tests/test_core_engine_transfer.py",
    "tests/test_serving.py",
    "tests/test_tasks.py",
    "tests/test_suites.py",
)

#: Old module file -> public functions captured (the new names are the same).
TARGETS: dict[str, tuple[str, ...]] = {
    "analysis/bootstrap.py": (
        "bootstrap_ci",
        "delta_significance",
        "wilson_bounds",
        "wilson_ci",
        "round_up",
        "round_down",
    ),
    "analysis/calibration.py": (
        "clopper_pearson_upper",
        "required_samples",
        "cutoff_at_target",
        "cross_fit_cutoffs",
        "expected_calibration_error",
        "brier_score",
        "reliability_bins",
        "auroc",
        "spearman",
        "cutoff_for_precision",
    ),
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


def plain(value: Any) -> Any:
    """Turn result objects into plain data (anything with ``to_dict``, also inside lists)."""
    if isinstance(value, list):
        return [plain(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict) and not isinstance(value, (dict, tuple, set, frozenset, str, bytes)):
        return to_dict()
    return value


def read_commit(checkout: Path) -> str:
    """The checked-out commit, read from ``.git`` (loose ref or packed-refs), without running git."""
    git = checkout / ".git"
    if git.is_file():  # a worktree: "gitdir: <path>"
        git = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
    head = (git / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        return head
    ref = head[5:]
    common = git
    if (git / "commondir").is_file():
        common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
    for base in (git, common):
        if (base / ref).is_file():
            return (base / ref).read_text(encoding="utf-8").strip()
    packed = common / "packed-refs"
    for line in packed.read_text(encoding="utf-8").splitlines() if packed.is_file() else []:
        if line.endswith(" " + ref):
            return line.split(" ", 1)[0]
    raise SystemExit(f"cannot resolve {ref} in {checkout}")


def import_old_dojo(old_dojo: Path) -> ModuleType:
    """Import ``llm_dojo_scoring`` and require that it comes from inside ``old_dojo``."""
    module = sys.modules.get("llm_dojo_scoring") or importlib.import_module("llm_dojo_scoring")
    location = Path(module.__file__).resolve()
    if old_dojo.resolve() not in location.parents:
        raise RuntimeError(
            f"llm_dojo_scoring was imported from {location}, not from {old_dojo}; refusing to capture"
        )
    return module


# ---------------------------------------------------------------------------
# Child phases: capture calls while the old tests or the golden run execute
# ---------------------------------------------------------------------------


def call_recorder(old_dojo: Path) -> tuple[Any, list[dict[str, Any]], dict[str, int]]:
    """A ``sys.setprofile`` hook recording each targeted call's arguments, copied at call time."""
    package_root = str(old_dojo / "llm_dojo_scoring") + os.sep
    targets = {(module, name) for module, names in TARGETS.items() for name in names}
    records: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    def profile(frame, event, arg):  # noqa: ANN001 - sys.setprofile signature
        """Record the arguments of a targeted function at call time."""
        if event != "call":
            return
        filename = frame.f_code.co_filename
        if not filename.startswith(package_root):
            return
        name = frame.f_code.co_name
        if (filename[len(package_root) :], name) not in targets:
            return
        code = frame.f_code
        arguments = {
            variable: frame.f_locals.get(variable)
            for variable in code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
        }
        try:
            records.append({"function": name, "args": encode(copy.deepcopy(arguments))})
        except Unencodable as error:
            skipped[f"{name}:{error}"] = skipped.get(f"{name}:{error}", 0) + 1

    return profile, records, skipped


def collect_test_calls(old_dojo: Path, output: Path) -> None:
    """Run the old tests under the hook and write every targeted call."""
    import pytest

    import_old_dojo(old_dojo)
    profile, records, skipped = call_recorder(old_dojo)
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
    """Regenerate the old golden baseline under the hook and write every targeted call."""
    import_old_dojo(old_dojo)
    spec = importlib.util.spec_from_file_location(
        "freeze_goldens", old_dojo / "scripts" / "freeze_goldens.py"
    )
    goldens = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(goldens)
    profile, records, skipped = call_recorder(old_dojo)
    inputs = goldens.load_inputs()
    sys.setprofile(profile)
    try:
        payload = goldens.collect_goldens(inputs)
    finally:
        sys.setprofile(None)
    identical = goldens.render_goldens(payload) == goldens.SCORES_PATH.read_text(encoding="utf-8")
    output.write_text(
        json.dumps({"records": records, "skipped": skipped, "regenerated_identical": identical})
    )


# ---------------------------------------------------------------------------
# Generated inputs
# ---------------------------------------------------------------------------

NAN = float("nan")
INF = float("inf")


def score_lists(rng: random.Random) -> list[list[Any]]:
    """Per-document score lists: binary, continuous, constant, tiny, mixed and hostile."""
    lists: list[list[Any]] = [[], [None], [1.0], [0.5, None], [None, None, None]]
    for size in (2, 3, 4, 5, 8, 13, 30, 75, 150):
        lists.append([float(rng.random() < 0.7) for _ in range(size)])
        lists.append([round(rng.random(), 4) for _ in range(size)])
        lists.append([rng.random() < 0.4 for _ in range(size)])
    for size in (2, 3, 10):
        lists.append([0.75] * size)
        lists.append([rng.choice([0, 1, 1.0, True, False, None, "0.5", "x", 2]) for _ in range(size)])
    lists += [
        [1.0, NAN, 0.0],
        [NAN, NAN],
        [1.0, INF, 0.0],
        [-INF, 0.5, 0.5],
        [0.1, "nan", 0.9],
        ["1", "0", " 0.5 ", "abc", {}, [1]],
        [10**400, 1.0],
        [-3.0, 7.5, 1e300, 1e300],
        [1, 2, 3, 4],
    ]
    return lists


def generated_cases(rng: random.Random) -> list[dict[str, Any]]:
    """Seeded inputs for every target; arguments are keyword dicts, encoded as tagged JSON."""
    cases: list[dict[str, Any]] = []

    def add(function: str, **arguments: Any) -> None:
        cases.append({"function": function, "args": encode(arguments)})

    lists = score_lists(rng)
    for values in lists:
        add("bootstrap_ci", values=values)
        add("bootstrap_ci", values=values, n_boot=50, seed=rng.randrange(10**6))
    for n_boot in (1, 2, 3, 10, 39, 40, 41, 200):
        for alpha in (0.05, 0.1, 0.01, 0.5):
            add("bootstrap_ci", values=rng.choice(lists[5:30]), n_boot=n_boot, alpha=alpha, seed=7)
    for n_boot, alpha in ((0, 0.05), (-5, 0.05), (100, 0.0), (100, 1.0), (100, -0.2), (100, 1.5), (100, NAN)):
        add("bootstrap_ci", values=[0.0, 1.0, 1.0], n_boot=n_boot, alpha=alpha)
    for seed in (0, 1, 42, 2**40, -3):
        add("bootstrap_ci", values=[0.0, 1.0, 0.5, 0.25], n_boot=100, seed=seed)
    for _ in range(40):
        first, second = rng.choice(lists), rng.choice(lists)
        add("delta_significance", values_a=first, values_b=second, n_boot=rng.choice([20, 100, 400]))
    add("delta_significance", values_a=[0.5] * 8, values_b=[1.0] * 8)
    add("delta_significance", values_a=[0.0, 1.0], values_b=[0.0, 1.0])
    for n_boot, alpha in ((0, 0.05), (1, 0.05), (100, 0.0), (100, 1.0), (100, 2.0)):
        add(
            "delta_significance",
            values_a=[0.0, 1.0, 1.0],
            values_b=[1.0, 1.0, 0.0],
            n_boot=n_boot,
            alpha=alpha,
        )

    rates = [0.0, 0.001, 0.1, 0.5, 0.9, 0.9999, 1.0, -0.1, 1.1, 3.0, NAN, INF, True, 1, 0]
    counts = [0, 1, 2, 10, 100, 1000, 10**6, 2.5, 10.0, -1, True]
    for p in rates:
        for n in counts:
            add("wilson_ci", p=p, n=n)
            if not (isinstance(n, int) and n <= 0):
                add("wilson_bounds", p=p, n=n)
    for z in (1.96, 0, 0.0, 2, 2.575829, 1.0, -1.0, NAN, INF):
        for p, n in ((0.9, 100), (0.0, 5), (1.0, 5), (0.5, 1)):
            add("wilson_ci", p=p, n=n, z=z)
            add("wilson_bounds", p=p, n=n, z=z)
    add("wilson_ci", p=None, n=10)
    add("wilson_bounds", p=0.5, n=0)
    for _ in range(60):
        add(
            "wilson_ci",
            p=round(rng.random(), 6),
            n=rng.randrange(1, 5000),
            z=rng.choice([1.96, 1.645, 2.575829]),
        )

    floats = [0.0, 1.0, -1.0, 0.00010000000000000002, 0.12345, 0.99995, 1e-12, 123.456789, NAN, INF, -INF]
    floats += [rng.uniform(-2, 2) for _ in range(30)]
    for value in floats:
        for places in (0, 1, 2, 4, 6):
            add("round_up", value=value, places=places)
            add("round_down", value=value, places=places)

    for errors in (0, 1, 2, 3, 5, 80, 250):
        for n in (0, 1, 5, 135, 149, 299, 405, 473, 3000, 10**6):
            add("clopper_pearson_upper", errors=errors, n=n)
    for confidence in (0.5, 0.9, 0.99, 0.999999, 0.0, 1.0, -0.1, NAN):
        add("clopper_pearson_upper", errors=3, n=100, confidence=confidence)
    for errors, n in ((-1, 10), (11, 10), (True, 10), (1.0, 10), (1, 10.5), ("1", 10), (5, 5)):
        add("clopper_pearson_upper", errors=errors, n=n)
    for errors in (0, 1, 2, 3, 10, 80, 2_000_000, -1, True):
        for risk in (0.01, 0.02, 0.05, 0.5, 1e-300, 0.0, 1.0):
            add("required_samples", errors=errors, risk=risk)
    for confidence in (0.5, 0.99, 0.0, 1.0, -0.5, NAN):
        add("required_samples", errors=0, confidence=confidence)
        add("required_samples", errors=2, confidence=confidence)

    levels = [0.0, 0.1, 0.5, 0.5, 0.7, 0.8, 0.9, 0.95, 1.0]
    row_sets: list[list[Any]] = [[], [(0.9, True)], [(0.9, False)], [(0.0, True), (0.0, True)]]
    for size in (3, 10, 40, 120, 300):
        row_sets.append([(rng.choice(levels), rng.random() < 0.8) for _ in range(size)])
        row_sets.append([(round(rng.random(), 3), rng.random() < 0.6) for _ in range(size)])
    row_sets.append([(0.9, True)] * 100 + [(0.5, False)] * 20)
    row_sets.append([(0.9, 1), (0.8, 0), (0.7, "yes"), (0.6, None)])
    row_sets.append([(0.9, True), (NAN, False), (0.5, True)])
    for rows in row_sets:
        for target in (0.5, 0.8, 0.95, 1.0):
            add("cutoff_at_target", rows=rows, target=target)
    for target in (0.0, -0.1, 1.01, NAN):
        add("cutoff_at_target", rows=row_sets[5], target=target)
    for rows in row_sets:
        add("cross_fit_cutoffs", rows=rows)
        add(
            "cross_fit_cutoffs",
            rows=rows,
            target=rng.choice([0.8, 0.9, 0.95]),
            folds=rng.choice([2, 3, 5, 10]),
            seed=rng.randrange(1000),
            confidence=rng.choice([0.9, 0.95, 0.99]),
        )
    for folds, confidence in ((1, 0.95), (0, 0.95), (5, 1.5), (5, 0.0), (5, 1.0)):
        add("cross_fit_cutoffs", rows=row_sets[6], folds=folds, confidence=confidence)

    pair_sets: list[list[Any]] = [
        [],
        [(1.0, True), (1.0, True), (0.0, False), (0.0, False)],
        [(1.0, False), (0.0, True)],
        [(1.0, True), (0.49, False)],
        [(0.5, True), (0.5, False)],
        [(NAN, True), (INF, False), (-0.1, True), (1.1, False), (True, True), ("0.5", False), (None, True)],
        [(0.3, 1), (0.7, 0), (0.9, "x"), (0.2, None), (1, True), (0, False)],
        [(0.1, True), (0.2, True), (0.3, True)],
    ]
    for size in (5, 20, 100, 400):
        pair_sets.append([(round(rng.random(), 4), rng.random() < 0.7) for _ in range(size)])
        pair_sets.append([(rng.choice(levels), rng.random() < 0.5) for _ in range(size)])
    for pairs in pair_sets:
        add("brier_score", pairs=pairs)
        for bins in (1, 2, 10, 15):
            add("expected_calibration_error", pairs=pairs, bins=bins)
            add("reliability_bins", pairs=pairs, bins=bins)
    for bins in (0, -1):
        add("expected_calibration_error", pairs=pair_sets[1], bins=bins)
        add("reliability_bins", pairs=pair_sets[1], bins=bins)

    score_sets: list[tuple[list[Any], list[Any]]] = [
        ([], []),
        ([0.9, 0.8, 0.2], [True, True, False]),
        ([0.5, 0.5], [True, False]),
        ([0.5], [True]),
        ([0.1, 0.2], [True, True]),
        ([0.3, NAN, 0.7, 0.2], [False, True, True, False]),
        ([True, 0.4, 0.6, 1], [False, False, True, True]),
        ([10**400, 1, 2], [True, False, False]),
        ([0.1, 0.2, 0.3], [1, 0, "yes"]),
        ([0.1, 0.2], [True]),
    ]
    for size in (4, 15, 60, 250):
        score_sets.append(
            ([rng.choice(levels) for _ in range(size)], [rng.random() < 0.5 for _ in range(size)])
        )
        score_sets.append(
            ([round(rng.random(), 5) for _ in range(size)], [rng.random() < 0.3 for _ in range(size)])
        )
    for scores, labels in score_sets:
        add("auroc", scores=scores, labels=labels)
        for precision in (0.5, 0.9, 1.0):
            add("cutoff_for_precision", scores=scores, labels=labels, precision=precision)
    for precision in (0.0, 1.5, -1.0, NAN):
        add("cutoff_for_precision", scores=[0.9, 0.1], labels=[True, False], precision=precision)

    axis_sets: list[tuple[list[Any], list[Any]]] = [
        ([1, 2, 3], [1, 2, 3]),
        ([1, 1, 1], [1, 2, 3]),
        ([1, 2], [2, 1]),
        ([], []),
        ([1, 2, 3], [3, 2, 1]),
        ([1, NAN, 2, 3, 4], [4, 3, NAN, 1, 0]),
        ([2**53, 2**53 + 1, 2**53 + 2], [1, 2, 3]),
        ([1, 2, 3], [1, 2]),
        ([0.1, 0.1, 0.2, 0.3], [5, 5, 5, 6]),
    ]
    for size in (3, 5, 20, 100):
        axis_sets.append(
            ([rng.choice(levels) for _ in range(size)], [round(rng.random(), 3) for _ in range(size)])
        )
        axis_sets.append(([rng.randrange(5) for _ in range(size)], [rng.randrange(5) for _ in range(size)]))
    for xs, ys in axis_sets:
        add("spearman", xs=xs, ys=ys)

    # Inputs no earlier source fed (adversarial review, round 1): valid numerics the old
    # Dojo accepted (a Decimal rate, an integer-valued float support), the order of the
    # "fewer than two values" step and the parameter checks, a text rate, a text seed.
    # They draw nothing from ``rng``, so every case above is unchanged.
    add("wilson_ci", p=decimal.Decimal("0.5"), n=10)
    add("wilson_ci", p=decimal.Decimal("0.25"), n=40, z=1.645)
    add("wilson_bounds", p=decimal.Decimal("0.5"), n=10)
    add("wilson_ci", p="0.5", n=10)
    for n in (10.0, 2.5, 0.0, True):
        add("wilson_ci", p=None, n=n)
    for n in (1.0, 0.0, -1.0, 1e300):
        add("wilson_ci", p=0.5, n=n)
    for n in (1.0, 1e300):
        add("wilson_bounds", p=0.5, n=n)
    add("bootstrap_ci", values=[0.5], n_boot=0)
    add("bootstrap_ci", values=[], alpha=NAN)
    add("bootstrap_ci", values=[0.5, None], seed="abc")
    add("bootstrap_ci", values=[0.0, 1.0, 0.5, 0.25], n_boot=100, seed="abc")
    add("delta_significance", values_a=[0.5], values_b=[0.1, 0.2], alpha=NAN)
    add("delta_significance", values_a=[0.1, 0.2], values_b=[], n_boot=-1)
    add("delta_significance", values_a=[0.0, 1.0, 1.0], values_b=[1.0, 1.0, 0.0], n_boot=100, seed="abc")
    return cases


# ---------------------------------------------------------------------------
# Old outputs
# ---------------------------------------------------------------------------


def old_functions() -> dict[str, Any]:
    """Map each target name to the old callable."""
    from llm_dojo_scoring.analysis import bootstrap, calibration

    modules = {"analysis/bootstrap.py": bootstrap, "analysis/calibration.py": calibration}
    return {name: getattr(modules[module], name) for module, names in TARGETS.items() for name in names}


def old_functions_from_files(old_dojo: Path) -> dict[str, Any]:
    """Map each target name to the old callable, loading the two old modules by file path.

    The modules are loaded under a private package name, so ``calibration``'s relative
    import of ``bootstrap`` resolves, without importing the rest of the old package.
    """
    package_name = "_old_dojo_analysis"
    directory = old_dojo / "llm_dojo_scoring" / "analysis"
    package = ModuleType(package_name)
    package.__path__ = [str(directory)]
    sys.modules[package_name] = package
    modules: dict[str, ModuleType] = {}
    for relative in TARGETS:
        path = old_dojo / "llm_dojo_scoring" / relative
        name = f"{package_name}.{path.stem}"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        if Path(module.__file__).resolve() != path.resolve():
            raise SystemExit(f"{relative} loaded from {module.__file__}, not from {path}")
        modules[relative] = module
    return {name: getattr(modules[module], name) for module, names in TARGETS.items() for name in names}


def case_key(case: dict[str, Any]) -> str:
    """The deduplication key of a case; its id is the first 16 hex digits of its SHA-256."""
    return json.dumps([case["function"], case["args"]], sort_keys=True, ensure_ascii=True)


def case_line(source: str, case: dict[str, Any], outcome: dict[str, Any]) -> str:
    """One corpus line."""
    return canonical_text(
        {
            "id": hashlib.sha256(case_key(case).encode("utf-8")).hexdigest()[:16],
            "source": source,
            "function": case["function"],
            "args": case["args"],
            "outcome": outcome,
        }
    )


def outcome_kind(outcome: dict[str, Any]) -> str:
    """``result``, ``raises`` or ``unencodable`` (a raised outcome also holds ``message``)."""
    return next(kind for kind in ("result", "raises", "unencodable") if kind in outcome)


def supplement(old_dojo: Path, output: Path, seed: int) -> int:
    """Append the generated cases the committed corpus lacks, with their old outcomes."""
    commit = read_commit(old_dojo)
    if commit != EXPECTED_OLD_COMMIT:
        print(f"old Dojo is at {commit}, expected {EXPECTED_OLD_COMMIT}; refusing to write", file=sys.stderr)
        return 1
    lines = output.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])["header"]
    if header["seed"] != seed or header["old_commit"] != commit:
        print("the corpus was captured with another seed or old commit; refusing to write", file=sys.stderr)
        return 1
    cases = [json.loads(line) for line in lines[1:]]
    seen = {case["id"] for case in cases}
    functions = old_functions_from_files(old_dojo)
    added: list[str] = []
    for case in generated_cases(random.Random(seed)):
        case_id = hashlib.sha256(case_key(case).encode("utf-8")).hexdigest()[:16]
        if case_id in seen:
            continue
        seen.add(case_id)
        added.append(case_line("generated", case, run_case(functions, case)))
    if not added:
        print("every generated case is already in the corpus; nothing written")
        return 0
    cases += [json.loads(line) for line in added]
    header["cases"] = len(cases)
    header["cases_by_function"] = dict(sorted(Counter(case["function"] for case in cases).items()))
    header["outcome_kinds"] = dict(sorted(Counter(outcome_kind(case["outcome"]) for case in cases).items()))
    header.setdefault("supplements", []).append(
        {
            "cases_added": len(added),
            "method": (
                "--supplement: the old analysis/bootstrap.py and analysis/calibration.py loaded by "
                "file path from the old checkout; old tests and golden run not re-run"
            ),
            "dependencies": {name: importlib.metadata.version(name) for name in ("scipy", "numpy")},
            "python": sys.version.split()[0],
        }
    )
    output.write_text(
        "\n".join([json.dumps({"header": header}, sort_keys=True, ensure_ascii=True)] + lines[1:] + added)
        + "\n"
    )
    print(f"old commit {commit}; appended {len(added)} generated cases to {output} ({len(cases)} in all)")
    print(f"outcomes: {header['outcome_kinds']}")
    return 0


def run_case(functions: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Call the old function on a fresh copy of the arguments and return the encoded outcome."""
    arguments = decode(copy.deepcopy(case["args"]))
    try:
        result = functions[case["function"]](**arguments)
    except Exception as error:  # the exception is the recorded outcome
        return {"raises": type(error).__name__, "message": str(error)}
    try:
        return {"result": encode(plain(result))}
    except Unencodable as error:
        return {"unencodable": str(error)}


def capture_child(old_dojo: Path, flag: str) -> dict[str, Any]:
    """Run one child phase of this script in a temporary directory and return its JSON output."""
    environment = {key: value for key, value in os.environ.items() if key != "LLM_DOJO_SCORING_CONFIG"}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch) / "captured.json"
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
    parser.add_argument(
        "--supplement",
        action="store_true",
        help="append the generated cases the corpus at --output lacks (old modules loaded by path)",
    )
    parser.add_argument("--collect-test-calls", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--collect-golden-calls", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    old_dojo = options.old_dojo.resolve()
    sys.path.insert(0, str(old_dojo))
    if options.collect_test_calls:
        collect_test_calls(old_dojo, options.collect_test_calls)
        return 0
    if options.collect_golden_calls:
        collect_golden_calls(old_dojo, options.collect_golden_calls)
        return 0
    if options.output is None:
        parser.error("--output is required")
    output = options.output.resolve()
    if old_dojo in output.parents:
        parser.error("refusing to write inside the old Dojo checkout")
    if options.supplement:
        return supplement(old_dojo, output, options.seed)
    commit = read_commit(old_dojo)
    if commit != EXPECTED_OLD_COMMIT:
        print(f"old Dojo is at {commit}, expected {EXPECTED_OLD_COMMIT}; refusing to freeze", file=sys.stderr)
        return 1
    os.environ.pop("LLM_DOJO_SCORING_CONFIG", None)
    try:
        llm_dojo_scoring = import_old_dojo(old_dojo)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1

    tests = capture_child(old_dojo, "--collect-test-calls")
    if tests["exit_code"] != 0:
        print(f"old tests exited {tests['exit_code']}; refusing to freeze", file=sys.stderr)
        return 1
    golden = capture_child(old_dojo, "--collect-golden-calls")
    if not golden["regenerated_identical"]:
        print(
            "the old golden run under the hook differs from scores.json; refusing to freeze", file=sys.stderr
        )
        return 1

    functions = old_functions()
    sources = [("golden_run", record) for record in golden["records"]]
    sources += [("old_tests", record) for record in tests["records"]]
    sources += [("generated", case) for case in generated_cases(random.Random(options.seed))]

    seen: set[str] = set()
    lines: list[str] = []
    counts: dict[str, int] = {}
    outcome_kinds: dict[str, int] = {}
    for source, case in sources:
        key = case_key(case)
        if key in seen:
            continue
        seen.add(key)
        outcome = run_case(functions, case)
        kind = outcome_kind(outcome)
        outcome_kinds[kind] = outcome_kinds.get(kind, 0) + 1
        lines.append(case_line(source, case, outcome))
        counts[case["function"]] = counts.get(case["function"], 0) + 1

    header = {
        "header": {
            "description": "llm-dojo-scoring outputs for the statistics layer (differential parity corpus)",
            "old_commit": commit,
            "old_version": llm_dojo_scoring.__version__,
            "seed": options.seed,
            "captured_test_files": list(OLD_TEST_FILES),
            "captured_test_calls": len(tests["records"]),
            "captured_test_skipped": tests["skipped"],
            "captured_golden_calls": len(golden["records"]),
            "captured_golden_skipped": golden["skipped"],
            "cases": len(lines),
            "cases_by_function": dict(sorted(counts.items())),
            "outcome_kinds": dict(sorted(outcome_kinds.items())),
            "dependencies": {name: importlib.metadata.version(name) for name in ("scipy", "numpy")},
            "python": sys.version.split()[0],
            "script": "packages/dojo/scripts/capture_old_dojo_statistics_cases.py",
        }
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join([json.dumps(header, sort_keys=True, ensure_ascii=True)] + lines) + "\n")
    print(f"old commit {commit}, version {llm_dojo_scoring.__version__}")
    print(
        f"captured {len(tests['records'])} test calls and {len(golden['records'])} golden-run calls; "
        f"wrote {len(lines)} unique cases to {output}"
    )
    for name, count in sorted(counts.items()):
        print(f"  {count:6d} {name}")
    print(f"outcomes: {dict(sorted(outcome_kinds.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
