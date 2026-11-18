"""Task 10b: the floor replay timeline, rebuilt from the record alone.

The reference is the sandbox's ``tui/floor_replay.py:build_timeline`` contract. Two checks
stand for it: an expected timeline computed by hand from the fixture's rows (always run),
and, when a sandbox checkout is named by ``MAILROOM_SANDBOX_CHECKOUT``, the sandbox's own
``build_timeline`` run on the same folder (skipped cleanly otherwise).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from record_testkit import (
    FIXTURE_RUN_ID,
    SENTINEL,
    copy_fixture,
    crash_fixture,
    read_jsonl,
    source_ids,
    write_jsonl,
)

from mailroom_record.blobs import BlobStore
from mailroom_record.importer.sandbox_run import import_sandbox_run
from mailroom_record.log import EventLog
from mailroom_record.replay import replay_timeline

TOLERANCE_S = 0.001  # 1 ms


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def expected_timeline(folder: Path) -> list[tuple]:
    """The timeline worked out from the folder's rows: every row at its own time from the
    earliest row, nodes then items in file order on a tie, a checkpoint at 0 and one 0.5 s
    after the last row."""
    nodes = read_jsonl(folder / "nodes.jsonl")
    items = read_jsonl(folder / "items.jsonl")
    cp = json.loads((folder / "checkpoint.json").read_text())
    t0 = min(_epoch(r["ts"]) for r in [*nodes, *items])
    rows = [(_epoch(r["ts"]) - t0, i, ("node", r["doc"], r["node"], r["status"])) for i, r in enumerate(nodes)]
    rows += [(_epoch(r["ts"]) - t0, len(nodes) + i, ("item", r["item_id"])) for i, r in enumerate(items)]
    rows.sort()
    end = rows[-1][0] + 0.5
    final = cp["state"] if cp["state"] in ("done", "failed", "running", "paused", "prepared") else "paused"
    return (
        [(0.0, ("checkpoint", "running", 0, cp["total"]))]
        + [(at, what) for at, _, what in rows]
        + [(end, ("checkpoint", final, cp["cursor"], cp["total"]))]
    )


def normalise(timeline: list[tuple[float, dict]]) -> list[tuple]:
    out = []
    for at, event in timeline:
        ((kind, body),) = event.items()
        if kind == "node":
            what = ("node", body["doc"], body["node"], body["status"])
        elif kind == "item":
            what = ("item", body["item_id"])
        else:
            what = ("checkpoint", body["state"], body["cursor"], body["total"])
        out.append((at, what))
    return out


def _store(root: Path) -> SimpleNamespace:
    blobs = BlobStore(root)
    return SimpleNamespace(root=root, blobs=blobs, log=EventLog(root, blobs))


def _same(got: list[tuple], want: list[tuple]) -> None:
    assert [w for _, w in got] == [w for _, w in want]
    for (a, _), (b, _) in zip(got, want, strict=True):
        assert abs(a - b) <= TOLERANCE_S


# --- the plan's tests ---------------------------------------------------------------------


def test_timeline_matches_fixture_folder(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    r = import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    timeline, meta = replay_timeline(tmp_store.log, r.run_id)
    _same(normalise(timeline), expected_timeline(fixture_folder))
    assert meta["node_records"] == len(read_jsonl(fixture_folder / "nodes.jsonl")) and not meta["synthetic_timing"]
    assert meta["item_records"] == 3
    assert (meta["source_run_id"], meta["engine_kind"], meta["engine_model"]) == (
        FIXTURE_RUN_ID, "openrouter", "vendor/test-model"
    )
    assert (meta["incomplete"], meta["source_state"], meta["warnings"]) == (False, "done", [])
    assert meta["duration_s"] == pytest.approx(timeline[-1][0], abs=TOLERANCE_S)


def test_in_flight_node_replays_as_running(tmp_store: SimpleNamespace, fixture_folder_crashed: Path) -> None:
    r = import_sandbox_run(fixture_folder_crashed, tmp_store.log, tmp_store.blobs)
    timeline, meta = replay_timeline(tmp_store.log, r.run_id)
    _same(normalise(timeline), expected_timeline_torn(fixture_folder_crashed))
    c3 = [e["node"] for _, e in timeline if "node" in e and e["node"]["doc"].endswith("c3")]
    assert (c3[-1]["node"], c3[-1]["status"]) == ("extract", "running")
    assert (meta["incomplete"], meta["source_state"]) == (True, "running")
    assert timeline[-1][1]["checkpoint"]["state"] == "running"


def expected_timeline_torn(folder: Path) -> list[tuple]:
    clean = folder.parent / "untorn"
    clean.mkdir()
    for p in folder.iterdir():
        data = p.read_bytes()
        (clean / p.name).write_bytes(data[: data.rfind(b"\n") + 1] if p.name == "nodes.jsonl" else data)
    return expected_timeline(clean)


def test_timeline_has_no_document_text(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    r = import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    dumped = json.dumps(replay_timeline(tmp_store.log, r.run_id))
    assert SENTINEL not in dumped
    assert "party_a" not in dumped and "extracted_data" not in dumped  # no extracted values or names
    assert "contract" in dumped  # positive control: the outcome fields are there
    item = [e["item"] for _, e in replay_timeline(tmp_store.log, r.run_id)[0] if "item" in e][0]
    assert item["doc_type"] == "contract" and item["extraction_confidence"] == 0.81


def test_native_run_replays(store_with_events: SimpleNamespace) -> None:  # written by LogWriter, not imported
    timeline, meta = replay_timeline(store_with_events.log, "r1")
    nodes = [e["node"] for _, e in timeline if "node" in e]
    assert sorted((n["doc"], n["node"], n["status"]) for n in nodes) == [
        ("d1", "classify", "done"), ("d1", "extract", "done"), ("d2", "classify", "done"),
    ]
    assert nodes[0]["decisions"] == [{"chosen": "contract"}]
    assert [at for at, _ in timeline] == sorted(at for at, _ in timeline)
    assert (meta["node_records"], meta["item_records"], meta["synthetic_timing"]) == (3, 0, False)
    assert meta["source_state"] == "missing" and meta["incomplete"]
    assert meta["engine_kind"] == "mock"


def test_node_rows_carry_the_floor_fields(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    r = import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    rows = [e["node"] for _, e in replay_timeline(tmp_store.log, r.run_id)[0] if "node" in e]
    judge_start, judge_end = [n for n in rows if n["node"] == "judge_verify"]
    assert judge_start["status"] == "running" and judge_start["step"] == 4
    assert (judge_end["stage"], judge_end["ms"], judge_end["agent"]) == ("verified", 600.0, "judge")
    assert judge_end["decisions"][0]["answer"] == "partial" and "options" not in judge_end["decisions"][0]
    classify = [n for n in rows if n["node"] == "classify" and n["status"] == "done"][0]
    assert "options" not in classify["decisions"][0]


def test_an_unknown_run_raises(tmp_store: SimpleNamespace) -> None:
    with pytest.raises(FileNotFoundError):
        replay_timeline(tmp_store.log, "no-such-run")


# --- random folders ---------------------------------------------------------------------

NODE_NAMES = ("intake", "classify", "extract", "judge_verify", "human_review")


@st.composite
def sandbox_rows(draw: Any) -> tuple[list[dict], list[dict], str]:
    """Node and item rows of up to three documents, interleaved, every row at its own time."""
    n_docs = draw(st.integers(1, 3))
    plans = []
    for d in range(n_docs):
        visits = draw(st.lists(st.sampled_from(NODE_NAMES), min_size=1, max_size=4))
        ends = [draw(st.sampled_from(("done", "done", "error", "paused"))) for _ in visits]
        in_flight = draw(st.booleans())
        plans.append((d, visits, ends, in_flight))
    queues = []
    for d, visits, ends, in_flight in plans:
        q = []
        for i, (name, end) in enumerate(zip(visits, ends, strict=True)):
            q.append(("running", d, name, i + 1))
            if not (in_flight and i == len(visits) - 1):
                q.append((end, d, name, None))
        queues.append(q)
    order = draw(st.permutations([d for d, q in enumerate(queues) for _ in q]))
    t = 1_795_000_000.0
    nodes = []
    for d in order:
        status, doc, name, step = queues[d].pop(0)
        t += draw(st.integers(1, 5000)) / 1000.0
        row = {"doc": f"DOC-{doc:016x}", "node": name, "status": status, "agent": None, "model": None, "ts": round(t, 3)}
        row |= {"step": step} if status == "running" else {"ms": 1.0, "stage": "s", "decisions": []}
        nodes.append(row)
    items = []
    for d, _, _, in_flight in plans:
        if not in_flight:
            t += 0.25
            items.append({"item_id": f"DOC-{d:016x}", "ok": True, "error": None, "pred": {}, "score": {}, "ts": t})
    state = "running" if any(p[3] for p in plans) else "done"
    return nodes, items, state


def _random_folder(base: Path, nodes: list[dict], items: list[dict], state: str) -> Path:
    folder = copy_fixture(base / "folder", run_id="ar-random")
    docs = {r["doc"] for r in nodes}
    rows = [r for r in read_jsonl(folder / "dataset.jsonl")][: len(docs)]
    for i, row in enumerate(rows):
        row["id"] = f"DOC-{i:016x}"
    write_jsonl(folder / "dataset.jsonl", rows)
    write_jsonl(folder / "nodes.jsonl", nodes)
    write_jsonl(folder / "items.jsonl", items)
    cp = json.loads((folder / "checkpoint.json").read_text())
    cp |= {"state": state, "cursor": len(items), "total": len(rows)}
    (folder / "checkpoint.json").write_text(json.dumps(cp))
    return folder


@given(sandbox_rows())
def test_random_folders_replay_as_their_rows(tmp_path_factory: pytest.TempPathFactory, rows: tuple) -> None:
    base = tmp_path_factory.mktemp("random")
    folder = _random_folder(base, *rows)
    store = _store(base / "store")
    r = import_sandbox_run(folder, store.log, store.blobs)
    timeline, meta = replay_timeline(store.log, r.run_id)
    _same(normalise(timeline), expected_timeline(folder))
    assert meta["node_records"] == len(rows[0])
    sandbox = _sandbox_floor_replay()
    if sandbox is not None:
        want, want_meta = sandbox.build_timeline(folder)
        _same(normalise(timeline), normalise(want))
        _same_meta(meta, want_meta)


# --- parity with the sandbox's own build_timeline ------------------------------------------


def _sandbox_floor_replay() -> ModuleType | None:
    """The sandbox's ``tui/floor_replay.py`` loaded by path, or ``None`` without a checkout."""
    checkout = os.environ.get("MAILROOM_SANDBOX_CHECKOUT")
    if not checkout:
        return None
    path = Path(checkout) / "src" / "mailroom_sandbox" / "tui" / "floor_replay.py"
    if not path.is_file():
        return None
    name = "sandbox_floor_replay_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while the class is built
    spec.loader.exec_module(module)
    return module


def _same_meta(got: dict, want: dict) -> None:
    for key in ("source_run_id", "engine_kind", "engine_model", "profile", "node_records", "item_records",
                "synthetic_timing", "incomplete", "source_state"):
        assert got[key] == want[key], key
    assert abs(got["duration_s"] - want["duration_s"]) <= TOLERANCE_S


@pytest.mark.parametrize("variant", ["complete", "crashed"])
def test_parity_with_the_sandbox_build_timeline(tmp_path: Path, variant: str) -> None:
    sandbox = _sandbox_floor_replay()
    if sandbox is None:
        pytest.skip("no sandbox checkout (set MAILROOM_SANDBOX_CHECKOUT to run the parity check)")
    folder = copy_fixture(tmp_path / "src" / variant, run_id=f"ar-parity-{variant}")
    if variant == "crashed":
        crash_fixture(folder)
    loaded = [m for m in sys.modules if m.startswith("mailroom_sandbox")]
    assert loaded == []  # the module brought in nothing but the standard library
    store = _store(tmp_path / "store")
    r = import_sandbox_run(folder, store.log, store.blobs)
    got, got_meta = replay_timeline(store.log, r.run_id)
    want, want_meta = sandbox.build_timeline(folder)
    _same(normalise(got), normalise(want))
    _same_meta(got_meta, want_meta)
    assert [e for _, e in got if "node" in e][0]["node"]["doc"] in source_ids(folder)
