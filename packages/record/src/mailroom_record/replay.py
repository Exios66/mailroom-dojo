"""The floor replay timeline of a run, rebuilt from the record alone (Task 10b, spec R7).

``replay_timeline(log, run_id)`` returns ``(timeline, meta)`` under the contract of the
sandbox's recorded replay (``tui/floor_replay.py:build_timeline``), so any stored or
imported run can be played on the floor without its source folder:

- ``timeline`` is a sorted list of ``(at_seconds, event)``; ``event`` is ``{"node": row}``,
  ``{"item": row}`` or ``{"checkpoint": {"state", "cursor", "total"}}``. A checkpoint
  ``running`` opens it at 0 and the run's final checkpoint closes it 0.5 s after the last
  row. Times are seconds from the earliest node or item time, rounded to 0.1 ms.
- ``meta`` holds ``source_run_id``, ``engine_kind``, ``engine_model``, ``profile``,
  ``node_records``, ``item_records``, ``duration_s``, ``synthetic_timing``, ``incomplete``,
  ``source_state`` and ``warnings``.

What each event becomes is read from ``payload.floor``, the role the writer gave it:

- ``node`` (and any event of a document without ``floor``, as a natively written run has):
  a node row ``{doc, node, status, agent, model, step, stage, ms, decisions}`` at the
  event's ``ts``. When the event carries ``started_ts`` (or ``started_order``), a
  ``running`` row ``{doc, node, status, agent, model, step}`` comes first at that time. An
  event whose ``status`` is ``running`` (in flight when the run stopped) is one ``running``
  row. A native event's status is ``error`` for ``kind="error"``, else ``done``; an imported
  event keeps its row's status, ``None`` when the row had none (as the sandbox shows it). A
  number or a bool is shown as stored; a status that was free text was stored as its
  SHA-256 and length, so it cannot be restored and shows as ``None`` (the sandbox shows the text).
- ``item``: an item row with the document's outcome (``ok``, ``error`` as the cause token,
  ``latency_ms``, token totals and the decision's outcome fields: ``doc_type``, ``stage``,
  both confidences, ``review_causes``, ``review_verdict``, ``escalation_reason``). A
  document's later item replaces its earlier one, as in the sandbox.
- ``checkpoint`` (on ``_run``): the run's final ``state``, ``cursor`` and ``total``; the
  last one wins.
- ``draw`` gives the document's source id (``doc`` in the rows); ``lifecycle`` and any
  other ``_run`` event are not on the floor.

Rows are put in the source's order by ``payload.order`` (``started_order`` for a start), as
the importer records it; rows without one (a native run) follow by time, then document and
``seq``. From there the timing rules are the sandbox's own, step for step: a row's time
places it when known; an event marked ``ts_absent`` has no time, and a run with no time at
all falls back to document order, labelled ``synthetic_timing``. An untimed start lands at
the latest row; an untimed finish lands at its start plus ``ms`` when the two pair by
``(doc, node, step)``, the finish's step being its own row's (``payload.finish_step``) or
none, as in the sandbox, which writes ``step`` on running rows only (a step stored
scrubbed, free text kept as its SHA-256 and length, pairs by its canonical JSON); else 0.1 s after the
latest row. An untimed item lands at its document's last finish, else 0.5 s after the
latest row. Only ``done`` and
``failed`` final states count as complete; ``cancelled`` replays as ``failed``.

No document text and no extracted value is in the timeline: rows are built from a fixed
list of fields, decisions lose any ``options`` entry, and blobs are never read. Raises
``FileNotFoundError`` for an unknown run and ``ValueError`` for a run with no node or item
row, or a timeline longer than seven days (the sandbox's limits).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mailroom_record.hashing import canonical_json
from mailroom_record.log import EventLog
from mailroom_record.models import Event

__all__ = ["MAX_TIMELINE_S", "replay_timeline"]

MAX_TIMELINE_S = 7 * 24 * 3600
_OUTCOME_KEYS = (
    "doc_type",
    "stage",
    "classification_confidence",
    "extraction_confidence",
    "review_causes",
    "review_verdict",
    "escalation_reason",
)
_ITEM_PAYLOAD_KEYS = ("prompt_tokens", "completion_tokens")
_COMPLETE = frozenset({"done", "failed"})
_KEPT = frozenset({"prepared", "running", "paused"})


def _seconds(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _pairable(step: Any) -> Any:
    """``step`` as a dict key: a step stored scrubbed (a dict, or a list) by its canonical JSON,
    so two rows naming the same text step still pair; anything else as stored."""
    return canonical_json(step) if isinstance(step, (dict, list)) else step


def _decisions(decision: Any) -> list[Any]:
    if decision is None:
        return []
    items = decision if isinstance(decision, list) else [decision]
    return [{k: v for k, v in d.items() if k != "options"} if isinstance(d, dict) else d for d in items]


class _Rows:
    """Node and item rows rebuilt from events, each with its time (or ``None``) and order key."""

    def __init__(self) -> None:
        # (order key, time, row, doc id, the step the timing pairs it by)
        self.nodes: list[tuple[tuple[Any, ...], float | None, dict[str, Any], str, Any]] = []
        self.items: list[tuple[tuple[Any, ...], float | None, dict[str, Any], str]] = []
        self.checkpoint: dict[str, Any] = {}
        self.source: dict[str, str] = {}
        self.untimed = 0

    def _key(self, order: Any, e: Event, t: float | None) -> tuple[Any, ...]:
        o = _int(order)
        return (0, o) if o is not None else (1, t if t is not None else float("inf"), e.doc_id, e.seq)

    def _time(self, e: Event) -> float | None:
        if e.payload.get("ts_absent") is True:
            self.untimed += 1
            return None
        return _seconds(e.ts)

    def add(self, e: Event) -> None:
        floor = e.payload.get("floor")
        if e.doc_id == "_run":
            if floor == "checkpoint":
                self.checkpoint = {k: e.payload.get(k) for k in ("state", "cursor", "total")}
            return
        if floor == "draw":
            sid = e.payload.get("source_id")
            if isinstance(sid, str) and sid:
                self.source[e.doc_id] = sid
        elif floor == "item":
            self._item(e)
        elif floor in (None, "node"):
            self._node(e)

    def _node(self, e: Event) -> None:
        p = e.payload
        status = p.get("status")
        if p.get("floor") != "node":  # a native event: its status, else its kind says how it ended
            if not isinstance(status, str) or not status:
                status = "error" if e.kind == "error" else "done"
        elif isinstance(status, (dict, list)):
            status = None  # free text, stored as its SHA-256 and length: the sandbox's text is lost
        base = {"doc": e.doc_id, "node": e.node, "agent": e.agent, "model": e.model, "step": p.get("step")}
        pair = _pairable(base["step"])
        if status == "running":
            t = self._time(e)
            self.nodes.append((self._key(p.get("order"), e, t), t, base | {"status": "running"}, e.doc_id, pair))
            return
        if "started_ts" in p or "started_order" in p:
            started = p.get("started_ts")
            st = _seconds(started) if isinstance(started, str) else None
            if st is None:
                self.untimed += 1
            start = base | {"status": "running"}
            self.nodes.append((self._key(p.get("started_order"), e, st), st, start, e.doc_id, pair))
        t = self._time(e)
        row = base | {
            "status": status,
            "stage": p.get("stage"),
            "ms": e.latency_ms,
            "decisions": _decisions(e.decision),
        }
        # The sandbox pairs a finish with its start by (doc, node, step), and writes ``step``
        # on running rows only: a finish pairs by step only when its own row named one.
        pair_step = pair if p.get("finish_step") is True else None
        self.nodes.append((self._key(p.get("order"), e, t), t, row, e.doc_id, pair_step))

    def _item(self, e: Event) -> None:
        p = e.payload
        decision = e.decision if isinstance(e.decision, dict) else {}
        row: dict[str, Any] = {
            "ok": p.get("ok", e.kind != "error"),
            "error": e.cause,
            "latency_ms": e.latency_ms,
            **{k: p.get(k) for k in _ITEM_PAYLOAD_KEYS},
            **{k: decision.get(k) for k in _OUTCOME_KEYS},
        }
        t = self._time(e)
        self.items.append((self._key(p.get("order"), e, t), t, row, e.doc_id))


def replay_timeline(log: EventLog, run_id: str) -> tuple[list[tuple[float, dict[str, Any]]], dict[str, Any]]:
    """The run's floor replay timeline and its metadata (see the module doc)."""
    run = log.read_run(run_id)
    rows = _Rows()
    for e in log.iter_events(run_id):
        rows.add(e)
    if not rows.nodes and not rows.items:
        raise ValueError(f"run {run_id!r} has no node or item records to replay")

    def doc(doc_id: str) -> str:
        return rows.source.get(doc_id, doc_id)

    nodes = [(t, r | {"doc": doc(d)}, s) for _, t, r, d, s in sorted(rows.nodes, key=lambda x: x[0])]
    items = [(t, r | {"item_id": doc(d), "doc": doc(d)}) for _, t, r, d in sorted(rows.items, key=lambda x: x[0])]

    # From here on: the sandbox's build_timeline rules, applied to the rebuilt rows.
    times = [t for t, *_ in [*nodes, *items] if t is not None]
    synthetic = not times
    t0 = min(times) if times else 0.0
    cursor = 0.0
    sequence = 0
    events: list[tuple[float, int, dict[str, Any]]] = []
    running: dict[tuple[str, str, Any], float] = {}
    last_done: dict[str, float] = {}
    for stamp, row, pair_step in nodes:
        status = row["status"]
        key = (row["doc"], row["node"], pair_step)
        if stamp is not None and not synthetic:
            at = max(0.0, stamp - t0)
        elif status == "running":
            at = cursor
        elif key in running:
            at = running[key] + max(0.0, float(row.get("ms") or 0)) / 1000.0
        else:
            at = cursor + 0.1
        if status == "running":
            running[key] = at
        else:
            at = max(at, running.pop(key, at))
            last_done[row["doc"]] = max(last_done.get(row["doc"], at), at)
        cursor = max(cursor, at)
        events.append((at, sequence, {"node": row}))
        sequence += 1

    item_at: dict[str, tuple[float, int, dict[str, Any]]] = {}
    for stamp, row in items:
        if stamp is not None and not synthetic:
            at = max(0.0, stamp - t0)
        elif row["doc"] in last_done:
            at = last_done[row["doc"]]
        else:
            at = cursor + 0.5
        cursor = max(cursor, at)
        item_at[row["item_id"]] = (at, sequence, {"item": row})
        sequence += 1

    checkpoint = rows.checkpoint
    total = _int(checkpoint.get("total"))
    if total is None or total < 0:
        total = len(item_at)
    timeline: list[tuple[float, int, dict[str, Any]]] = [
        (0.0, -1, {"checkpoint": {"state": "running", "cursor": 0, "total": total}}),
        *events,
        *item_at.values(),
    ]
    source_state = checkpoint.get("state")
    if source_state == "cancelled":
        final_state = "failed"
    elif source_state in _KEPT or source_state in _COMPLETE:
        final_state = source_state
    else:
        final_state = "paused"
    final_cursor = _int(checkpoint.get("cursor"))
    if final_cursor is None:
        final_cursor = len(item_at)
    timeline.append(
        (cursor + 0.5, sequence + 1, {"checkpoint": {"state": final_state, "cursor": final_cursor, "total": total or len(item_at)}})
    )
    timeline.sort(key=lambda r: (r[0], r[1]))
    duration = timeline[-1][0]
    if duration > MAX_TIMELINE_S:
        raise ValueError(f"run {run_id!r}: the timeline exceeds 7 days ({duration:.0f}s)")
    warnings = [f"{rows.untimed} rows had no source time"] if rows.untimed else []
    meta = {
        "source_run_id": run.source.get("source_run_id") or run.run_id,
        "engine_kind": run.backend,
        "engine_model": run.source.get("engine_model"),
        "profile": run.source.get("profile"),
        "node_records": len(nodes),
        "item_records": len(item_at),
        "duration_s": round(duration, 3),
        "synthetic_timing": synthetic,
        "incomplete": source_state not in _COMPLETE,
        "source_state": source_state or "missing",
        "warnings": warnings,
    }
    return [(round(at, 4), event) for at, _, event in timeline], meta
