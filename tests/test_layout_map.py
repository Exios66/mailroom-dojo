"""README.md's layout map against the real tree, and tests of the checker on synthetic trees."""

from __future__ import annotations

from pathlib import Path

import pytest

from check_layout import Entry, LayoutMapError, check_layout, main, parse_layout_map

ROOT = Path(__file__).resolve().parent.parent

MAP = """\
# demo

## Layout

```
demo/
├── README.md                  what this is
├── packages/                  one folder per package
│   ├── common/                mailroom_common: shared foundation
│   └── engine/                mailroom_engine: agents (project 5)
│       (each package: pyproject.toml, README.md, src/<module>/)
├── config/                    tuning (from project 2 on)
│   └── clients/<client>/      a client profile
├── labels/<snapshot>/         label snapshots (project 2)
├── docs/
│   ├── adr/                   decisions
│   ├── test-strategy-*.md     one per package
│   └── reports/               written by scripts (created on first use)
└── .circleci/                 CI
```

Rules: none.
"""


def make_tree(root: Path, readme: str = MAP) -> Path:
    (root / "README.md").write_text(readme, encoding="utf-8")
    for folder in ("docs/adr", ".circleci", "packages/common/src/mailroom_common"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "docs/test-strategy-common.md").write_text("x\n", encoding="utf-8")
    for name in ("README.md", "pyproject.toml"):
        (root / "packages/common" / name).write_text("x\n", encoding="utf-8")
    return root


# --- the real workspace -------------------------------------------------------------------


def test_real_readme_map_parses_and_names_the_shared_packages() -> None:
    entries = parse_layout_map((ROOT / "README.md").read_text(encoding="utf-8"))
    names = {e.display for e in entries}
    assert {"packages/common/", "packages/dictionaries/", "scripts/", "tests/", "Makefile"} <= names


@pytest.mark.skipif(
    not all((ROOT / "packages" / p / "README.md").is_file() for p in ("common", "dictionaries")),
    reason="the common and dictionaries READMEs are still being written in their own commits",
)
def test_real_workspace_matches_its_layout_map() -> None:
    assert check_layout(ROOT) == []


# --- the parser -----------------------------------------------------------------------------


def test_parser_builds_full_paths_and_inherits_optional() -> None:
    entries = {e.display: e for e in parse_layout_map(MAP)}
    assert list(entries) == [
        "README.md",
        "packages/",
        "packages/common/",
        "packages/engine/",
        "config/",
        "config/clients/<client>/",
        "labels/<snapshot>/",
        "docs/",
        "docs/adr/",
        "docs/test-strategy-*.md",
        "docs/reports/",
        ".circleci/",
    ]
    assert entries["config/clients/<client>/"] == Entry(("config", "clients", "<client>"), True, True, 13)
    assert entries["packages/engine/"].optional and not entries["packages/common/"].optional
    assert entries["docs/reports/"].optional and not entries["docs/adr/"].optional
    assert not entries["README.md"].is_dir


@pytest.mark.parametrize(
    ("readme", "message"),
    [
        ("# no map here\n", "no '## Layout' heading"),
        ("## Layout\n\ntext only\n", "no closed fenced block"),
        ("## Layout\n```\nroot/\n├── a/\n", "no closed fenced block"),
        ("## Layout\n```\nroot/\n```\n", "has no entries"),
        ("## Layout\n```\nroot/\n├── a/\n  ├── b/\n```\n", "line 5: indent is not in four-character steps"),
        ("## Layout\n```\nroot/\n├── a/\n│   │   ├── b/\n```\n", "line 5: entry is indented under nothing"),
    ],
    ids=["no-heading", "no-block", "unclosed", "empty", "bad-indent", "orphan"],
)
def test_malformed_map_raises_with_its_line(readme: str, message: str) -> None:
    with pytest.raises(LayoutMapError, match=message):
        parse_layout_map(readme)


# --- the checker on synthetic trees ---------------------------------------------------------


def test_matching_tree_has_no_problems(tmp_path: Path) -> None:
    assert check_layout(make_tree(tmp_path)) == []


def test_missing_required_folder_and_file_are_reported_with_map_line(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "docs/adr").rmdir()
    (root / "docs/test-strategy-common.md").unlink()
    assert check_layout(root) == [
        "README.md:16: folder docs/adr/ is in the layout map but missing",
        "README.md:17: file docs/test-strategy-*.md is in the layout map but missing",
    ]


def test_a_file_where_a_folder_is_mapped_is_missing(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / ".circleci").rmdir()
    (root / ".circleci").write_text("not a folder\n", encoding="utf-8")
    assert check_layout(root) == ["README.md:19: folder .circleci/ is in the layout map but missing"]


def test_optional_entries_may_be_absent_or_present(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    for folder in ("config/clients/acme", "labels/v1", "docs/reports", "packages/engine/src/mailroom_engine"):
        (root / folder).mkdir(parents=True)
    for name in ("README.md", "pyproject.toml"):
        (root / "packages/engine" / name).write_text("x\n", encoding="utf-8")
    assert check_layout(root) == []


def test_unmapped_folders_are_reported_and_noise_is_ignored(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    for folder in (
        "tools",
        "docs/notes",
        ".venv",
        "__pycache__",
        "x.egg-info",
        "build",
        "dist",
        "docs/adr/drafts",
    ):
        (root / folder).mkdir(parents=True)
    (root / "notes.txt").write_text("files are not checked\n", encoding="utf-8")
    # docs/adr/drafts is not reported: the map lists no children of docs/adr.
    # The root is checked first, then each mapped folder in path order.
    assert check_layout(root) == [
        "tools/: folder exists but is not in the layout map in README.md",
        "docs/notes/: folder exists but is not in the layout map in README.md",
    ]


def test_unmapped_package_is_reported_with_its_missing_parts(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "packages/utilities/src/utilities").mkdir(parents=True)
    assert check_layout(root) == [
        "packages/utilities/: folder exists but is not in the layout map in README.md",
        "packages/utilities/: missing README.md",
        "packages/utilities/: missing pyproject.toml",
        "packages/utilities/: missing src/mailroom_utilities/",
    ]


def test_package_missing_its_module_folder_is_reported(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "packages/common/src/mailroom_common").rmdir()
    (root / "packages/common/README.md").unlink()
    assert check_layout(root) == [
        "packages/common/: missing README.md",
        "packages/common/: missing src/mailroom_common/",
    ]


def test_placeholder_folders_disable_the_extras_check_below_them(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "config/clients/acme/anything").mkdir(parents=True)
    assert check_layout(root) == []


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(tmp_path)]) == 2
    assert "README.md" in capsys.readouterr().err

    (tmp_path / "README.md").write_bytes(b"\xff\xfe not utf-8")
    assert main(["--root", str(tmp_path)]) == 2
    assert "check_layout:" in capsys.readouterr().err

    make_tree(tmp_path)
    assert main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "check_layout: 0 problem(s)"

    (tmp_path / "stray").mkdir()
    assert main(["--root", str(tmp_path)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "stray/: folder exists but is not in the layout map in README.md",
        "check_layout: 1 problem(s)",
    ]
