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
    return found


def test_imports_in_tree_sees_both_import_forms(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import mailroom_dojo.x\nfrom mailroom_engine import y\nfrom . import z\n")
    assert imports_in_tree(tmp_path) == ["mailroom_dojo.x", "mailroom_engine"]


def test_record_imports_no_sibling_package() -> None:
    offenders = imports_in_tree(Path(mailroom_record.__file__).parent)
    assert not [m for m in offenders if m.split(".")[0] in FORBIDDEN]


def test_package_version_is_set() -> None:
    assert mailroom_record.__version__ == "0.1.0"
