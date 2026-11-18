#!/usr/bin/env python3
"""PreToolUse guard: refuse Edit/Write/MultiEdit/NotebookEdit on files not to hand-edit.

Reads the hook JSON on stdin. Exit 2 (with a reason on stderr) blocks the call and
shows the reason to Claude; any other exit lets it through. Fails open on malformed
input so a hook bug can never wedge a session. Matching ignores case.

Protected:
- ``.env`` secrets: a ``.env`` file or folder, ``.env.<name>`` (except ``.env.example``)
  and basenames ending in ``.env`` such as ``prod.env``.
- Stored run data, wherever the store lives (it is usually outside the repository):
  event segments, sealed column files and seals under ``runs/<run_id>/``,
  content-addressed blobs under ``blobs/<aa>/<bb>/``, packed run archives (``.tar.zst``)
  and the stored sandbox runs under ``runs/autoresearch/``. The record is append-only
  (design, ADR-0001 and ADR-0004); a hand edit breaks the hash chain or fakes a baseline.
- ``docs/reports/`` and any ``docs/**/*gate-report.md`` inside the repository: gate
  reports are written by scripts.

Repository-only rules hold in both the launch checkout (``CLAUDE_PROJECT_DIR``) and the
checkout that contains the payload's ``cwd`` (a linked worktree), found with
``git rev-parse --show-toplevel``.

This is a guard against accidents, not a security boundary: it sees only the edit tools'
file path, so a Bash redirect, ``sed -i``, ``cp`` or a script writes past it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

ENV_REASON = (
    "Secrets file. Secrets live only in environment variables; only .env.example is "
    "tracked. Ask the owner to edit .env themselves."
)

ENV_RULES = [
    # .env as a file or a folder (.env/secret), and .env.<anything> except .env.example.
    (r"(^|/)\.env(/|$)", ENV_REASON),
    (r"(^|/)\.env\.(?!example$)[^/]+(/|$)", ENV_REASON),
    # Basenames such as prod.env.
    (r"(^|/)[^/]+\.env$", ENV_REASON),
]

STORED_RUN_RULES = [
    (
        r"(^|/)runs/[^/]+/(segments|columns|seals)/",
        "Stored run record (event segments, sealed columns or seals). The record is "
        "append-only; write it only through mailroom_record, never by hand.",
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

# Anchored at the repository root: an absolute path (one outside the repository) can
# never match, so these rules apply only inside it.
REPO_RULES = [
    (
        r"^docs/(reports/(?!readme\.md$)|(.*/)?[^/]*gate-report\.md$)",
        "Gate reports are written by scripts. Re-run the script that writes the report.",
    ),
]

RULES = [*ENV_RULES, *STORED_RUN_RULES, *REPO_RULES]


def _candidates(path: str, root: str) -> list[str]:
    """Return the POSIX paths to check: repo-relative when inside root, else absolute.

    Two views are checked: the lexical one (``.``/``..`` folded, symlinks kept) and
    the resolved one (symlinks followed), so ``docs/../.env`` and a symlinked folder
    are both caught.
    """
    path = path.replace("\\", "/")
    root = root.replace("\\", "/")
    joined = os.path.join(root, path)
    views = [
        (os.path.normpath(joined), os.path.normpath(os.path.abspath(root))),
        (os.path.realpath(joined), os.path.realpath(root)),
    ]
    found: list[str] = []
    for full, base in views:
        try:
            rel = os.path.relpath(full, base).replace("\\", "/")
        except ValueError:  # different drive on Windows
            rel = ".."
        if rel != ".." and not rel.startswith("../"):
            found.append(rel)
        else:
            found.append(full.replace("\\", "/"))
    return found


def blocked_reason(path: str, root: str) -> str | None:
    """Return why ``path`` must not be hand-edited, or None when it may be.

    Matching ignores case (``.ENV``, ``Docs/Reports``, ``.TAR.ZST``), since common
    filesystems do too.
    """
    if not path:
        return None
    for candidate in _candidates(path, root):
        for pattern, reason in RULES:
            if re.search(pattern, candidate, flags=re.IGNORECASE):
                return reason
    return None


def _active_checkout(cwd: object) -> str | None:
    """The top level of the git checkout containing ``cwd``, or None.

    In a linked worktree, ``CLAUDE_PROJECT_DIR`` is the checkout the session started in
    while the payload's ``cwd`` is the worktree being edited; the repository rules must
    hold in both. Raw ``cwd`` is never used as a root (it may be a subfolder).
    """
    if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
        return None
    try:
        done = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = done.stdout.strip()
    return top if done.returncode == 0 and top else None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not isinstance(path, str):
            return 0
        roots = [os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()]
        active = _active_checkout(payload.get("cwd"))
        if active and active not in roots:
            roots.append(active)
        reason = next((r for r in (blocked_reason(path, root) for root in roots) if r), None)
    except Exception:
        return 0
    if reason:
        print(f"Blocked edit to {path}: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
