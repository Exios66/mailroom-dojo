"""Plain helpers shared by the desk tests (fixtures live in conftest.py).

Everything is synthetic: no real document text, no network, no model call. Records are
built with ``mailroom_record``'s own APIs on a temporary root. The process targets at the
bottom run in spawned children, so they live here (an importable module), not in a test
file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from mailroom_desk.models import ReviewRequest
from mailroom_desk.policy import Policy, parse_policy
from mailroom_record.blobs import BlobStore
from mailroom_record.log import EventLog
from mailroom_record.models import Event, Run

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)

# A synthetic policy. Tokens are made up; none is a real cause of any pipeline.
POLICY: dict[str, Any] = {
    "name": "synthetic",
    "description": "A synthetic policy for the desk tests.",
    "default_queue": "main",
    "causes": [
        {"token": "flag_high", "description": "High flag.", "priority": "high", "sla_hours": 4},
        {"token": "flag_normal", "description": "Normal flag.", "priority": "normal", "sla_hours": 24},
        {"token": "flag_low", "description": "Low flag.", "priority": "low", "sla_hours": 72},
        {"token": "flag_class", "description": "Class flag.", "priority": "normal", "sla_hours": 24},
        {"token": "flag_conf", "description": "Confidence flag.", "priority": "low", "sla_hours": 48},
        {"token": "slot:*:*", "description": "A slot pattern.", "priority": "low", "sla_hours": 72},
        {"token": "slot:exact:one", "description": "An exact slot.", "priority": "high", "sla_hours": 2},
        {"token": "spot_check", "description": "Sampled.", "priority": "low", "sla_hours": 72},
    ],
    "rules": [
        {
            "name": "high_to_senior",
            "when": {"cause_in": ["flag_high"]},
            "then": {"queue": "senior", "required_role": "senior"},
        },
        {
            "name": "class_and_low_conf",
            "when": {"cause_in": ["flag_class"], "class_in": ["kind_b"], "confidence_below": 0.5},
            "then": {"priority": "high", "sla_hours": 1, "queue": "combo", "prefill_ref": "prefill-combo"},
        },
        {"name": "class_only", "when": {"class_in": ["kind_a", "kind_b"]}, "then": {"queue": "by_class"}},
        {
            "name": "low_conf",
            "when": {"confidence_below": 0.3},
            "then": {"priority": "normal", "sla_hours": 12},
        },
        {"name": "slots", "when": {"cause_in": ["slot:*:*"]}, "then": {"queue": "slots"}},
        {"name": "sampled", "when": {"cause_in": ["spot_check"]}, "then": {"queue": "qa"}},
        {"name": "default", "then": {}},
    ],
    "sample": {"rate": 0.1, "seed": "synthetic-seed"},
}


def policy_dict(**overrides: Any) -> dict[str, Any]:
    """A deep copy of ``POLICY`` with top-level keys replaced."""
    raw = yaml.safe_load(yaml.safe_dump(POLICY))
    raw.update(overrides)
    return raw


def policy(**overrides: Any) -> Policy:
    return parse_policy(policy_dict(**overrides))


def write_yaml(path: Path, data: Any) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def run_header(run_id: str = "r1", kind: str = "specialist_only") -> Run:
    return Run(
        run_id=run_id,
        kind=kind,
        backend="mock",
        origin="native",
        policy_name=None,
        policy_hash=None,
        prompt_set=None,
        prompt_hashes={},
        agent_models={},
        code_version=None,
        label_snapshot_ids=[],
        document_ids=[],
        caps={},
        signed_by=None,
        created_at="2026-11-18T12:00:00Z",
        source={},
    )


def make_run(root: Path, run_id: str = "r1", kind: str = "specialist_only") -> EventLog:
    log = EventLog(Path(root), BlobStore(Path(root)))
    log.create_run(run_header(run_id, kind))
    return log


def log_of(root: Path) -> EventLog:
    return EventLog(Path(root), BlobStore(Path(root)))


def engine_append(
    root: Path,
    run_id: str,
    doc_id: str,
    kind: str = "decision",
    *,
    payload: dict | None = None,
    node: str = "step",
    writer_id: str = "w0",
    ts: str | None = None,
) -> Event:
    """Append one non-desk event with its own writer, flushed and closed (as an engine would)."""
    with log_of(root).open_writer(run_id, writer_id) as w:
        return w.append(doc_id=doc_id, node=node, kind=kind, payload=payload, ts=ts)


def request(doc_id: str = "d1", *causes: str, run_id: str = "r1", **kw: Any) -> ReviewRequest:
    return ReviewRequest(
        run_id=run_id, doc_id=doc_id, causes=causes, requested_by=kw.pop("requested_by", "tester"), **kw
    )


def doc_events(root: Path, run_id: str, doc_id: str) -> list[Event]:
    return list(log_of(root).iter_events(run_id, doc_id))


def review_events(root: Path, run_id: str = "r1") -> list[Event]:
    return [e for e in log_of(root).iter_events(run_id) if e.kind == "review"]


def segment_files(root: Path, run_id: str = "r1") -> dict[str, bytes]:
    d = Path(root) / "runs" / run_id / "segments"
    return {p.name: p.read_bytes() for p in sorted(d.glob("*.jsonl"))}


def seal_within(log: EventLog, run_id: str, seconds: float = 30.0):
    """``seal_run`` with a deadline. ``seal_run`` waits for the seal lock without a timeout,
    so a lock the desk failed to release would hang the suite; here it fails the test."""
    import threading

    from mailroom_record.seal import seal_run

    out: list = []
    t = threading.Thread(target=lambda: out.append(seal_run(log, run_id)), daemon=True)
    t.start()
    t.join(seconds)
    assert out, f"seal_run of {run_id!r} did not finish within {seconds} s (a seal lock is held)"
    return out[0]


class FakeClock:
    """An injectable desk clock that only moves when told to."""

    def __init__(self, start: datetime = T0) -> None:
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta: float) -> datetime:
        self.moment += timedelta(**delta)
        return self.moment


# --- process targets (spawned children) ---------------------------------------------


class _SlowClock:
    """A clock that waits ``pause`` seconds when read: the desk reads it under its lock,
    after the in-review check and before the append, so this widens exactly that window."""

    def __init__(self, pause: float) -> None:
        self.pause = pause

    def __call__(self) -> datetime:
        import time

        time.sleep(self.pause)
        return datetime.now(UTC)


def enqueue_in_child(
    root: str, run_id: str, doc_ids: list[str], barrier: Any, results: Any, pause: float
) -> None:
    """Wait at ``barrier``, then enqueue each document; put ``(doc_id, outcome)`` on ``results``."""
    from mailroom_desk import AlreadyInReview, Desk

    desk = Desk(Path(root), clock=_SlowClock(pause), lock_timeout=20)
    pol = policy(sample={"rate": 0.0, "seed": ""})
    barrier.wait()
    for doc_id in doc_ids:
        try:
            desk.enqueue(request(doc_id, "flag_low", run_id=run_id), pol)
            results.put((doc_id, "enqueued"))
        except AlreadyInReview:
            results.put((doc_id, "already"))
        except Exception as exc:  # noqa: BLE001 - reported to the parent, which fails the test
            results.put((doc_id, f"error {type(exc).__name__}: {exc}"))


def stall_in_child(
    root: str, run_id: str, doc_ids: list[str], barrier: Any, results: Any, pause: float
) -> None:
    """``enqueue_in_child``'s signature, but it never reports: it waits at ``barrier`` and
    hangs, so the parent's result collection times out (the reaping test)."""
    import time

    barrier.wait()
    time.sleep(600)


class _MarkerClock:
    """A clock that creates ``marker`` and then hangs: the parent kills the process while
    it holds the desk lock (and the seal lock), between the check and the append."""

    def __init__(self, marker: str) -> None:
        self.marker = marker

    def __call__(self) -> datetime:
        import time

        Path(self.marker).write_text("holding the lock", encoding="utf-8")
        time.sleep(600)
        raise AssertionError("never reached: the parent kills this process")


def enqueue_then_hang(root: str, run_id: str, doc_id: str, marker: str) -> None:
    from mailroom_desk import Desk

    desk = Desk(Path(root), clock=_MarkerClock(marker), lock_timeout=60)
    desk.enqueue(request(doc_id, "flag_low", run_id=run_id), policy(sample={"rate": 0.0, "seed": ""}))


def hold_desk_lock(root: str, run_id: str, marker: str) -> None:
    """Take the desk lock, create ``marker``, and hang until killed."""
    import time

    from mailroom_desk.lock import desk_lock

    with desk_lock(Path(root), run_id, timeout=10):
        Path(marker).write_text("held", encoding="utf-8")
        time.sleep(600)


def engine_writes_across_a_handoff(root: str, run_id: str, doc_id: str, marker: str, go: str) -> None:
    """A writer in another process that breaks the one-owner rule: it appends to ``doc_id``
    (buffered, after reading the head), creates ``marker``, waits for ``go`` to exist, then
    flushes and closes. If the desk wrote the document meanwhile, the chain forks."""
    import time

    with log_of(Path(root)).open_writer(run_id, "engine") as w:
        w.append(doc_id=doc_id, node="step", kind="decision")
        Path(marker).write_text("buffered", encoding="utf-8")
        deadline = time.monotonic() + 60
        while not Path(go).exists():
            if time.monotonic() > deadline:
                raise TimeoutError("the parent never said go")
            time.sleep(0.01)
