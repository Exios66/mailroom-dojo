"""The per-run desk lock: one exclusive ``flock`` on ``<root>/desk/<run_id>.lock``.

The record allows one writer per document chain, but only enforces it inside one process
(``DocumentInUse``); two processes writing one document fork its chain. So every desk
action that appends takes this lock for its whole read-decide-append cycle: it re-reads
the case under the lock, opens the writer ``desk``, appends, closes (which flushes and
fsyncs), applies the event to the index if there is one, and only then releases the lock.

``flock`` locks belong to an open file description, so two threads of one process
exclude each other as two processes do, and the kernel drops the lock when its holder
dies (a killed desk process never leaves the run locked). The lock file itself is never
deleted (deleting it would let two holders lock two different inodes). Acquisition polls
with a non-blocking ``flock`` until ``timeout`` seconds pass, then raises
``DeskLockTimeout``.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator
from pathlib import Path

from mailroom_desk.errors import DeskLockTimeout

__all__ = ["DEFAULT_LOCK_TIMEOUT", "desk_lock", "lock_path"]

DEFAULT_LOCK_TIMEOUT = 30.0
_POLL_START = 0.002
_POLL_MAX = 0.05


def lock_path(root: Path, run_id: str) -> Path:
    """Where the desk lock of ``run_id`` lives (``run_id`` must already be validated)."""
    return Path(root) / "desk" / f"{run_id}.lock"


@contextlib.contextmanager
def desk_lock(root: Path, run_id: str, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    """Hold the exclusive desk lock of ``run_id`` for the ``with`` block."""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0:
        raise ValueError(f"timeout must be a number >= 0, got {timeout!r}")
    path = lock_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        pause = _POLL_START
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise DeskLockTimeout(run_id, timeout) from None
                time.sleep(min(pause, left))
                pause = min(pause * 2, _POLL_MAX)
        yield
    finally:
        os.close(fd)  # closing the only descriptor releases the flock
