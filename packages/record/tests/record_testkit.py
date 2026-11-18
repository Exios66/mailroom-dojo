"""Plain helpers shared by the record tests (fixtures live in conftest.py).

Everything here is synthetic: no real document text, no network, no model call.
"""

from __future__ import annotations

from typing import Any

from mailroom_record.models import GENESIS, BlobRef, Event, event_hash

SENTINEL = "SECRET-DOCUMENT-TEXT"

DOC_A = "a" * 64
SHA_X = "b" * 64


def v1_event_fields(**overrides: Any) -> dict[str, Any]:
    """Every Event field except ``hash``, at schema version 1, with every optional set."""
    fields: dict[str, Any] = {
        "schema_version": 1,
        "run_id": "r1",
        "doc_id": DOC_A,
        "seq": 0,
        "ts": "2026-11-18T12:00:00.000000Z",
        "node": "classify",
        "agent": "sorter",
        "attempt": 0,
        "kind": "decision",
        "model": "test-model",
        "prompt_id": "sorter@v1",
        "tokens_in": 120,
        "tokens_out": 30,
        "tokens_cached": 0,
        "price_basis": "list",
        "latency_ms": 812.5,
        "decision": {"chosen": "contract", "confidence": 0.91},
        "cause": None,
        "inputs": [{"sha256": SHA_X, "role": "transcript", "size": 10}],
        "outputs": [],
        "payload": {"route": "extract"},
        "prev_hash": GENESIS,
    }
    fields.update(overrides)
    return fields


def make_event(**overrides: Any) -> Event:
    """A valid current-version Event whose hash is computed by ``Event.build``."""
    return Event.build(**v1_event_fields(**overrides))


def v1_event_dict(**overrides: Any) -> dict[str, Any]:
    """A stored v1 event line as a dict, ``hash`` included."""
    return make_event(**overrides).model_dump(mode="json")


def stored_v0_event_dict(**overrides: Any) -> dict[str, Any]:
    """A stored v0 event line: no ``tokens_cached``, hash computed over that v0 form."""
    raw = v1_event_fields(**overrides) | {"schema_version": 0}
    raw.pop("tokens_cached")
    raw["hash"] = event_hash(raw)
    return raw


def run_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "run_id": "r1",
        "kind": "specialist_only",
        "backend": "mock",
        "origin": "native",
        "policy_name": None,
        "policy_hash": None,
        "prompt_set": "deploy_v1",
        "prompt_hashes": {"sorter": "c" * 64},
        "agent_models": {"sorter": "test-model"},
        "code_version": None,
        "label_snapshot_ids": [],
        "document_ids": [DOC_A],
        "caps": {"usd": 1.5},
        "signed_by": None,
        "created_at": "2026-11-18T12:00:00Z",
        "source": {},
    }
    fields.update(overrides)
    return fields


def seal_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "run_id": "r1",
        "n": 0,
        "sealed_at": "2026-11-18T13:00:00Z",
        "doc_heads": {DOC_A: "d" * 64},
        "document_ids": [DOC_A],
        "event_count": 3,
        "prev_seal_hash": GENESIS,
    }
    fields.update(overrides)
    return fields


def blob_ref(**overrides: Any) -> BlobRef:
    return BlobRef(**({"sha256": SHA_X, "role": "original", "size": 3} | overrides))


def put_in_child(root: str, data: bytes, role: str, rounds: int) -> str:
    """Process target: put the same bytes ``rounds`` times into a store at ``root``."""
    from pathlib import Path

    from mailroom_record.blobs import BlobStore

    store = BlobStore(Path(root))
    sha = ""
    for _ in range(rounds):
        sha = store.put(data, role).sha256
    return sha


# --- event log helpers (Task 5) -----------------------------------------------------


def segment_lines(root: Any, run_id: str = "r1") -> list[bytes]:
    """Every whole line (newline included) of every segment of a run, in segment order."""
    from pathlib import Path

    out: list[bytes] = []
    for seg in sorted((Path(root) / "runs" / run_id / "segments").glob("*.jsonl")):
        data = seg.read_bytes()
        end = data.rfind(b"\n") + 1
        out.extend(line + b"\n" for line in data[:end].split(b"\n")[:-1])
    return out


def check_chains(root: Any, run_id: str = "r1") -> dict[str, int]:
    """Check every whole stored line: hash, link and seq per document. Returns doc -> count.

    Hashes are checked on the stored dict (ADR-0005), never on a model.
    """
    import json

    by_doc: dict[str, list[dict[str, Any]]] = {}
    for line in segment_lines(root, run_id):
        raw = json.loads(line)
        assert event_hash(raw) == raw["hash"], f"bad hash at {raw['doc_id']}/{raw['seq']}"
        by_doc.setdefault(raw["doc_id"], []).append(raw)
    for doc_id, events in by_doc.items():
        events.sort(key=lambda r: r["seq"])
        assert [r["seq"] for r in events] == list(range(len(events))), f"seq gap in {doc_id}"
        assert events[0]["prev_hash"] == GENESIS
        for prev, cur in zip(events, events[1:]):
            assert cur["prev_hash"] == prev["hash"], f"broken link in {doc_id} at {cur['seq']}"
    return {d: len(e) for d, e in by_doc.items()}


def append_in_child(root: str, run_id: str, writer_id: str, docs: list[str], n: int) -> None:
    """Process target: one writer appends ``n`` events to each of ``docs``, then closes."""
    from pathlib import Path

    from mailroom_record.blobs import BlobStore
    from mailroom_record.log import EventLog

    log = EventLog(Path(root), BlobStore(Path(root)))
    with log.open_writer(run_id, writer_id, flush_every=7) as w:
        for i in range(n):
            for doc in docs:
                w.append(doc_id=doc, node=f"n{i}", kind="decision", payload={"i": i})


def append_until_killed(root: str, run_id: str, writer_id: str, docs: list[str], flush_every: int) -> None:
    """Process target: append large events forever, round-robin over ``docs``.

    Each line carries ~1 MB of synthetic padding, so with a large ``flush_every`` each
    flush is one long write and a SIGKILL usually lands in the middle of it (a torn tail).
    """
    from pathlib import Path

    from mailroom_record.blobs import BlobStore
    from mailroom_record.log import EventLog

    log = EventLog(Path(root), BlobStore(Path(root)))
    w = log.open_writer(run_id, writer_id, flush_every=flush_every)
    pad = "z" * (1024 * 1024)
    i = 0
    while True:
        w.append(doc_id=docs[i % len(docs)], node="loop", kind="handoff", payload={"i": i, "pad": pad})
        i += 1


# --- seal helpers (Task 6) ----------------------------------------------------------


def append_more(store: Any, doc_id: str, n: int, writer_id: str = "w-more") -> list[Any]:
    """Append ``n`` events to ``doc_id`` with a fresh writer, close it, return the events."""
    with store.log.open_writer("r1", writer_id) as w:
        return [w.append(doc_id=doc_id, node=f"more{i}", kind="handoff") for i in range(n)]


def seal_in_child(root: str, run_id: str, rounds: int) -> list[int]:
    """Process target: seal ``run_id`` ``rounds`` times, return the seal numbers seen."""
    from pathlib import Path

    from mailroom_record.blobs import BlobStore
    from mailroom_record.log import EventLog
    from mailroom_record.seal import seal_run

    log = EventLog(Path(root), BlobStore(Path(root)))
    return [seal_run(log, run_id).n for _ in range(rounds)]


# --- fault injection (JJB's bar, the fault matrix) -----------------------------------


def _make_raw_class() -> type:
    import io
    import os

    class OsWriteRaw(io.RawIOBase):
        """A raw file that writes through ``os.write`` exactly like ``FileIO`` does.

        ``FileIO.write`` is a thin wrapper over write(2) that does not go through the
        ``os`` module, so ``os.write`` patches would not reach it. Files opened with
        ``os.fdopen(fd, "wb")`` under a faulted store get this raw layer under the usual
        ``BufferedWriter``, so an injected short write or ``ENOSPC`` reaches the stack
        (buffering, zstd, pyarrow) the way a real one would.
        """

        def __init__(self, fd: int) -> None:
            self._fd = fd

        def writable(self) -> bool:
            return True

        def seekable(self) -> bool:
            return True

        def fileno(self) -> int:
            return self._fd

        def write(self, b: Any) -> int:
            return os.write(self._fd, b)

        def seek(self, pos: int, whence: int = 0) -> int:
            return os.lseek(self._fd, pos, whence)

        def tell(self) -> int:
            return os.lseek(self._fd, 0, os.SEEK_CUR)

        def close(self) -> None:
            if not self.closed:
                try:
                    os.close(self._fd)
                finally:
                    super().close()

    return OsWriteRaw


class Faults:
    """Count, and on request fail, the syscalls the record makes under one store root.

    Patches ``os.write``, ``os.fsync``, ``os.replace`` and ``os.link``; only calls that
    touch a path under ``root`` are counted or faulted, so pytest's own I/O is untouched.
    ``fail(name, k, action)`` makes the ``k``-th (1-based) such call of ``name`` run
    ``action`` instead: ``"ENOSPC"`` or ``"EIO"`` (raise, nothing written), ``"short"``
    (``os.write`` writes just over half and returns the count), ``"zero"`` (``os.write``
    returns 0), or ``"short+ENOSPC"`` (a short write, then ENOSPC on the next write).
    """

    NAMES = ("write", "fsync", "replace", "link")

    def __init__(self, monkeypatch: Any, root: Any) -> None:
        import errno
        import io
        import os
        from collections import Counter

        self.root = os.path.realpath(root)
        self.calls: Counter[str] = Counter()
        self.plan: dict[tuple[str, int], str] = {}
        self.fired: list[tuple[str, int, str]] = []
        self._errno = {"ENOSPC": errno.ENOSPC, "EIO": errno.EIO}
        self._real = {n: getattr(os, n) for n in self.NAMES}
        real_fdopen = os.fdopen
        raw_cls = _make_raw_class()
        self._enospc_next = False

        def under_root(path: str) -> bool:
            return path == self.root or path.startswith(self.root + os.sep)

        def ours_fd(fd: int) -> bool:
            try:
                return under_root(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                return False

        def ours_path(p: Any) -> bool:
            return under_root(os.path.realpath(os.fspath(p)))

        def fault(name: str) -> str | None:
            self.calls[name] += 1
            action = self.plan.get((name, self.calls[name]))
            if action is not None:
                self.fired.append((name, self.calls[name], action))
            return action

        def raise_for(action: str, name: str) -> None:
            code = self._errno[action]
            raise OSError(code, f"injected {action} in os.{name}")

        def write(fd: int, data: Any) -> int:
            if not ours_fd(fd):
                return self._real["write"](fd, data)
            if self._enospc_next:
                self._enospc_next = False
                self.calls["write"] += 1
                self.fired.append(("write", self.calls["write"], "ENOSPC"))
                raise_for("ENOSPC", "write")
            action = fault("write")
            if action in ("ENOSPC", "EIO"):
                raise_for(action, "write")
            if action == "zero":
                return 0
            if action in ("short", "short+ENOSPC"):
                view = memoryview(data).cast("B")
                if action == "short+ENOSPC":
                    self._enospc_next = True
                # Just past half, so a buffer of equal lines is not cut on a line boundary.
                cut = min(len(view) // 2 + 1, max(1, len(view) - 1))
                return self._real["write"](fd, view[:cut])
            return self._real["write"](fd, data)

        def fsync(fd: int) -> None:
            if ours_fd(fd):
                action = fault("fsync")
                if action in ("ENOSPC", "EIO"):
                    raise_for(action, "fsync")
            return self._real["fsync"](fd)

        def two_paths(name: str) -> Any:
            def call(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
                if ours_path(src) or ours_path(dst):
                    action = fault(name)
                    if action in ("ENOSPC", "EIO"):
                        raise_for(action, name)
                return self._real[name](src, dst, *args, **kwargs)

            return call

        def fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if mode == "wb" and not args and not kwargs and ours_fd(fd):
                return io.BufferedWriter(raw_cls(fd))
            return real_fdopen(fd, mode, *args, **kwargs)

        monkeypatch.setattr(os, "write", write)
        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", two_paths("replace"))
        monkeypatch.setattr(os, "link", two_paths("link"))
        monkeypatch.setattr(os, "fdopen", fdopen)

    def use_root(self, root: Any) -> None:
        """Count and fault calls under ``root`` from now on (a fresh store per case)."""
        import os

        self.root = os.path.realpath(root)
        self.reset()

    def fail(self, name: str, k: int, action: str) -> None:
        self.plan[(name, k)] = action

    def reset(self) -> None:
        self.calls.clear()
        self.plan.clear()
        self.fired.clear()
        self._enospc_next = False


def tmp_files(root: Any) -> list[str]:
    """Every leftover temp file (``*.tmp``) under ``root``."""
    from pathlib import Path

    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*.tmp"))
