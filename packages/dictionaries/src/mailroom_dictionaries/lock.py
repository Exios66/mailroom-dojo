"""The version lock: a committed ``{name: {version, hash}}`` record of every shipped dictionary.

The lock makes a forgotten version bump impossible to miss. Comparing the lock with the
dictionaries on disk gives one :class:`LockProblem` per difference:

* ``unbumped``: the content changed but the version did not (blocks a lock update);
* ``backwards``: the version is lower than the locked one (blocks a lock update);
* ``stale``: the version was bumped but the lock was not rewritten;
* ``unlocked``: a data file is missing from the lock;
* ``orphaned``: the lock lists a dictionary whose file is gone.

The test suite fails on any problem; ``scripts/update_lock.py`` rewrites the lock unless a
blocking problem is present.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mailroom_common.errors import ConfigError

__all__ = ["BLOCKING_KINDS", "LOCK_NAME", "LockProblem", "lock_problems", "read_lock", "write_lock"]

LOCK_NAME = "dictionaries.lock.json"

#: Problems a lock update must refuse: they mean a version was not bumped as it should be.
BLOCKING_KINDS = frozenset({"unbumped", "backwards"})


@dataclass(frozen=True)
class LockProblem:
    kind: str
    name: str
    message: str


def read_lock(path: str | Path) -> dict[str, dict[str, Any]]:
    """Read a lock file; a missing, unreadable or malformed lock is a :class:`ConfigError`."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read dictionary lock: {exc}", source=str(path)) from exc
    if not isinstance(data, dict) or not all(
        isinstance(row, dict)
        and set(row) == {"version", "hash"}
        and type(row["version"]) is int
        and isinstance(row["hash"], str)
        for row in data.values()
    ):
        raise ConfigError("dictionary lock must map each name to {version: int, hash: str}", source=str(path))
    return data


def write_lock(path: str | Path, manifest: Mapping[str, Mapping[str, Any]]) -> None:
    """Write ``manifest`` as the lock: sorted keys, two-space indent, trailing newline."""
    text = json.dumps({name: dict(row) for name, row in manifest.items()}, indent=2, sort_keys=True)
    Path(path).write_text(text + "\n", encoding="utf-8")


def lock_problems(
    locked: Mapping[str, Mapping[str, Any]], current: Mapping[str, Mapping[str, Any]]
) -> list[LockProblem]:
    """Every difference between the lock and the current manifest, sorted by name."""
    problems: list[LockProblem] = []
    for name in sorted(set(locked) | set(current)):
        if name not in locked:
            problems.append(
                LockProblem(
                    "unlocked", name, f"dictionary {name!r} is not in the lock; run scripts/update_lock.py"
                )
            )
            continue
        if name not in current:
            problems.append(
                LockProblem(
                    "orphaned", name, f"the lock lists {name!r} but its data file {name}.yaml is missing"
                )
            )
            continue
        old, new = locked[name], current[name]
        if new["version"] < old["version"]:
            problems.append(
                LockProblem(
                    "backwards",
                    name,
                    f"version of {name!r} went backwards: lock has {old['version']}, file has {new['version']}",
                )
            )
        elif new["version"] == old["version"] and new["hash"] != old["hash"]:
            problems.append(
                LockProblem(
                    "unbumped",
                    name,
                    f"content of {name!r} changed but its version is still {new['version']}: "
                    "bump `version`, then run scripts/update_lock.py",
                )
            )
        elif new["version"] > old["version"]:
            problems.append(
                LockProblem(
                    "stale",
                    name,
                    f"{name!r} is at version {new['version']} but the lock has {old['version']}; "
                    "run scripts/update_lock.py",
                )
            )
    return problems
