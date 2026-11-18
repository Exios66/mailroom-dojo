"""Check that the layout map in README.md matches the folders that really exist.

The map is the first fenced block under the ``## Layout`` heading: a tree drawn with
``├──``/``└──`` and four-character indents, one entry per line, ``name   description``.
A name ending in ``/`` is a folder; ``<client>`` and ``*`` are placeholders.

Reported as problems:

- an entry that should exist today but does not. An entry is optional when it, or an entry
  above it, carries ``(project N)``, ``(from project N on)`` or ``(created on first use)``
  in its description; an entry with a placeholder must match at least one real path;
- a real folder that the map does not name, checked in every mapped folder whose children
  the map lists by literal name (hidden folders, ``__pycache__``, ``*.egg-info``, ``build``
  and ``dist`` are ignored unless the map names them);
- a package folder under ``packages/`` without ``README.md``, ``pyproject.toml`` or
  ``src/mailroom_<name>/``.

Exit status: 0 clean, 1 problems found, 2 when README.md or its layout map is missing or
cannot be read.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Entry", "LayoutMapError", "check_layout", "main", "parse_layout_map"]

_CONNECTORS = ("├── ", "└── ")
_INDENTS = ("│   ", "    ")
_OPTIONAL = re.compile(r"\((?:from )?project \d+(?: on)?\)|\(created on first use\)")
_PLACEHOLDER = re.compile(r"<[^>/]+>|\*")
_IGNORED_SUFFIXES = (".egg-info",)
_IGNORED_NAMES = frozenset({"__pycache__", "build", "dist"})


class LayoutMapError(ValueError):
    """README.md has no layout map, or a line of it cannot be read as a tree entry."""


@dataclass(frozen=True)
class Entry:
    """One line of the map: its path from the root, as components, and how to check it."""

    parts: tuple[str, ...]
    is_dir: bool
    optional: bool
    line: int

    @property
    def has_placeholder(self) -> bool:
        return any(_PLACEHOLDER.search(part) for part in self.parts)

    @property
    def display(self) -> str:
        return "/".join(self.parts) + ("/" if self.is_dir else "")


def parse_layout_map(readme: str) -> list[Entry]:
    """Parse the layout map in ``readme`` (the text of README.md) into entries.

    Raises :class:`LayoutMapError` when there is no ``## Layout`` heading, no fenced block
    under it, or an entry line whose indent is not made of four-character steps.
    """
    lines = readme.splitlines()
    try:
        heading = next(i for i, text in enumerate(lines) if text.strip() == "## Layout")
    except StopIteration:
        raise LayoutMapError("README.md has no '## Layout' heading") from None
    try:
        start = next(i for i in range(heading + 1, len(lines)) if lines[i].startswith("```"))
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("```"))
    except StopIteration:
        raise LayoutMapError("README.md has no closed fenced block under '## Layout'") from None

    entries: list[Entry] = []
    stack: list[Entry] = []  # stack[d] is the latest entry at depth d
    for index in range(start + 1, end):
        text = lines[index]
        position = next((text.find(c) for c in _CONNECTORS if c in text), -1)
        if position < 0:
            continue  # the root line, or a note such as "(each package: ...)"
        prefix = text[:position]
        if len(prefix) % 4 or any(prefix[i : i + 4] not in _INDENTS for i in range(0, len(prefix), 4)):
            raise LayoutMapError(
                f"README.md line {index + 1}: indent is not in four-character steps: {text!r}"
            )
        depth = len(prefix) // 4
        if depth > len(stack):
            raise LayoutMapError(f"README.md line {index + 1}: entry is indented under nothing: {text!r}")
        name, _, description = text[position + 4 :].strip().partition(" ")
        parent = stack[depth - 1] if depth else None
        entry = Entry(
            parts=(parent.parts if parent else ()) + tuple(name.rstrip("/").split("/")),
            is_dir=name.endswith("/"),
            optional=bool(_OPTIONAL.search(description)) or bool(parent and parent.optional),
            line=index + 1,
        )
        del stack[depth:]
        stack.append(entry)
        entries.append(entry)
    if not entries:
        raise LayoutMapError("the layout map in README.md has no entries")
    return entries


def _ignored(path: Path) -> bool:
    return path.name.startswith(".") or path.name in _IGNORED_NAMES or path.name.endswith(_IGNORED_SUFFIXES)


def _matches(root: Path, entry: Entry) -> list[Path]:
    pattern = "/".join(_PLACEHOLDER.sub("*", part) for part in entry.parts)
    candidates = sorted(root.glob(pattern)) if entry.has_placeholder else [root / pattern]
    return [p for p in candidates if (p.is_dir() if entry.is_dir else p.is_file())]


def check_layout(root: Path) -> list[str]:
    """Compare ``root/README.md``'s layout map with the tree under ``root``; return problems.

    Raises :class:`LayoutMapError` if the map is missing or malformed, and ``OSError`` if
    README.md cannot be read.
    """
    entries = parse_layout_map((root / "README.md").read_text(encoding="utf-8"))
    problems: list[str] = []

    for entry in entries:
        if not entry.optional and not _matches(root, entry):
            kind = "folder" if entry.is_dir else "file"
            problems.append(
                f"README.md:{entry.line}: {kind} {entry.display} is in the layout map but missing"
            )

    # Folders the map does not name, in every mapped folder whose child folders it lists by
    # literal name. A file entry (even a pattern such as test-strategy-*.md) does not count.
    mapped_dirs: set[tuple[str, ...]] = {()} | {e.parts for e in entries if e.is_dir}
    for parts in sorted(mapped_dirs):
        depth = len(parts)
        children = {
            e.parts[depth]
            for e in entries
            if e.parts[:depth] == parts
            and (len(e.parts) > depth + 1 or (len(e.parts) == depth + 1 and e.is_dir))
        }
        folder = root.joinpath(*parts)
        if not children or any(_PLACEHOLDER.search(c) for c in children) or not folder.is_dir():
            continue
        for child in sorted(folder.iterdir()):
            if child.is_dir() and child.name not in children and not _ignored(child):
                shown = "/".join((*parts, child.name))
                problems.append(f"{shown}/: folder exists but is not in the layout map in README.md")

    packages = root / "packages"
    if packages.is_dir():
        for package in sorted(p for p in packages.iterdir() if p.is_dir() and not _ignored(p)):
            required = {
                "README.md": (package / "README.md").is_file(),
                "pyproject.toml": (package / "pyproject.toml").is_file(),
                f"src/mailroom_{package.name}/": (package / "src" / f"mailroom_{package.name}").is_dir(),
            }
            problems.extend(
                f"packages/{package.name}/: missing {name}" for name, ok in required.items() if not ok
            )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    try:
        problems = check_layout(args.root)
    except (LayoutMapError, OSError, UnicodeDecodeError) as exc:
        print(f"check_layout: {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(problem)
    print(f"check_layout: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
