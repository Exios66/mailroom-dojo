#!/usr/bin/env python3
"""PreToolUse guard: refuse a tool call that would write a session link or a tool footer.

Runs on Bash, on Edit/Write/MultiEdit/NotebookEdit, and on GitHub tools (MCP). It blocks
the call when the command or any text the tool would write carries a link to a Claude
session, project, thread, artifact or share page, a bare session id, a session trailer,
or a Claude Code footer. ``Co-Authored-By`` trailers are fine. The guard script
``.circleci/check_no_session_links.sh`` runs the same check on commits, pull requests and
tracked files; keep the two patterns in step (``tools/governance/tests`` checks that they
agree).

Limits. The hook sees only the tool input. It cannot see a footer that a tool appends
on the server side (the GitHub comment and PR tools in cloud sessions do), nor a string
a command builds at runtime (``printf``, variables, base64); overwrite a PR body and read
it back, as ``.claude/rules/pr-flow.md`` says. Matching is per line and ASCII, like the
script's ``grep``: a footer split across lines, a non-breaking space, lookalike letters
or zero-width characters are not caught. It guards against accidents, not adversaries.
A read-only search whose regex spells out a link path (the domain, an escaped dot and a
slash) is blocked too; write such regexes with a bracketed letter, or run the guard script.

The text an Edit replaces (``old_string``) is not checked, so a leaked link can be
removed with Edit. Exit 2 (reason on stderr, never the matched text) blocks the call;
any other exit lets it through. Fails open on malformed input so a hook bug can never
wedge a session. The bracketed letters keep this file from matching its own pattern.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

_DOT = r"(\.|\\\.|&#0*46;|&#x0*2e;|%2e)"
_WS = r"[ \t\r\f\v]"  # grep's [[:space:]] within one line, ASCII only
_SEP = r"[-_* \t\r\f\v]+"

PATTERN = re.compile(
    rf"https?://([a-z0-9-]+{_DOT})*claude{_DOT}a[i]"
    rf"|claude{_DOT}a[i](/|%2f)[a-z0-9]"
    rf"|claude{_DOT}co[m](/|%2f)(claude-code|code|session|project|chat|artifact|share)"
    rf"|Claude[-_ ][S]ession{_WS}*:"
    r"|[s]ession_[a-z0-9]{20,}"
    rf"|G[e]nerated{_SEP}(with|by|using){_SEP}\[?[*_]*Claude{_SEP}Code",
    re.IGNORECASE | re.ASCII,
)

# Keys whose text is being removed, not written.
SKIPPED_KEYS = frozenset({"old_string", "old_str"})

CHECKED_TOOLS = frozenset({"bash", "edit", "write", "notebookedit", "multiedit"})


def _is_checked_tool(name: str) -> bool:
    lowered = name.lower()
    return lowered in CHECKED_TOOLS or (lowered.startswith("mcp__") and "github" in lowered)


def _strings(value: Any) -> list[str]:
    """Every string inside ``value`` except those under ``SKIPPED_KEYS``.

    An explicit stack instead of recursion, so deeply nested input cannot raise
    RecursionError (which would fail open).
    """
    found: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            stack.extend(v for k, v in item.items() if k not in SKIPPED_KEYS)
        elif isinstance(item, list):
            stack.extend(item)
    return found


def find_violation(tool_name: str, tool_input: Any) -> bool:
    """Return True when the call would write forbidden text."""
    if not _is_checked_tool(tool_name):
        return False
    return any(PATTERN.search(text) for text in _strings(tool_input))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool_name = payload.get("tool_name") or ""
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_name, str):
            return 0
        blocked = find_violation(tool_name, tool_input)
    except Exception:
        return 0
    if blocked:
        print(
            f"Blocked {tool_name}: the call carries a Claude session/project/artifact link, "
            "a session id or trailer, or a Claude Code footer. These never go into commits, "
            "PR titles or bodies, comments, tags or tracked files (CLAUDE.md, hard rules). "
            "Remove it; a Co-Authored-By trailer alone is fine. To search for such text, "
            "write the regex with a bracketed letter or run "
            "bash .circleci/check_no_session_links.sh.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
