#!/usr/bin/env python3
"""PreToolUse guard: refuse a tool call that would write a session link or a tool footer.

Runs on Bash, on Edit/Write/NotebookEdit, and on GitHub tools (MCP). It blocks the call
when the command or any text the tool would write carries a link to a Claude session,
project, thread, artifact or share page, a session trailer, or a Claude Code footer.
``Co-Authored-By`` trailers are fine. CI runs the same check on commits, pull requests
and tracked files (``.circleci/check_no_session_links.sh``); keep the two patterns in
step (``tools/governance/tests`` checks that they agree).

The text an Edit replaces (``old_string``) is not checked, so a leaked link can be
removed with Edit. Exit 2 (reason on stderr, never the matched text) blocks the call;
any other exit lets it through. Fails open on malformed input so a hook bug can never
wedge a session. The bracketed letters keep this file from matching its own pattern.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Iterator

PATTERN = re.compile(
    r"https?://([a-z0-9-]+\.)*claude\.a[i]"
    r"|claude\.a[i]/[a-z0-9]"
    r"|claude\.co[m]/(claude-code|code|project|chat|artifact|share)"
    r"|Claude-[S]ession\s*:"
    r"|G[e]nerated\s+(with|by)\s+\[?Claude\s+Code",
    re.IGNORECASE,
)

# Keys whose text is being removed, not written.
SKIPPED_KEYS = frozenset({"old_string", "old_str"})


def _is_checked_tool(name: str) -> bool:
    return name in {"Bash", "Edit", "Write", "NotebookEdit", "MultiEdit"} or (
        name.startswith("mcp__") and "github" in name.lower()
    )


def _strings(value: Any) -> Iterator[str]:
    """Yield every string inside ``value`` except those under ``SKIPPED_KEYS``."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in SKIPPED_KEYS:
                yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


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
            "a session trailer or a Claude Code footer. These never go into commits, PR "
            "titles or bodies, comments, tags or tracked files (CLAUDE.md, hard rules). "
            "Remove it; a Co-Authored-By trailer alone is fine.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
