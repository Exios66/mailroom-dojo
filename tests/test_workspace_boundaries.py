"""The workspace's one-way dependency rule, and tests of the checker that enforces it."""

from __future__ import annotations

from pathlib import Path

import pytest

from check_boundaries import ALLOWED, NEVER, Problem, check_workspace, imported_modules, main, table_problems

ROOT = Path(__file__).resolve().parent.parent


def write(root: Path, relative: str, text: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def messages(problems: list[Problem], root: Path) -> list[str]:
    return [p.render(root) for p in problems]


# --- the real workspace -------------------------------------------------------------------


def test_workspace_obeys_the_dependency_table() -> None:
    problems = check_workspace(ROOT)
    assert messages(problems, ROOT) == []


def test_every_existing_package_has_a_row() -> None:
    present = {p.name for p in (ROOT / "packages").iterdir() if p.is_dir() and not p.name.startswith(".")}
    assert present - set(ALLOWED) == set()


def test_table_rows_state_the_agreed_rule() -> None:
    assert ALLOWED["common"] == frozenset()
    assert ALLOWED["dictionaries"] == {"common"}
    assert ALLOWED["record"] == frozenset()  # changes when record adopts common
    assert ALLOWED["labels"] == {"common", "dictionaries", "record"}
    assert ALLOWED["dojo"] == {"common", "dictionaries", "record", "labels"}
    assert ALLOWED["engine"] == {"common", "dictionaries", "record"}
    assert ALLOWED["launcher"] == set(ALLOWED) - {"launcher"}
    assert ("dojo", "engine") in NEVER
    assert table_problems(ALLOWED, NEVER) == []


def test_common_is_a_leaf_every_other_package_may_use() -> None:
    assert all("common" in targets for name, targets in ALLOWED.items() if name not in {"common", "record"})


# --- the import scanner ---------------------------------------------------------------------


def test_scanner_sees_every_absolute_import_form() -> None:
    source = b"""
import os, mailroom_record.log as log
from mailroom_labels.snapshot import load
from . import sibling
from .. import parent
from .inner import thing

def later():
    import mailroom_dojo

if TYPE_CHECKING:
    from mailroom_engine import Agent

try:
    import mailroom_launcher
except ImportError:
    pass

class K:
    def m(self):
        from mailroom_common.errors import ConfigError
"""
    assert imported_modules(source) == [
        (2, "mailroom_record.log"),
        (2, "os"),
        (3, "mailroom_labels.snapshot"),
        (9, "mailroom_dojo"),
        (12, "mailroom_engine"),
        (15, "mailroom_launcher"),
        (21, "mailroom_common.errors"),
    ]


def test_scanner_raises_on_unparseable_source() -> None:
    with pytest.raises(SyntaxError):
        imported_modules(b"def broken(:\n")


# --- the checker on synthetic workspaces ----------------------------------------------------


def make_package(root: Path, name: str, src: str = "", test: str = "") -> None:
    write(root, f"packages/{name}/src/mailroom_{name}/__init__.py", src)
    write(root, f"packages/{name}/tests/test_{name}.py", test)


def test_allowed_import_passes(tmp_path: Path) -> None:
    make_package(tmp_path, "common")
    make_package(tmp_path, "dictionaries", src="from mailroom_common.errors import ConfigError\n")
    make_package(tmp_path, "labels", test="import mailroom_dictionaries\nimport mailroom_labels\n")
    assert check_workspace(tmp_path) == []


def test_forbidden_import_is_reported_with_file_and_line(tmp_path: Path) -> None:
    make_package(tmp_path, "common", src='"""doc"""\n\nimport json\nimport mailroom_record\n')
    make_package(tmp_path, "record")
    assert messages(check_workspace(tmp_path), tmp_path) == [
        "packages/common/src/mailroom_common/__init__.py:4: "
        "common must not import mailroom_record (it may import: nothing of ours)"
    ]


def test_dojo_importing_engine_is_reported(tmp_path: Path) -> None:
    make_package(tmp_path, "engine")
    make_package(tmp_path, "dojo", test="from mailroom_engine.agents import Sorter\n")
    assert messages(check_workspace(tmp_path), tmp_path) == [
        "packages/dojo/tests/test_dojo.py:1: dojo must not import mailroom_engine.agents "
        "(it may import: common, dictionaries, labels, record)"
    ]


def test_import_hidden_in_a_function_is_reported(tmp_path: Path) -> None:
    make_package(tmp_path, "dictionaries", src="def load():\n    import mailroom_labels\n    return 1\n")
    make_package(tmp_path, "labels")
    problems = check_workspace(tmp_path)
    assert [(p.path.name, p.line) for p in problems] == [("__init__.py", 2)]
    assert "dictionaries must not import mailroom_labels" in problems[0].message


def test_conditional_imports_are_reported(tmp_path: Path) -> None:
    make_package(
        tmp_path,
        "record",
        src=(
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from mailroom_common import errors\n"
            "try:\n"
            "    import mailroom_dictionaries\n"
            "except ImportError:\n"
            "    mailroom_dictionaries = None\n"
        ),
    )
    make_package(tmp_path, "common")
    make_package(tmp_path, "dictionaries")
    assert [(p.line, p.message.split(" (")[0]) for p in check_workspace(tmp_path)] == [
        (3, "record must not import mailroom_common"),
        (5, "record must not import mailroom_dictionaries"),
    ]


def test_scripts_folder_is_scanned_and_relative_imports_are_ignored(tmp_path: Path) -> None:
    make_package(tmp_path, "common", src="from . import errors\nfrom .errors import ConfigError\n")
    write(tmp_path, "packages/common/scripts/tool.py", "import mailroom_launcher\n")
    make_package(tmp_path, "launcher")
    assert [p.path.name for p in check_workspace(tmp_path)] == ["tool.py"]


def test_syntax_error_file_is_reported_and_the_rest_still_checked(tmp_path: Path) -> None:
    make_package(tmp_path, "common", src="def broken(:\n    pass\n")
    write(tmp_path, "packages/common/tests/test_other.py", "import mailroom_engine\n")
    make_package(tmp_path, "engine")
    problems = messages(check_workspace(tmp_path), tmp_path)
    assert len(problems) == 2
    assert problems[0].startswith("packages/common/src/mailroom_common/__init__.py:1: cannot parse")
    assert problems[1].startswith(
        "packages/common/tests/test_other.py:1: common must not import mailroom_engine"
    )


@pytest.mark.parametrize(
    "content",
    [b"import os\x00\n", b"\xff\xfe\xfa not utf-8\n", b"# -*- coding: no-such-codec -*-\nimport os\n"],
    ids=["nul-byte", "not-utf8", "unknown-codec"],
)
def test_undecodable_file_is_reported_not_raised(tmp_path: Path, content: bytes) -> None:
    make_package(tmp_path, "common")
    write(tmp_path, "packages/common/src/mailroom_common/bad.py", content)
    problems = check_workspace(tmp_path)
    assert len(problems) == 1
    assert problems[0].path.name == "bad.py"
    assert "cannot parse, so its imports are unchecked" in problems[0].message


def test_unmapped_package_fails(tmp_path: Path) -> None:
    make_package(tmp_path, "common")
    make_package(tmp_path, "utilities", src="import mailroom_common\n")
    assert messages(check_workspace(tmp_path), tmp_path) == [
        "packages/utilities:0: package 'utilities' has no row in the dependency table "
        "in scripts/check_boundaries.py"
    ]


def test_unknown_workspace_module_is_reported(tmp_path: Path) -> None:
    make_package(tmp_path, "labels", src="import mailroom_helpers\n")
    assert [p.message for p in check_workspace(tmp_path)] == [
        "labels imports mailroom_helpers, which is no package of this workspace"
    ]


def test_missing_packages_are_skipped_but_their_rows_still_bind(tmp_path: Path) -> None:
    # engine does not exist here; dojo importing it is still a violation, not an unknown module.
    make_package(tmp_path, "dojo", src="import mailroom_engine\n")
    assert [p.message.split(" (")[0] for p in check_workspace(tmp_path)] == [
        "dojo must not import mailroom_engine"
    ]


def test_bad_table_is_reported(tmp_path: Path) -> None:
    make_package(tmp_path, "dojo")
    table = {"dojo": frozenset({"engine", "dojo"}), "engine": frozenset()}
    assert [p.message for p in check_workspace(tmp_path, table, NEVER)] == [
        "dependency table: row 'dojo' allows 'engine', which must never be allowed",
        "dependency table: row 'dojo' lists itself; self-imports are always allowed",
    ]
    assert table_problems({"a": frozenset({"ghost"})}, frozenset()) == [
        "row 'a' allows 'ghost', which has no row of its own"
    ]


def test_missing_packages_folder_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no packages/ folder"):
        check_workspace(tmp_path)


def test_cli_exit_codes_and_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(tmp_path)]) == 2
    assert "no packages/ folder" in capsys.readouterr().err

    make_package(tmp_path, "common")
    assert main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "check_boundaries: 0 problem(s) in 1 package(s): common"

    write(tmp_path, "packages/common/src/mailroom_common/x.py", "import mailroom_dojo\n")
    assert main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "packages/common/src/mailroom_common/x.py:1: common must not import mailroom_dojo" in out
    assert out.strip().endswith("check_boundaries: 1 problem(s) in 1 package(s): common")
