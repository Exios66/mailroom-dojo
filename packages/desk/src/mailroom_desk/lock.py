"""The per-run desk lock: one exclusive ``flock`` on ``<root>/desk/<run_id>.lock``.

The record allows one writer per document chain, but only enforces it inside one process
(``DocumentInUse``); two processes writing one document fork its chain. So every desk
action that appends takes this lock for its whole read-decide-append cycle: it re-reads
the case under the lock, opens the writer ``desk``, appends, closes (which flushes and
fsyncs), applies the event to the index if there is one, and only then releases the lock.

``flock`` locks belong to an open file description, and every acquisition opens the file
anew, so two threads of one process exclude each other as two processes do, and the kernel
drops the lock when its holder dies (a killed desk process never leaves the run locked).
The lock file itself is never deleted (deleting it would let two holders lock two
different inodes). Acquisition polls with a non-blocking ``flock`` until ``timeout``
seconds pass, then raises ``DeskLockTimeout``.

``seal_guard`` takes the record's own seal lock (``runs/<run_id>/seals/.lock``, the file
``mailroom_record.seal.seal_run`` holds for a whole seal, documented in that module's
layout) the same way, so a seal cannot land between the desk's "is this run closed?" check
and its append (see ``desk.py``). It makes ``seals/`` durable first (creates it if missing,
then fsyncs it and the run directory, on every call), because ``seal_run`` skips those
fsyncs for a directory that already exists.

A process forked without ``exec`` inherits the lock: ``flock`` belongs to the open file
description, which a ``fork`` shares, so the lock stays held until the parent *and* the
child have closed it (or exited). Never fork while holding a desk lock; start workers with
``multiprocessing``'s ``spawn`` (or ``fork`` followed by ``exec``, as ``subprocess`` does):
the descriptors are non-inheritable, so an exec'd child never holds the lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator
from pathlib import Path

from mailroom_desk.errors import DeskLockTimeout

__all__ = ["DEFAULT_LOCK_TIMEOUT", "check_timeout", "desk_lock", "lock_path", "seal_guard", "seal_lock_path"]

DEFAULT_LOCK_TIMEOUT = 30.0
_POLL_START = 0.002
_POLL_MAX = 0.05


def lock_path(root: Path, run_id: str) -> Path:
    """Where the desk lock of ``run_id`` lives (``run_id`` must already be validated)."""
    return Path(root) / "desk" / f"{run_id}.lock"


def seal_lock_path(root: Path, run_id: str) -> Path:
    """The record's seal lock of ``run_id`` (``runs/<run_id>/seals/.lock``)."""
    return Path(root) / "runs" / run_id / "seals" / ".lock"


def check_timeout(timeout: object) -> float:
    """``timeout`` as a float, or ``ValueError`` unless it is a finite number >= 0."""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 <= timeout < float("inf"):
        raise ValueError(f"timeout must be a finite number >= 0, got {timeout!r}")
    return float(timeout)


@contextlib.contextmanager
def _flock(path: Path, run_id: str, timeout: float, what: str, mode: int) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, mode)
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
                    raise DeskLockTimeout(run_id, timeout, what) from None
                time.sleep(min(pause, left))
                pause = min(pause * 2, _POLL_MAX)
        yield
    finally:
        os.close(fd)  # closing the only descriptor releases the flock


@contextlib.contextmanager
def desk_lock(root: Path, run_id: str, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    """Hold the exclusive desk lock of ``run_id`` for the ``with`` block."""
    timeout = check_timeout(timeout)
    path = lock_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _flock(path, run_id, timeout, "desk", 0o600):
        yield


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _seals_dir_durable(seals: Path) -> None:
    """Make ``seals/`` exist durably: create it if missing, then fsync it and the run
    directory that holds its entry.

    ``seal_run`` creates ``seals/`` the same way, but skips the fsyncs when the directory
    already exists. So if the desk created it with a plain ``mkdir`` (and the machine lost
    power before the run directory reached the disk), a later seal written into it could
    vanish with the directory. The fsyncs run on every call, not only on creation, so a
    desk killed between its ``mkdir`` and its fsync is repaired by the next call; an fsync
    of a directory with nothing new to write is cheap.
    """
    with contextlib.suppress(FileExistsError):
        seals.mkdir()
    _fsync_dir(seals)
    _fsync_dir(seals.parent)


@contextlib.contextmanager
def seal_guard(root: Path, run_id: str, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    """Hold the record's seal lock of ``run_id`` (``seal_run`` waits while it is held).

    Creates ``seals/`` if it is missing, durably (see ``_seals_dir_durable``), as
    ``seal_run`` itself does; the run directory must exist (``FileNotFoundError``).
    """
    timeout = check_timeout(timeout)
    path = seal_lock_path(root, run_id)
    _seals_dir_durable(path.parent)
    with _flock(path, run_id, timeout, "seal", 0o644):
        yield
