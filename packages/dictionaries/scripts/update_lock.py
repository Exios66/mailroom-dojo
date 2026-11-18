"""Rewrite the dictionary version lock from the data files.

Usage: python packages/dictionaries/scripts/update_lock.py [--data-dir DIR]

Refuses (exit 1, lock untouched) when a dictionary's content changed but its version did not,
or its version went backwards. Exit 0 after writing, or when the lock is already current.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mailroom_common.errors import ConfigError
from mailroom_dictionaries.dictionary import DATA_DIR, load_dictionary_dir
from mailroom_dictionaries.lock import BLOCKING_KINDS, LOCK_NAME, lock_problems, read_lock, write_lock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir", type=Path, default=DATA_DIR, help="folder holding the dictionaries and lock"
    )
    args = parser.parse_args(argv)
    lock_path = args.data_dir / LOCK_NAME
    try:
        current = {
            name: {"version": row["version"], "hash": row["hash"]}
            for name, row in load_dictionary_dir(args.data_dir).manifest().items()
        }
        locked = read_lock(lock_path) if lock_path.exists() else {}
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    problems = lock_problems(locked, current)
    blocking = [p for p in problems if p.kind in BLOCKING_KINDS]
    if blocking:
        for problem in blocking:
            print(f"refused: {problem.message}", file=sys.stderr)
        return 1
    if not problems:
        print(f"lock is current ({len(current)} dictionaries)")
        return 0
    write_lock(lock_path, current)
    for problem in problems:
        print(f"updated {problem.name}: {problem.kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
