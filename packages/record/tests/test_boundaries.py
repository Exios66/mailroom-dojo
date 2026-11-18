"""mailroom_record must not import any sibling workspace package."""

from __future__ import annotations

import ast
from pathlib import Path

import mailroom_record

FORBIDDEN = ("mailroom_dojo", "mailroom_engine", "mailroom_launcher", "mailroom_labels")


def imports_in_tree(root: Path) -> list[str]:
    """Return every absolute module name imported by any .py file under root."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.append(node.module)
            elif isinstance(node, ast.Call) and _dynamic_import(node.func) and node.args:
                # __import__("x") and importlib.import_module("x"): a string constant is a
                # name; anything else cannot be checked statically, so it is reported as is.
                arg = node.args[0]
                found.append(arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else "<dynamic>")
    return found


def _dynamic_import(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in {"__import__", "import_module"}
    return isinstance(func, ast.Attribute) and func.attr == "import_module"


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
