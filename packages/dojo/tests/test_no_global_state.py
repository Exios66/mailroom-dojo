"""Static checks over the Dojo source: no environment, caches, network, threads or mutable module state.

Scoring results depend only on the profile and the inputs a caller passes. These
checks read the source with :mod:`ast`, so a new module is covered automatically.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from dojo_testkit import SOURCE_ROOT

PACKAGE_DIRECTORY = SOURCE_ROOT
SOURCE_FILES = sorted(PACKAGE_DIRECTORY.rglob("*.py"))

FORBIDDEN_IMPORTS = {
    "socket",
    "urllib",
    "http",
    "requests",
    "httpx",
    "aiohttp",
    "threading",
    "multiprocessing",
    "concurrent",
    "asyncio",
    "openai",
    "anthropic",
    "langfuse",
    "llm_dojo_scoring",
    "dotenv",
}
FORBIDDEN_DECORATORS = {"lru_cache", "cache", "cached_property"}
#: Module-level names that may hold a call result: immutable values built once, and the
#: loader's section readers (closures that read and never write what they close over).
IMMUTABLE_FACTORIES = {
    "frozenset",
    "tuple",
    "FrozenMapping",
    "MappingProxyType",
    "re.compile",
    "logging.getLogger",
    "TypeVar",
    "_FixedPivotParserInfo",
    "datetime.datetime",
    "_simple_section",
}


def _relative(path: Path) -> str:
    """The path relative to the package, for messages."""
    return str(path.relative_to(SOURCE_ROOT.parent))


def _call_name(node: ast.AST) -> str:
    """The dotted name of a call's function, or ""."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def test_the_source_tree_is_found():
    """Guard against checking nothing."""
    assert len(SOURCE_FILES) >= 15
    assert PACKAGE_DIRECTORY / "profile" / "loading.py" in SOURCE_FILES


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_no_environment_network_thread_or_engine_imports(path):
    """No module reads the environment or imports network, thread, LLM or old-Dojo code."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            names = []
        problems += [f"imports {name}" for name in names if name.split(".")[0] in FORBIDDEN_IMPORTS]
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "putenv"}:
            problems.append(f"uses os.{node.attr} at line {node.lineno}")
        if isinstance(node, ast.Global):
            problems.append(f"global statement at line {node.lineno}")
        for decorator in getattr(node, "decorator_list", []):
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if _call_name(target).split(".")[-1] in FORBIDDEN_DECORATORS:
                problems.append(f"cache decorator at line {node.lineno}")
        if isinstance(node, ast.Call) and _call_name(node.func) in {
            "datetime.now",
            "date.today",
            "time.time",
            "datetime.today",
            "time.localtime",
        }:
            problems.append(f"reads the clock at line {node.lineno}")
    assert not problems, f"{_relative(path)}: {problems}"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_no_mutable_module_level_values(path):
    """Module-level assignments hold constants or immutable values, never a list, dict, set or open call
    result."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            if names == ["__all__"] or value is None:
                continue
            if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
                problems.append(f"{names} is a mutable {type(value).__name__} at line {node.lineno}")
            elif isinstance(value, ast.Call) and _call_name(value.func) not in IMMUTABLE_FACTORIES:
                problems.append(f"{names} = {_call_name(value.func)}(...) at line {node.lineno}")
    assert not problems, f"{_relative(path)}: {problems}"


def test_the_checks_catch_what_they_look_for(tmp_path, monkeypatch):
    """A module with each forbidden pattern fails both checks (the checks are not vacuous)."""
    bad = tmp_path / "mailroom_dojo" / "bad.py"
    bad.parent.mkdir()
    bad.write_text(
        "import os, socket\nfrom functools import lru_cache\nCACHE = {}\nSEEN = set()\n"
        "@lru_cache\ndef f():\n    global CACHE\n    return os.environ.get('X')\n"
    )
    import test_no_global_state as module

    monkeypatch.setattr(module, "SOURCE_ROOT", bad.parent)
    with pytest.raises(AssertionError) as imports:
        module.test_no_environment_network_thread_or_engine_imports(bad)
    for expected in ("imports socket", "os.environ", "global statement", "cache decorator"):
        assert expected in str(imports.value)
    with pytest.raises(AssertionError) as state:
        module.test_no_mutable_module_level_values(bad)
    assert "['CACHE'] is a mutable Dict" in str(state.value) and "['SEEN'] = set(...)" in str(state.value)
