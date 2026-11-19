"""Package boundaries (plan: the desk imports record and labels and nothing else of ours;
record and labels import nothing of the desk) and domain-freedom (R4)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

import mailroom_desk
import mailroom_labels
import mailroom_record

WORKSPACE = (
    "mailroom_record",
    "mailroom_labels",
    "mailroom_desk",
    "mailroom_dojo",
    "mailroom_engine",
    "mailroom_launcher",
)
ALLOWED = {"mailroom_record", "mailroom_labels", "mailroom_desk"}
SRC = Path(mailroom_desk.__file__).parent
RECORD_SRC = Path(mailroom_record.__file__).parent
LABELS_SRC = Path(mailroom_labels.__file__).parent
REPO = Path(__file__).resolve().parents[3]


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


def _top(names: list[str]) -> set[str]:
    return {m.split(".")[0] for m in names if m.split(".")[0] in WORKSPACE}


def test_imports_in_tree_sees_both_import_forms(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import mailroom_desk.x\nfrom mailroom_engine import y\nfrom . import z\n")
    assert imports_in_tree(tmp_path) == ["mailroom_desk.x", "mailroom_engine"]


def test_desk_imports_only_record_and_labels_among_workspace_packages() -> None:
    workspace = _top(imports_in_tree(SRC))
    assert workspace - ALLOWED == set()
    # Positive controls: the walker sees both packages the desk may use.
    assert {"mailroom_record", "mailroom_labels"} <= workspace


def test_record_and_labels_import_nothing_of_the_desk() -> None:
    for src, own in ((RECORD_SRC, "mailroom_record"), (LABELS_SRC, "mailroom_labels")):
        workspace = _top(imports_in_tree(src))
        assert "mailroom_desk" not in workspace, src
        assert own in workspace  # positive control: the walker reads that package's files


def test_library_code_names_no_class_or_field_of_the_synthetic_profile() -> None:
    """R4: classes and fields are profile data; a hard-coded one would make a new class a
    code change. The names are the labels package's synthetic profile."""
    profile = yaml.safe_load(
        (REPO / "packages/labels/tests/fixtures/synthetic-profile.yaml").read_text(encoding="utf-8")
    )
    names = set(profile["classes"])
    for spec in profile["classes"].values():
        names |= set(spec.get("fields", {}))
        names |= set(spec.get("subclasses") or [])
    assert {"letter", "filing", "sender"} <= names  # positive control: the profile was read
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in sorted(names):
            if re.search(rf"['\"]{re.escape(name)}['\"]", text):
                offenders.append((path.name, name))
    assert offenders == []


def test_cause_tokens_live_in_config_not_in_code() -> None:
    """Only the two reserved tokens (the sample's cause and the outage tokens R1 refuses)
    are named in code; every other cause token is policy data."""
    policy = yaml.safe_load((REPO / "config/review/default.yaml").read_text(encoding="utf-8"))
    tokens = {c["token"] for c in policy["causes"]} - {"spot_check"}
    assert "boss_conflict" in tokens  # positive control: the catalog was read
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in sorted(tokens):
            if re.search(rf"['\"]{re.escape(token)}['\"]", text):
                offenders.append((path.name, token))
    assert offenders == []


def test_package_version_and_public_names() -> None:
    assert mailroom_desk.__version__ == "0.1.0"
    assert len(set(mailroom_desk.__all__)) == len(mailroom_desk.__all__)
    for name in mailroom_desk.__all__:
        assert hasattr(mailroom_desk, name), name
