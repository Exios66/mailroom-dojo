#!/usr/bin/env python3
"""PreToolUse guard: refuse Edit/Write/NotebookEdit on files Claude must not hand-edit.

Reads the hook JSON on stdin. Exit 2 (with a reason on stderr) blocks the call and
shows the reason to Claude; any other exit lets it through. Fails open on malformed
input so a hook bug can never wedge a session.

Protected:
- ``.env`` files (secrets live in environment variables; only ``.env.example`` is tracked).
- Stored run data, wherever the store lives (it is usually outside the repository):
  event segments and sealed column files under ``runs/<run_id>/``, content-addressed
  blobs under ``blobs/<aa>/<bb>/``, packed run archives (``.tar.zst``) and the stored
  sandbox runs under ``runs/autoresearch/``. The record is append-only (design, ADR-0001
  and ADR-0004); a hand edit breaks the hash chain or fakes a baseline.
- ``docs/reports/`` and ``docs/*gate-report.md`` inside the repository: gate reports are
  written by scripts.
"""

from __future__ import annotations

import json
import os
import re
import sys

ENV_RULE = (
    r"(^|/)\.env(\.(?!example$)[^/]+)?$",
    "Secrets file. Secrets live only in environment variables; only .env.example is "
    "tracked. Ask the owner to edit .env themselves.",
)

STORED_RUN_RULES = [
    (
        r"(^|/)runs/[^/]+/(segments|columns)/",
        "Stored run record (event segments or sealed columns). The record is append-only; "
        "write it only through mailroom_record, never by hand.",
    ),
    (
        r"(^|/)blobs/[0-9a-f]{2}/[0-9a-f]{2}/",
        "Content-addressed blob store. Blobs are written once by mailroom_record and "
        "deleted only by tombstone.",
    ),
    (
        r"\.tar\.zst$",
        "Packed run archive. Re-pack it with the record tooling instead of editing it.",
    ),
    (
        r"(^|/)runs/autoresearch/",
        "Stored sandbox run folder: one of the only baselines there are. Read it; import "
        "it into a record; never change it.",
    ),
]

REPO_RULES = [
    (
        r"^docs/(reports/(?!README\.md$)|[^/]*gate-report\.md$)",
        "Gate reports are written by scripts. Re-run the script that writes the report.",
    ),
]


def _candidates(path: str, root: str) -> tuple[list[str], bool]:
    """Return the repo-relative POSIX paths to check, and whether any is inside root.

    Two views are checked: the lexical one (``.``/``..`` folded, symlinks kept) and
    the resolved one (symlinks followed), so ``docs/../.env`` and a symlinked folder
    are both caught. A path outside the root in both views is returned absolute.
    """
    path = path.replace("\\", "/")
    root = root.replace("\\", "/")
    joined = os.path.join(root, path)
    views = [
        (os.path.normpath(joined), os.path.normpath(os.path.abspath(root))),
        (os.path.realpath(joined), os.path.realpath(root)),
    ]
    inside: list[str] = []
    for full, base in views:
        try:
            rel = os.path.relpath(full, base).replace("\\", "/")
        except ValueError:  # different drive on Windows
            continue
        if rel != ".." and not rel.startswith("../"):
            inside.append(rel)
    if inside:
        return inside, True
    return [views[0][0].replace("\\", "/"), views[1][0].replace("\\", "/")], False


def blocked_reason(path: str, root: str) -> str | None:
    """Return why ``path`` must not be hand-edited, or None when it may be."""
    if not path:
        return None
    rels, inside = _candidates(path, root)
    rules = [ENV_RULE, *STORED_RUN_RULES]
    if inside:
        rules += REPO_RULES
    for rel in rels:
        for pattern, reason in rules:
            if re.search(pattern, rel):
                return reason
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not isinstance(path, str):
            return 0
        root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        reason = blocked_reason(path, root)
    except Exception:
        return 0
    if reason:
        print(f"Blocked edit to {path}: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
