"""Fault injection for blobs, the log and seals (docs/test-strategy-record.md, fault matrix).

Each test first runs an operation cleanly to count the syscalls it makes under the store
root, then repeats it on a fresh store once per (syscall, call number, fault) and asserts
the exact outcome: a clear ``OSError`` (or completion, for a short write the code must
retry), nothing partial at a final path, no temp file left, a failed log writer refusing
further use, and an on-disk state that verifies and recovers when used again.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow.parquet as pq
import pytest
from record_testkit import Faults, check_chains, run_fields, tmp_files

from mailroom_record.blobs import BlobStore, BlobTombstoned
from mailroom_record.log import EventLog, TornTail
from mailroom_record.models import Run, Seal, seal_hash
from mailroom_record.seal import list_seals, seal_run

RAISING = {"ENOSPC", "EIO", "short+ENOSPC", "zero"}
# Incompressible synthetic bytes, so the compressed blob spans several raw writes.
DATA = random.Random(0).randbytes(48 * 1024)
ROLE = "original"


@pytest.fixture
def faults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Faults:
    return Faults(monkeypatch, tmp_path)


def new_store(tmp_path: Path, name: str) -> SimpleNamespace:
    root = tmp_path / name
    root.mkdir()
    blobs = BlobStore(root)
    log = EventLog(root, blobs)
    log.create_run(Run(**run_fields()))
    return SimpleNamespace(root=root, blobs=blobs, log=log)


def cases(
    faults: Faults, tmp_path: Path, setup: Callable[[SimpleNamespace], Any], op: Callable[[SimpleNamespace, Any], Any],
    actions: dict[str, list[str]],
) -> Iterator[tuple[str, int, str, SimpleNamespace, Any]]:
    """Yield ``(syscall, k, action, store, setup_result)`` for every fault point of ``op``."""
    clean = new_store(tmp_path, "clean")
    ctx = setup(clean)
    faults.use_root(clean.root)
    op(clean, ctx)
    counts = dict(faults.calls)
    assert counts.get("fsync"), "the clean run made no fsync: the counter does not see the store"
    i = 0
    for name, n in sorted(counts.items()):
        for k in range(1, n + 1):
            for action in actions.get(name, []):
                i += 1
                store = new_store(tmp_path, f"case{i}")
                ctx = setup(store)
                faults.use_root(store.root)
                faults.fail(name, k, action)
                yield name, k, action, store, ctx


def run_op(faults: Faults, op: Callable[[], Any]) -> BaseException | None:
    """Run ``op``; return the OSError it raised (None if it completed). Faults are off after."""
    err: BaseException | None = None
    try:
        op()
    except OSError as exc:
        err = exc
    fired = list(faults.fired)
    faults.plan.clear()
    raising = any(a in RAISING for _, _, a in fired)
    assert fired, "the planned fault never fired"
    if raising:
        assert err is not None, f"{fired}: the fault was swallowed and the call reported success"
        text = f"{err} | {err.__cause__} | {err.__context__}"
        assert "injected" in text or "wrote 0 bytes" in text, f"{fired}: an unrelated error {err!r}"
    else:
        assert err is None, f"{fired}: a short write must be completed, got {err!r}"
    return err


# --- blobs ------------------------------------------------------------------------

BLOB_ACTIONS = {"write": ["ENOSPC", "EIO", "short", "short+ENOSPC"], "fsync": ["ENOSPC", "EIO"],
                "replace": ["EIO"], "link": ["EIO"]}


def test_blob_put_under_every_fault(faults: Faults, tmp_path: Path) -> None:
    seen = 0
    for name, k, action, store, _ in cases(faults, tmp_path, lambda s: None,
                                           lambda s, _: s.blobs.put(DATA, ROLE), BLOB_ACTIONS):
        seen += 1
        where = f"{action} at os.{name} call {k}"
        err = run_op(faults, lambda: store.blobs.put(DATA, ROLE))
        assert tmp_files(store.root) == [], where
        sha = hashlib.sha256(DATA).hexdigest()
        state = store.blobs.exists(sha)
        if err is not None:
            assert state in ("missing", "present"), where
        if state == "present":
            assert store.blobs.get(sha) == DATA, where  # never a partial blob at its path
        # Recovery: the same put without faults stores the whole blob.
        assert store.blobs.put(DATA, ROLE).sha256 == sha
        assert store.blobs.get(sha) == DATA and tmp_files(store.root) == [], where
    assert seen >= 8


def test_blob_tombstone_under_every_fault(faults: Faults, tmp_path: Path) -> None:
    def setup(store: SimpleNamespace) -> str:
        return store.blobs.put(DATA, ROLE).sha256

    seen = 0
    for name, k, action, store, sha in cases(faults, tmp_path, setup,
                                             lambda s, sha: s.blobs.tombstone(sha, "erasure request"),
                                             BLOB_ACTIONS):
        seen += 1
        where = f"{action} at os.{name} call {k}"
        err = run_op(faults, lambda: store.blobs.tombstone(sha, "erasure request"))
        assert tmp_files(store.root) == [], where
        if store.blobs.tombstone_path(sha).exists():
            info = store.blobs.tombstone_info(sha)  # a whole record, never a partial one
            assert info["sha256"] == sha and info["reason"] == "erasure request", where
        else:
            assert err is not None and store.blobs.get(sha) == DATA, where
        store.blobs.tombstone(sha, "erasure request")  # recovery finishes the deletion
        assert store.blobs.exists(sha) == "tombstoned" and not store.blobs.content_path(sha).exists(), where
        with pytest.raises(BlobTombstoned):
            store.blobs.get(sha)
        with pytest.raises(BlobTombstoned):
            store.blobs.put(DATA, ROLE)
    assert seen >= 4


# --- the event log ------------------------------------------------------------------

LOG_ACTIONS = {"write": ["ENOSPC", "EIO", "short", "short+ENOSPC", "zero"], "fsync": ["ENOSPC", "EIO"]}
N_EVENTS, FLUSH_EVERY = 5, 2


def log_workload(store: SimpleNamespace, out: dict[str, Any]) -> None:
    """Open writer w0, append 5 events over d0/d1 with flush_every=2, close.

    ``out`` records the writer and the events known durable (covered by a flush that
    returned), so a test can check them after a fault.
    """
    out.update(writer=None, appended=[], durable=[])
    w = store.log.open_writer("r1", "w0", flush_every=FLUSH_EVERY)
    out["writer"] = w
    for i in range(N_EVENTS):
        out["appended"].append(w.append(doc_id=f"d{i % 2}", node=f"n{i}", kind="output"))
        if len(out["appended"]) % FLUSH_EVERY == 0:
            out["durable"] = list(out["appended"])
    w.close()
    out["durable"] = list(out["appended"])


def test_log_writer_under_every_fault(faults: Faults, tmp_path: Path) -> None:
    seen = 0
    for name, k, action, store, _ in cases(faults, tmp_path, lambda s: None,
                                           lambda s, _: log_workload(s, {}), LOG_ACTIONS):
        seen += 1
        where = f"{action} at os.{name} call {k}"
        out: dict[str, Any] = {}
        err = run_op(faults, lambda: log_workload(store, out))
        w = out["writer"]
        if err is not None and w is not None:
            # Poisoned (or closed by the failing close): it refuses further use.
            with pytest.raises(RuntimeError, match="failed earlier|is closed"):
                w.append(doc_id="d0", node="after", kind="output")
            with pytest.raises(RuntimeError, match="failed earlier|is closed"):
                w.flush()
            w.close()
            w.close()
        # On disk: every whole line verifies, chains are gap-free and linked.
        counts = check_chains(store.root)
        on_disk = {(e.doc_id, e.seq): e.hash for e in store.log.iter_events("r1")}
        for e in out["durable"]:
            assert on_disk.get((e.doc_id, e.seq)) == e.hash, f"{where}: a flushed event is missing"
        if err is None:
            assert len(on_disk) == N_EVENTS, where
        # Torn tails are reported exactly where the whole lines end.
        want = []
        for seg in sorted((store.root / "runs/r1/segments").glob("*.jsonl")):
            data = seg.read_bytes()
            if data and not data.endswith(b"\n"):
                want.append(TornTail(seg.name, data.rfind(b"\n") + 1))
        assert store.log.torn_tails("r1") == want, where
        if action == "short+ENOSPC" and name == "write":
            assert len(want) == 1, f"{where}: the cut line must be reported as a torn tail"
        # Recovery: the same writer id reopens and continues every chain.
        heads = store.log.doc_head_seqs("r1")
        with store.log.open_writer("r1", "w0", flush_every=1) as w2:
            if want:
                assert w2.segment.name == "w0.1.jsonl", where
            new = [w2.append(doc_id=d, node="recover", kind="output") for d in ("d0", "d1")]
        for e in new:
            assert e.seq == heads.get(e.doc_id, (-1, ""))[0] + 1, where
        after = check_chains(store.root)
        assert sum(after.values()) == sum(counts.values()) + 2, where
        assert seal_run(store.log, "r1").event_count == sum(after.values()), where
    assert seen >= 10


def test_log_short_writes_complete_whole_lines(faults: Faults, tmp_path: Path) -> None:
    # Every os.write returns after about half the bytes: the writer must loop.
    store = new_store(tmp_path, "s")
    faults.use_root(store.root)
    for k in range(1, 200):
        faults.fail("write", k, "short")
    out: dict[str, Any] = {}
    log_workload(store, out)
    assert len([f for f in faults.fired if f[2] == "short"]) > 3
    assert check_chains(store.root) == {"d0": 3, "d1": 2}
    assert store.log.torn_tails("r1") == []


def test_open_writer_fsync_error_releases_the_segment(faults: Faults, tmp_path: Path) -> None:
    store = new_store(tmp_path, "s")
    faults.use_root(store.root)
    faults.fail("fsync", 1, "EIO")
    with pytest.raises(OSError, match="injected EIO") as excinfo:
        store.log.open_writer("r1", "w0")
    assert excinfo.value is not None  # the traceback (and its frames) is still alive here
    with store.log.open_writer("r1", "w0") as w:  # not WriterInUse: the flock was released
        w.append(doc_id="d0", node="n", kind="output")
    assert check_chains(store.root) == {"d0": 1}


# --- seals ------------------------------------------------------------------------

SEAL_ACTIONS = BLOB_ACTIONS


def with_events(store: SimpleNamespace) -> None:
    with store.log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="classify", kind="decision", decision={"chosen": "contract"})
        w.append(doc_id="d1", node="extract", kind="output")
        w.append(doc_id="d2", node="classify", kind="decision")


def _seal_state(store: SimpleNamespace, where: str) -> None:
    """Whatever is at a final path is whole: a valid seal that rehashes, a readable Parquet."""
    seal_path = store.root / "runs/r1/seals/0.json"
    if seal_path.exists():
        raw = json.loads(seal_path.read_bytes())
        assert seal_hash(raw) == raw["hash"] and Seal.from_stored(raw).event_count == 3, where
    cols = store.root / "runs/r1/columns/events-0.parquet"
    if cols.exists():
        assert seal_path.exists(), f"{where}: a Parquet file without its seal"
        assert pq.read_table(cols).num_rows == 3, where


def test_seal_under_every_fault(faults: Faults, tmp_path: Path) -> None:
    seen = 0
    for name, k, action, store, _ in cases(faults, tmp_path, with_events,
                                           lambda s, _: seal_run(s.log, "r1"), SEAL_ACTIONS):
        seen += 1
        where = f"{action} at os.{name} call {k}"
        run_op(faults, lambda: seal_run(store.log, "r1"))
        assert tmp_files(store.root) == [], where
        _seal_state(store, where)
        # Recovery: sealing again commits seal 0 (or rebuilds its lost Parquet file).
        s0 = seal_run(store.log, "r1")
        assert (s0.n, s0.event_count) == (0, 3), where
        assert [s.n for s in list_seals(store.log, "r1")] == [0], where
        rows = pq.read_table(store.root / "runs/r1/columns/events-0.parquet").to_pylist()
        assert sorted((r["doc_id"], r["seq"]) for r in rows) == [("d1", 0), ("d1", 1), ("d2", 0)], where
        assert seal_run(store.log, "r1") == s0 and tmp_files(store.root) == [], where
    assert seen >= 10


def test_log_write_that_never_progresses_fails_instead_of_spinning(faults: Faults, tmp_path: Path) -> None:
    store = new_store(tmp_path, "s")
    w = store.log.open_writer("r1", "w0", flush_every=100)
    w.append(doc_id="d0", node="n", kind="output")
    faults.use_root(store.root)
    for k in range(1, 10_000):
        faults.fail("write", k, "zero")
    with pytest.raises(OSError, match="wrote 0 bytes"):
        w.flush()
    assert len(faults.fired) == 1
    with pytest.raises(RuntimeError, match="failed earlier"):
        w.append(doc_id="d0", node="n", kind="output")
    w.close()
