"""Package boundaries of mailroom_dojo: one-way dependencies, no globals, no environment, no network.

The Dojo may import the standard library (minus network, process and thread
modules), its declared third-party dependencies, itself, ``mailroom_record`` and
``mailroom_labels``. It never imports an engine or launcher, nor the old
``llm_dojo_scoring``. The check walks the source with :mod:`ast` (static imports,
relative imports, ``importlib.import_module``/``__import__`` calls), then imports
every module in a fresh interpreter and inspects ``sys.modules``. Each check has a
positive control showing it catches what it looks for.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from dojo_testkit import PACKAGE_ROOT, SOURCE_ROOT

SOURCE_FILES = sorted(SOURCE_ROOT.rglob("*.py"))

#: Third-party distributions' import names the Dojo may use (pyproject dependencies and extras).
THIRD_PARTY = {"dateutil", "jellyfish", "numpy", "scipy", "yaml", "sentence_transformers"}
#: Workspace packages the Dojo may import (record and, once it lands, labels).
WORKSPACE_ALLOWED = {"mailroom_dojo", "mailroom_record", "mailroom_labels"}
#: Standard-library modules that reach the network, spawn processes or threads, or read secrets.
STDLIB_FORBIDDEN = {
    "socket",
    "ssl",
    "urllib",
    "http",
    "ftplib",
    "smtplib",
    "imaplib",
    "poplib",
    "telnetlib",
    "xmlrpc",
    "socketserver",
    "asyncio",
    "threading",
    "multiprocessing",
    "concurrent",
    "subprocess",
    "selectors",
    "webbrowser",
    "getpass",
}
#: Never allowed, whatever else changes: engines, launchers, the old Dojo, network clients, model SDKs.
NAMED_FORBIDDEN = {
    "llm_dojo_scoring",
    "mailroom_engine",
    "mailroom_launcher",
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "openai",
    "anthropic",
    "langfuse",
    "dotenv",
    "torch",
}
CACHE_DECORATORS = {"lru_cache", "cache", "cached_property"}


def _relative(path: Path) -> str:
    """A source path relative to the package root, for messages."""
    return str(path.relative_to(SOURCE_ROOT.parent))


def _dotted(node: ast.AST) -> str:
    """The dotted name of an expression (``os.environ``), or ""."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _import_allowed(name: str) -> bool:
    """True when a top-level import name is allowed for the Dojo."""
    top = name.split(".")[0]
    if top in NAMED_FORBIDDEN or top in STDLIB_FORBIDDEN:
        return False
    if top.startswith("mailroom_"):
        return top in WORKSPACE_ALLOWED
    return top in sys.stdlib_module_names or top in THIRD_PARTY


def boundary_problems(path: Path, package_depth: int) -> list[str]:
    """Every boundary violation in one source file.

    ``package_depth`` is how many package levels the file sits below the top of
    ``mailroom_dojo`` (0 for a module directly in it), so a relative import that
    climbs out of the package is caught.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            problems += [
                f"imports {alias.name} (line {node.lineno})"
                for alias in node.names
                if not _import_allowed(alias.name)
            ]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and not _import_allowed(node.module):
                problems.append(f"imports {node.module} (line {node.lineno})")
            if node.level > package_depth + 1:
                problems.append(f"relative import climbs out of mailroom_dojo (line {node.lineno})")
        elif isinstance(node, ast.Call):
            function = _dotted(node.func)
            if function in {"importlib.import_module", "import_module", "__import__"}:
                target = node.args[0] if node.args else None
                if not isinstance(target, ast.Constant) or not _import_allowed(str(target.value)):
                    problems.append(
                        f"dynamic import of {ast.unparse(target) if target else '?'} (line {node.lineno})"
                    )
            if function in {"os.getenv", "getenv", "os.putenv", "os.environ.get"}:
                problems.append(f"reads the environment via {function} (line {node.lineno})")
        elif isinstance(node, ast.Attribute) and node.attr in {"environ", "environb"}:
            problems.append(f"uses {_dotted(node)} (line {node.lineno})")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"{type(node).__name__.lower()} statement (line {node.lineno})")
        for decorator in getattr(node, "decorator_list", []):
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if _dotted(target).split(".")[-1] in CACHE_DECORATORS:
                problems.append(f"cache decorator {_dotted(target)} (line {node.lineno})")
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            mutable = isinstance(
                node.value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)
            )
            factory = isinstance(node.value, ast.Call) and _dotted(node.value.func) in {
                "dict",
                "list",
                "set",
                "defaultdict",
                "collections.defaultdict",
                "OrderedDict",
                "deque",
                "Counter",
            }
            if names != ["__all__"] and (mutable or factory):
                problems.append(f"module-level mutable {names} (line {node.lineno})")
    return problems


def _depth(path: Path) -> int:
    """Package levels below ``mailroom_dojo`` (a module in it is 0)."""
    return len(path.relative_to(SOURCE_ROOT).parts) - 1


def test_the_source_tree_is_found():
    """Guard against walking nothing: the core modules and the task C packages are present."""
    names = {_relative(path) for path in SOURCE_FILES}
    for expected in (
        "mailroom_dojo/classification/labels.py",
        "mailroom_dojo/labels_interface/__init__.py",
        "mailroom_dojo/profile/loading.py",
        "mailroom_dojo/field_scoring/extraction.py",
        "mailroom_dojo/statistics/bootstrap.py",
        "mailroom_dojo/statistics/calibration.py",
        "mailroom_dojo/statistics/paired.py",
    ):
        assert expected in names
    assert len(SOURCE_FILES) >= 30


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_no_module_crosses_a_boundary(path):
    """No forbidden import (static, relative or dynamic), environment read, global, cache or module-level
    mutable."""
    assert boundary_problems(path, _depth(path)) == []


def test_the_checks_catch_each_violation(tmp_path):
    """A planted module with one of each violation yields one problem per violation."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import os, socket\n"
        "import requests\n"
        "from llm_dojo_scoring.scoring import classification\n"
        "from mailroom_engine.executor import run\n"
        "import mailroom_launcher\n"
        "from urllib.request import urlopen\n"
        "import threading\n"
        "from ... import outside\n"
        "import importlib\n"
        "from functools import lru_cache, cached_property\n"
        "CACHE = {}\n"
        "SEEN = set()\n"
        "engine = importlib.import_module('mailroom_engine')\n"
        "name = 'x'\n"
        "dynamic = __import__(name)\n"
        "@lru_cache(maxsize=None)\n"
        "def f():\n"
        "    global CACHE\n"
        "    return os.environ['KEY'], os.getenv('KEY')\n"
        "import mailroom_record, mailroom_labels, yaml, json\n"
    )
    problems = boundary_problems(bad, package_depth=1)
    joined = "\n".join(problems)
    for expected in (
        "imports socket",
        "imports requests",
        "imports llm_dojo_scoring.scoring",
        "imports mailroom_engine.executor",
        "imports mailroom_launcher",
        "imports urllib.request",
        "imports threading",
        "relative import climbs out",
        "dynamic import of 'mailroom_engine'",
        "dynamic import of name",
        "cache decorator lru_cache",
        "global statement",
        "uses os.environ",
        "reads the environment via os.getenv",
        "module-level mutable ['CACHE']",
        "module-level mutable ['SEEN']",
    ):
        assert expected in joined, expected
    assert not any(word in joined for word in ("mailroom_record", "mailroom_labels", "yaml", "json"))
    assert len(problems) == 16


#: The third-party modules the Dojo's source imports. The runtime test imports these first, so a
#: standard-library module a dependency loads itself is not charged to the Dojo: on Python 3.11,
#: scipy 1.17's ``scipy.special`` imports ``numpy.testing``, whose ``importlib.metadata`` loads
#: ``email.utils`` and with it ``socket``. A direct ``import socket`` in Dojo source is still
#: refused by the source scan above, on every Python.
DEPENDENCY_IMPORTS = ("dateutil.parser", "jellyfish", "numpy", "scipy.optimize", "scipy.special", "yaml")


def test_importing_every_module_loads_nothing_forbidden():
    """In a fresh interpreter, importing every Dojo module loads no engine, old Dojo, network client or model
    library, and adds no network module beyond what its own dependencies load."""
    script = (
        "import importlib, json, pkgutil, sys\n"
        f"for name in {DEPENDENCY_IMPORTS!r}: importlib.import_module(name)\n"
        "baseline = sorted(sys.modules)\n"
        "import mailroom_dojo\n"
        "names = [m.name for m in pkgutil.walk_packages(mailroom_dojo.__path__, 'mailroom_dojo.')]\n"
        "for name in names: importlib.import_module(name)\n"
        "print(json.dumps({'imported': names, 'baseline': baseline, 'loaded': sorted(sys.modules)}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=PACKAGE_ROOT
    )
    import json

    report = json.loads(completed.stdout)
    assert len(report["imported"]) >= 30
    loaded_tops = {name.split(".")[0] for name in report["loaded"]}
    added_tops = loaded_tops - {name.split(".")[0] for name in report["baseline"]}
    assert not (NAMED_FORBIDDEN | {"sentence_transformers"}) & loaded_tops
    assert not {"socket", "ssl", "http"} & added_tops
    assert {name for name in loaded_tops if name.startswith("mailroom_")} <= WORKSPACE_ALLOWED


def test_declared_dependencies_respect_the_direction():
    """pyproject names no engine, launcher or old Dojo, and the import allow-list covers every declared
    dependency."""
    project = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = list(project["dependencies"]) + [
        dependency for extra in project["optional-dependencies"].values() for dependency in extra
    ]
    names = {dependency.split(">")[0].split("=")[0].split("[")[0].strip().lower() for dependency in declared}
    assert not names & {
        "llm-dojo-scoring",
        "mailroom-engine",
        "mailroom-launcher",
        "requests",
        "httpx",
        "openai",
    }
    import_names = {
        "python-dateutil": "dateutil",
        "pyyaml": "yaml",
        "sentence-transformers": "sentence_transformers",
    }
    runtime = {import_names.get(name, name.replace("-", "_")) for name in names} - {
        "pytest",
        "pytest_xdist",
        "hypothesis",
        "mailroom_record",
    }
    assert runtime <= THIRD_PARTY
