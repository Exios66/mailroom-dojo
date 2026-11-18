"""mailroom_record must not import any sibling workspace package."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import mailroom_record

FORBIDDEN = ("mailroom_dojo", "mailroom_engine", "mailroom_launcher", "mailroom_labels")


def imports_in_tree(root: Path) -> list[str]:
    """Return every absolute module name imported by any .py file under root.

    Dynamic imports count too: ``__import__`` and ``importlib.import_module`` under any
    spelling (``builtins.__import__``, an alias from ``import ... as`` or
    ``from ... import ... as``, a name the function was assigned to), with the module name
    given by position or as ``name=``. A string constant is a name; any other argument, or
    the function handed on without being called, cannot be checked statically and is
    reported as ``<dynamic>``.
    """
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        loaders = _loader_names(tree)
        called: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.append(node.module)
            elif isinstance(node, ast.Call) and _is_loader(node.func, loaders):
                called.add(id(node.func))
                found.append(_module_arg(node))
        rebound = _rebound_values(tree)
        for node in ast.walk(tree):
            # A loader referenced but not called here: handed on (map, a callback), so
            # whatever it loads cannot be checked. A plain rebinding is followed instead.
            if _is_loader(node, loaders) and id(node) not in called and id(node) not in rebound:
                found.append("<dynamic>")
    return found


_LOADERS = frozenset({"__import__", "import_module"})


def _is_loader(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names and isinstance(node.ctx, ast.Load)
    return isinstance(node, ast.Attribute) and node.attr in _LOADERS


def _module_arg(call: ast.Call) -> str:
    arg = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "name"), None)
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else "<dynamic>"


def _rebound_values(tree: ast.AST) -> set[int]:
    """The ids of the values of plain assignments (``x = value``, ``x: T = value``)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            out.add(id(node.value))
    return out


def _loader_names(tree: ast.AST) -> set[str]:
    """Every name bound to ``__import__`` or ``import_module`` in a module, to a fixpoint."""
    names = set(_LOADERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            names.update(a.asname for a in node.names if a.name in _LOADERS and a.asname)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if _is_loader(value, names):
                for t in targets:
                    if isinstance(t, ast.Name) and t.id not in names:
                        names.add(t.id)
                        changed = True
    return names


def test_imports_in_tree_sees_dynamic_imports(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "import importlib\n"
        "from importlib import import_module\n"
        "__import__('mailroom_dojo.a')\n"
        "importlib.import_module('mailroom_engine.b')\n"
        "import_module('mailroom_labels')\n"
        "importlib.import_module(name)\n"
    )
    found = imports_in_tree(tmp_path)
    assert found == ["importlib", "importlib", "mailroom_dojo.a", "mailroom_engine.b", "mailroom_labels", "<dynamic>"]


def test_record_has_no_import_that_cannot_be_checked() -> None:
    assert "<dynamic>" not in imports_in_tree(Path(mailroom_record.__file__).parent)


def test_imports_in_tree_sees_both_import_forms(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import mailroom_dojo.x\nfrom mailroom_engine import y\nfrom . import z\n")
    assert imports_in_tree(tmp_path) == ["mailroom_dojo.x", "mailroom_engine"]


def test_record_imports_no_sibling_package() -> None:
    offenders = imports_in_tree(Path(mailroom_record.__file__).parent)
    assert not [m for m in offenders if m.split(".")[0] in FORBIDDEN]


def test_package_version_matches_the_installed_metadata() -> None:
    # pyproject.toml's version reaches the metadata; __version__ must say the same.
    from importlib.metadata import version

    assert mailroom_record.__version__ == version("mailroom-record")


@pytest.mark.parametrize(
    ("source", "want"),
    [
        # the name= keyword
        ("import importlib\nimportlib.import_module(name='mailroom_dojo.k')\n", "mailroom_dojo.k"),
        ("__import__(name='mailroom_engine.k')\n", "mailroom_engine.k"),
        # import aliases
        ("from importlib import import_module as im\nim('mailroom_labels.a')\n", "mailroom_labels.a"),
        ("from builtins import __import__ as imp\nimp('mailroom_launcher.a')\n", "mailroom_launcher.a"),
        ("import importlib as il\nil.import_module('mailroom_dojo.b')\n", "mailroom_dojo.b"),
        # receiver binding
        ("import importlib\nload = importlib.import_module\nload('mailroom_engine.c')\n", "mailroom_engine.c"),
        ("load = __import__\nagain = load\nagain('mailroom_dojo.c')\n", "mailroom_dojo.c"),
        ("import importlib\nload: object = importlib.import_module\nload('mailroom_labels.c')\n", "mailroom_labels.c"),
        # builtins.__import__
        ("import builtins\nbuiltins.__import__('mailroom_launcher.d')\n", "mailroom_launcher.d"),
        ("import builtins as b\nb.__import__(name='mailroom_dojo.d')\n", "mailroom_dojo.d"),
        # a non-constant name, and the function handed on uncalled: cannot be checked
        ("import importlib\nimportlib.import_module(name=n)\n", "<dynamic>"),
        ("import importlib\nlist(map(importlib.import_module, names))\n", "<dynamic>"),
    ],
)
def test_imports_in_tree_sees_every_spelling_of_a_dynamic_import(tmp_path: Path, source: str, want: str) -> None:
    (tmp_path / "a.py").write_text(source)
    assert want in imports_in_tree(tmp_path)
    if want != "<dynamic>":
        assert "<dynamic>" not in imports_in_tree(tmp_path)
