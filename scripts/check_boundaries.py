"""Enforce the workspace's one-way dependency rule between packages.

Every package under ``packages/<name>/`` ships the module ``mailroom_<name>``. ``ALLOWED``
says which sibling packages each one may import. The check parses (never imports or runs)
every ``.py`` file anywhere in a package's folder (``conftest.py`` and setup files at its root
included; ``.venv``, ``build``, ``dist``, ``__pycache__`` and ``*.egg-info`` folders excluded)
and reports, as problems:

- an absolute ``import mailroom_x`` or ``from mailroom_x import y`` that the table does not
  allow, wherever it sits (module level, inside a function, under ``if TYPE_CHECKING:`` or
  ``try:``); relative imports stay inside one package and are ignored;
- an import of a ``mailroom_*`` module that no package folder or table row accounts for;
- a package folder with no row in the table (so nobody adds a package unmapped);
- a file that cannot be parsed (syntax error, bad encoding, NUL byte): reported, not raised,
  because an unparsed file is an unchecked file.

Rows for packages that do not exist yet are skipped. Exit status: 0 clean, 1 problems found,
2 bad usage (no ``packages/`` folder).
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ALLOWED",
    "NEVER",
    "Problem",
    "check_workspace",
    "imported_modules",
    "main",
    "table_problems",
]

MODULE_PREFIX = "mailroom_"
#: Folders directly under a package root that are never scanned (tooling output, not package code).
#: A real subpackage that happens to be named ``build`` or ``dist`` deeper down is still scanned.
SKIPPED_FOLDERS = frozenset({".venv", "build", "dist"})
#: Folder names skipped at any depth: bytecode caches and packaging metadata.
SKIPPED_ANYWHERE = frozenset({"__pycache__"})

_EVERY_OTHER = frozenset({"common", "dictionaries", "record", "labels", "dojo", "engine"})

# Package folder name -> the sibling packages it may import. A package may always import itself.
ALLOWED: Mapping[str, frozenset[str]] = {
    "common": frozenset(),
    "dictionaries": frozenset({"common"}),
    # record imports nothing of ours TODAY. Adopting common (hashing, errors, JSONL IO) is a
    # planned follow-up; that change moves this row to frozenset({"common"}).
    "record": frozenset(),
    "labels": frozenset({"common", "dictionaries", "record"}),
    "dojo": frozenset({"common", "dictionaries", "record", "labels"}),
    "engine": frozenset({"common", "dictionaries", "record"}),
    "launcher": _EVERY_OTHER,
}

# Pairs that must never be allowed, whatever the table above says: the Dojo scores runs and
# must not depend on the engine that produces them.
NEVER: frozenset[tuple[str, str]] = frozenset({("dojo", "engine")})


@dataclass(frozen=True, order=True)
class Problem:
    """One rule violation, pointing at a file and line (line 0 when there is no line)."""

    path: Path
    line: int
    message: str

    def render(self, root: Path) -> str:
        try:
            shown = self.path.relative_to(root)
        except ValueError:
            shown = self.path
        return f"{shown}:{self.line}: {self.message}"


def imported_modules(source: bytes, filename: str = "<source>") -> list[tuple[int, str]]:
    """Return ``(line, module)`` for every absolute import in ``source``, in file order.

    ``import a.b, c`` gives ``a.b`` and ``c``; ``from a.b import c`` gives ``a.b``. Relative
    imports (``from . import x``) are skipped. Raises ``SyntaxError`` or ``ValueError`` (NUL
    bytes, undecodable source) when the file cannot be parsed.
    """
    tree = ast.parse(source, filename=filename)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module))
    return sorted(found)


def table_problems(allowed: Mapping[str, frozenset[str]], never: frozenset[tuple[str, str]]) -> list[str]:
    """Return what is wrong with the table itself: unknown targets and forbidden pairs."""
    problems: list[str] = []
    for package, targets in sorted(allowed.items()):
        for target in sorted(targets - set(allowed)):
            problems.append(f"row {package!r} allows {target!r}, which has no row of its own")
        if package in targets:
            problems.append(f"row {package!r} lists itself; self-imports are always allowed")
    for importer, imported in sorted(never):
        if imported in allowed.get(importer, frozenset()):
            problems.append(f"row {importer!r} allows {imported!r}, which must never be allowed")
    return problems


def _package_dirs(packages_root: Path) -> list[Path]:
    return sorted(
        p
        for p in packages_root.iterdir()
        if p.is_dir() and not p.name.startswith((".", "_")) and not p.name.endswith(".egg-info")
    )


def _python_files(package_dir: Path) -> Iterator[Path]:
    for path in sorted(package_dir.rglob("*.py")):
        folders = path.relative_to(package_dir).parts[:-1]
        if folders and folders[0] in SKIPPED_FOLDERS:
            continue
        if not any(part in SKIPPED_ANYWHERE or part.endswith(".egg-info") for part in folders):
            yield path


def check_workspace(
    root: Path,
    allowed: Mapping[str, frozenset[str]] = ALLOWED,
    never: frozenset[tuple[str, str]] = NEVER,
) -> list[Problem]:
    """Check every package under ``root/packages`` against the table; return sorted problems.

    Raises ``FileNotFoundError`` if ``root/packages`` is not a directory.
    """
    packages_root = root / "packages"
    if not packages_root.is_dir():
        raise FileNotFoundError(f"no packages/ folder under {root}")
    problems = [
        Problem(packages_root, 0, f"dependency table: {text}") for text in table_problems(allowed, never)
    ]
    package_dirs = _package_dirs(packages_root)
    known = set(allowed) | {p.name for p in package_dirs}
    for package_dir in package_dirs:
        name = package_dir.name
        if name not in allowed:
            problems.append(
                Problem(
                    package_dir,
                    0,
                    f"package {name!r} has no row in the dependency table in scripts/check_boundaries.py",
                )
            )
            continue
        may_import = allowed[name] | {name}
        for path in _python_files(package_dir):
            try:
                imports = imported_modules(path.read_bytes(), str(path))
            except (SyntaxError, ValueError) as exc:
                line = getattr(exc, "lineno", None) or 0
                problems.append(Problem(path, line, f"cannot parse, so its imports are unchecked: {exc}"))
                continue
            for line, module in imports:
                top = module.split(".")[0]
                if not top.startswith(MODULE_PREFIX):
                    continue
                target = top.removeprefix(MODULE_PREFIX)
                if target not in known:
                    problems.append(
                        Problem(path, line, f"{name} imports {module}, which is no package of this workspace")
                    )
                elif target not in may_import:
                    allowed_text = ", ".join(sorted(allowed[name])) or "nothing of ours"
                    problems.append(
                        Problem(
                            path, line, f"{name} must not import {module} (it may import: {allowed_text})"
                        )
                    )
    return sorted(problems)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    root: Path = args.root
    try:
        problems = check_workspace(root)
    except FileNotFoundError as exc:
        print(f"check_boundaries: {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(problem.render(root))
    present = sorted(p.name for p in _package_dirs(root / "packages"))
    print(f"check_boundaries: {len(problems)} problem(s) in {len(present)} package(s): {', '.join(present)}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
