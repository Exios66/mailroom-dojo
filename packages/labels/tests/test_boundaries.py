"""mailroom_labels imports mailroom_record and no other workspace package (plan decision 9)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

import mailroom_labels

WORKSPACE = ("mailroom_record", "mailroom_labels", "mailroom_dojo", "mailroom_engine", "mailroom_launcher")
ALLOWED = {"mailroom_record", "mailroom_labels"}
SRC = Path(mailroom_labels.__file__).parent


def imports_in_tree(root: Path) -> list[str]:
    """Every absolute module name imported by any .py file under root."""
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


def test_labels_imports_only_record_among_workspace_packages() -> None:
    workspace = {m.split(".")[0] for m in imports_in_tree(SRC) if m.split(".")[0] in WORKSPACE}
    assert workspace - ALLOWED == set()
    assert "mailroom_record" in workspace  # positive control: the walker sees the one it may use


#: Synthetic profile names that are also this package's own vocabulary, so a quoted
#: occurrence in library code is not a hard-coded class or field: ``kind`` is a manifest key.
PACKAGE_WORDS = frozenset({"kind"})


def synthetic_class_and_field_names() -> set[str]:
    """Every class, subclass and field name the synthetic test profile declares."""
    from labels_testkit import PROFILE_PATH

    raw = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    names = set(raw["classes"])
    for spec in raw["classes"].values():
        names |= set(spec["fields"]) | set(spec.get("subclasses", []))
    return names


def test_library_code_names_no_class_or_field_of_the_synthetic_profile() -> None:
    """R4: classes and fields are profile data. The library must not name the test profile's
    classes or fields (a hard-coded name would make a new class a code change). The names
    come from the profile itself, so a field added to the fixture is checked too."""
    names = synthetic_class_and_field_names()
    assert {"letter", "sender", "sent_on", "action_items"} <= names  # positive control
    assert names >= PACKAGE_WORDS  # an exclusion that no longer names a profile entry is stale
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in sorted(names - PACKAGE_WORDS):
            if re.search(rf"['\"]{re.escape(name)}['\"]", text):
                offenders.append((path.name, name))
    assert offenders == []


def test_package_version_is_set() -> None:
    assert mailroom_labels.__version__ == "0.1.0"
