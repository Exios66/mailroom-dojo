"""Chained seals and the Parquet column export (ADR-0002, ADR-0004)."""

from __future__ import annotations

import json
import multiprocessing
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from record_testkit import SENTINEL, append_more, seal_in_child, stored_v0_event_dict

from mailroom_record.hashing import canonical_json
from mailroom_record.log import ForkedChain
from mailroom_record.models import GENESIS, Event, Seal, seal_hash
from mailroom_record import seal as seal_module
from mailroom_record.seal import EVENT_COLUMNS, SealCorrupt, SealRefused, list_seals, seal_run

TS = "2026-11-18T13:00:00Z"


def columns(store: SimpleNamespace, n: int) -> Path:
    return store.root / "runs/r1/columns" / f"events-{n}.parquet"


def seal_file(store: SimpleNamespace, n: int) -> Path:
    return store.root / "runs/r1/seals" / f"{n}.json"


def rows(store: SimpleNamespace, n: int) -> list[dict[str, Any]]:
    return pq.read_table(columns(store, n)).to_pylist()


# --- the plan's tests -------------------------------------------------------------


def test_seal_commits_heads_and_document_list(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    seal = seal_run(store.log, "r1")
    assert seal.n == 0 and seal.prev_seal_hash == GENESIS
    assert seal.doc_heads == store.log.doc_heads("r1")
    assert sorted(seal.document_ids) == ["d1", "d2"]


def test_second_seal_chains_and_exports_only_new_events(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    s0 = seal_run(store.log, "r1")
    append_more(store, doc_id="d3", n=3)
    s1 = seal_run(store.log, "r1")
    assert s1.prev_seal_hash == s0.hash and s1.n == 1
    assert pq.read_table(columns(store, 1)).num_rows == 3


def test_parquet_has_no_payload_column(store_with_events: SimpleNamespace) -> None:
    seal_run(store_with_events.log, "r1")
    assert "payload" not in pq.read_schema(columns(store_with_events, 0)).names


def test_seal_without_new_events_is_a_no_op(store_with_events: SimpleNamespace) -> None:
    assert seal_run(store_with_events.log, "r1").hash == seal_run(store_with_events.log, "r1").hash


# --- additions: the seal record ---------------------------------------------------


def test_seal_file_is_canonical_and_rehashes(store_with_events: SimpleNamespace) -> None:
    seal = seal_run(store_with_events.log, "r1", sealed_at=TS)
    raw = seal_file(store_with_events, 0).read_bytes()
    stored = json.loads(raw)
    assert raw == canonical_json(stored)
    assert seal_hash(stored) == stored["hash"] == seal.hash
    assert stored["schema_version"] == 1 and stored["sealed_at"] == TS
    assert Seal.from_stored(stored) == seal
    assert list_seals(store_with_events.log, "r1") == [seal]


def test_event_count_is_cumulative_and_document_ids_sorted(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    s0 = seal_run(store.log, "r1")
    assert s0.event_count == 3
    append_more(store, doc_id="c0", n=2)
    s1 = seal_run(store.log, "r1")
    assert s1.event_count == 5
    assert s1.document_ids == ["c0", "d1", "d2"] == sorted(s1.doc_heads)


def test_sealed_at_defaults_to_utc_now_with_z(store_with_events: SimpleNamespace) -> None:
    sealed_at = seal_run(store_with_events.log, "r1").sealed_at
    assert sealed_at.endswith("Z") and sealed_at.startswith("20")


def test_bad_sealed_at_writes_nothing(store_with_events: SimpleNamespace) -> None:
    with pytest.raises(ValueError):
        seal_run(store_with_events.log, "r1", sealed_at="2026-11-18 13:00:00")
    assert not seal_file(store_with_events, 0).exists()
    assert not columns(store_with_events, 0).exists()


def test_no_op_seal_writes_nothing(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    s0 = seal_run(store.log, "r1")
    before = sorted(p.name for p in (store.root / "runs/r1").rglob("*"))
    assert seal_run(store.log, "r1", sealed_at=TS) == s0
    assert sorted(p.name for p in (store.root / "runs/r1").rglob("*")) == before


def test_empty_run_seals_as_an_empty_seal_zero(store: SimpleNamespace) -> None:
    # Decision: a run with no events gets seal 0 with no documents (a commitment that the
    # run was empty), and an empty Parquet file with the full schema.
    s0 = seal_run(store.log, "r1")
    assert (s0.n, s0.doc_heads, s0.document_ids, s0.event_count) == (0, {}, [], 0)
    table = pq.read_table(columns(store, 0))
    assert table.num_rows == 0 and table.column_names == list(EVENT_COLUMNS)
    assert seal_run(store.log, "r1") == s0
    append_more(store, doc_id="d1", n=1)
    s1 = seal_run(store.log, "r1")
    assert (s1.n, s1.prev_seal_hash, s1.event_count) == (1, s0.hash, 1)


def test_unknown_run_is_refused(store: SimpleNamespace) -> None:
    with pytest.raises(FileNotFoundError):
        seal_run(store.log, "nope")
    assert list_seals(store.log, "nope") == []


def test_unreadable_seal_file_is_an_error(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    seal_run(store.log, "r1")
    seal_file(store, 1).write_bytes(b"{}")  # a stray file where seal 1 would go
    append_more(store, doc_id="d3", n=1)
    with pytest.raises(SealCorrupt, match="1.json"):
        list_seals(store.log, "r1")
    with pytest.raises(SealCorrupt):
        seal_run(store.log, "r1")
    assert seal_file(store, 1).read_bytes() == b"{}"


def test_seal_file_is_write_once(store_with_events: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    # Seal 1 appears after the sealer read the seal list (a sealer that bypassed the lock):
    # the existing file is kept and the call fails instead of replacing it.
    store = store_with_events
    s0 = seal_run(store.log, "r1")
    append_more(store, doc_id="d3", n=1)
    s1 = seal_run(store.log, "r1")
    kept = seal_file(store, 1).read_bytes()
    append_more(store, doc_id="d4", n=1)
    raw0 = json.loads(seal_file(store, 0).read_bytes())
    monkeypatch.setattr(seal_module, "_load_seals", lambda log, run_id: [(s0, raw0)])
    with pytest.raises(SealRefused, match="already exists"):
        seal_run(store.log, "r1")
    assert seal_file(store, 1).read_bytes() == kept and s1.n == 1


# --- additions: which events a seal covers ----------------------------------------


def test_late_events_from_two_writers_go_into_the_next_seal(store: SimpleNamespace) -> None:
    a = store.log.open_writer("r1", "wa", flush_every=1000)
    b = store.log.open_writer("r1", "wb", flush_every=1000)
    a.append(doc_id="d1", node="n", kind="decision")
    a.append(doc_id="d2", node="n", kind="decision")
    b.append(doc_id="d3", node="n", kind="decision")
    a.flush()
    b.flush()
    late = a.append(doc_id="d1", node="late", kind="decision")  # buffered at seal time
    s0 = seal_run(store.log, "r1")
    assert s0.event_count == 3 and late.hash not in s0.doc_heads.values()
    b.append(doc_id="d3", node="n2", kind="decision")
    a.close()
    b.close()
    s1 = seal_run(store.log, "r1")
    got = [(r["doc_id"], r["seq"]) for r in rows(store, 1)]
    assert got == [("d1", 1), ("d3", 1)]
    assert s1.event_count == 5 and s1.doc_heads["d1"] == late.hash
    every = [(r["doc_id"], r["seq"]) for n in (0, 1) for r in rows(store, n)]
    assert sorted(every) == sorted((e.doc_id, e.seq) for e in store.log.iter_events("r1"))


def test_torn_tail_is_skipped_not_sealed(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    seg = store.root / "runs/r1/segments/w0.jsonl"
    with seg.open("ab") as f:
        f.write(b'{"run_id": "r1", "doc_')
    s0 = seal_run(store.log, "r1")
    assert s0.event_count == 3 and len(rows(store, 0)) == 3


def test_forked_chain_refuses_to_seal(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    head = list(store.log.iter_events("r1", "d1"))[0]
    forked = Event.build(
        **(head.model_dump(mode="json", exclude={"hash"}) | {"seq": 1, "prev_hash": head.hash, "node": "fork"})
    )
    (store.root / "runs/r1/segments/w9.jsonl").write_bytes(canonical_json(forked.model_dump(mode="json")) + b"\n")
    with pytest.raises(ForkedChain, match=r"'d1'.*seq 1"):
        seal_run(store.log, "r1")
    assert not (store.root / "runs/r1/seals/0.json").exists()
    assert not columns(store, 0).exists()


def test_log_missing_a_sealed_head_refuses_to_seal(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    seal_run(store.log, "r1")
    seg = store.root / "runs/r1/segments/w0.jsonl"
    lines = seg.read_bytes().splitlines(keepends=True)
    seg.write_bytes(b"".join(line for line in lines if b'"node":"extract"' not in line))  # cut d1's head
    append_more(store, doc_id="d3", n=1)
    with pytest.raises(SealRefused, match="d1"):
        seal_run(store.log, "r1")


# --- additions: the Parquet export ------------------------------------------------


def test_columns_and_types(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    ref = store.blobs.put(b"synthetic transcript", "transcript")
    with store.log.open_writer("r1", "w1") as w:
        e = w.append(
            doc_id="d4", node="extract", kind="model_call", tokens_in=10, latency_ms=1.5,
            decision=[{"b": 1, "a": 2}], inputs=[ref], outputs=[ref],
        )
    seal_run(store.log, "r1")
    assert EVENT_COLUMNS == tuple(f for f in Event.model_fields if f != "payload")
    table = pq.read_table(columns(store, 0))
    assert table.column_names == list(EVENT_COLUMNS)
    assert table.schema.field("inputs").type == pa.list_(pa.string())
    assert table.schema.field("tokens_in").type == pa.int64()
    assert table.schema.field("latency_ms").type == pa.float64()
    assert table.schema.field("decision").type == pa.string()
    row = next(r for r in table.to_pylist() if r["doc_id"] == "d4")
    assert row["decision"] == canonical_json(e.decision).decode() == '[{"a":2,"b":1}]'
    assert row["inputs"] == row["outputs"] == [ref.sha256]
    assert row["hash"] == e.hash and row["seq"] == 0 and row["tokens_out"] is None
    # Every exported value equals the event's own field (decision as canonical JSON).
    for got, ev in zip(table.to_pylist(), store.log.iter_events("r1")):
        for col in EVENT_COLUMNS:
            want = getattr(ev, col)
            if col == "decision":
                want = None if want is None else canonical_json(want).decode()
            elif col in ("inputs", "outputs"):
                want = [r.sha256 for r in want]
            assert got[col] == want, col


def test_all_null_columns_keep_their_types(store: SimpleNamespace) -> None:
    append_more(store, doc_id="d1", n=2)  # no tokens, no decision, no model: all None
    seal_run(store.log, "r1")
    schema = pq.read_schema(columns(store, 0))
    assert schema.field("tokens_in").type == pa.int64()
    assert schema.field("decision").type == pa.string()
    assert schema.field("model").type == pa.string()
    assert pq.read_schema(columns(store, 0)).equals(schema)


def test_v0_line_is_exported_through_the_upgrade_path(store: SimpleNamespace) -> None:
    raw = stored_v0_event_dict(doc_id="old", inputs=[])
    (store.root / "runs/r1/segments/legacy.jsonl").write_bytes(canonical_json(raw) + b"\n")
    seal = seal_run(store.log, "r1")
    (row,) = rows(store, 0)
    assert row["tokens_cached"] is None and row["hash"] == raw["hash"]
    assert row["schema_version"] == 0  # the version the line was stored in
    assert seal.doc_heads == {"old": raw["hash"]}


def _decoded_text(path: Path) -> str:
    return json.dumps(pq.read_table(path).to_pylist(), ensure_ascii=False)


def test_document_text_never_reaches_the_parquet(store: SimpleNamespace) -> None:  # leak scan
    ref = store.blobs.put(f"Dear claimant, {SENTINEL} policy 12345".encode(), "original")
    with store.log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="intake", kind="output", outputs=[ref], payload={"note": SENTINEL})
        w.append(doc_id="d1", node="classify", kind="decision", inputs=[ref], decision={"chosen": "letter"})
    seal_run(store.log, "r1")
    path = columns(store, 0)
    assert SENTINEL not in _decoded_text(path)
    assert SENTINEL.encode() not in path.read_bytes()


def test_leak_scan_would_find_the_sentinel_in_an_exported_column(store: SimpleNamespace) -> None:
    # Positive control for the scan above: a sentinel in an exported field is found.
    with store.log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="n", kind="error", cause=SENTINEL)
    seal_run(store.log, "r1")
    assert SENTINEL in _decoded_text(columns(store, 0))


def test_no_temp_files_are_left(store_with_events: SimpleNamespace) -> None:
    seal_run(store_with_events.log, "r1")
    assert not [p for p in (store_with_events.root / "runs").rglob("*") if p.name.endswith(".tmp")]


# --- additions: concurrency ---------------------------------------------------------


def _check_seal_chain(store: SimpleNamespace) -> list[Seal]:
    seals = list_seals(store.log, "r1")
    assert [s.n for s in seals] == list(range(len(seals)))
    prev = GENESIS
    for s in seals:
        assert s.prev_seal_hash == prev and seal_hash(s.model_dump(mode="json")) == s.hash
        prev = s.hash
    exported = [(r["doc_id"], r["seq"]) for s in seals for r in rows(store, s.n)]
    assert len(exported) == len(set(exported)) == seals[-1].event_count
    return seals


def test_concurrent_seal_calls_from_threads_chain_correctly(store: SimpleNamespace) -> None:
    n_threads, rounds = 6, 5
    barrier = threading.Barrier(n_threads + 1)
    errors: list[BaseException] = []

    def sealer() -> None:
        try:
            barrier.wait()
            for _ in range(rounds):
                seal_run(store.log, "r1")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=sealer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    with store.log.open_writer("r1", "w0", flush_every=3) as w:
        barrier.wait()
        for i in range(60):
            w.append(doc_id=f"d{i % 4}", node="n", kind="handoff")
    for t in threads:
        t.join()
    assert errors == []
    seal_run(store.log, "r1")
    seals = _check_seal_chain(store)
    assert seals[-1].event_count == 60


def test_concurrent_seal_calls_from_processes_chain_correctly(store: SimpleNamespace) -> None:
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(4) as pool:
        pending = pool.starmap_async(seal_in_child, [(str(store.root), "r1", 4)] * 4)
        with store.log.open_writer("r1", "w0", flush_every=2) as w:
            for i in range(40):
                w.append(doc_id=f"d{i % 3}", node="n", kind="handoff")
        pending.get(timeout=60)
    seal_run(store.log, "r1")
    seals = _check_seal_chain(store)
    assert seals[-1].event_count == 40


# --- additions: crash recovery and a tampered seal chain ---------------------------


def test_lost_parquet_of_the_latest_seal_is_rebuilt_from_the_log(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    seal_run(store.log, "r1")
    append_more(store, doc_id="d3", n=2)
    s1 = seal_run(store.log, "r1")
    want = rows(store, 1)
    columns(store, 1).unlink()  # a crash between the seal file and its Parquet file
    assert seal_run(store.log, "r1") == s1
    assert rows(store, 1) == want


def test_tampered_seal_chain_refuses_to_seal(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    seal_run(store.log, "r1")
    stored = json.loads(seal_file(store, 0).read_bytes())
    seal_file(store, 0).write_bytes(canonical_json(stored | {"event_count": 99}))  # hash not recomputed
    append_more(store, doc_id="d3", n=1)
    with pytest.raises(SealRefused, match="seal 0 does not hash"):
        seal_run(store.log, "r1")
    assert not seal_file(store, 1).exists()


def test_seal_chain_gap_refuses_to_seal(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    seal_run(store.log, "r1")
    append_more(store, doc_id="d3", n=1)
    seal_run(store.log, "r1")
    seal_file(store, 0).unlink()
    append_more(store, doc_id="d4", n=1)
    with pytest.raises(SealRefused, match="gap"):
        seal_run(store.log, "r1")


# --- review fixes: a consistent snapshot of the log --------------------------------


def _exported(store: SimpleNamespace) -> list[tuple[str, int]]:
    return [(r["doc_id"], r["seq"]) for s in list_seals(store.log, "r1") for r in rows(store, s.n)]


def test_handoff_during_the_read_is_not_sealed_over(store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    # Writer "a" hands d1 to writer "b" after the sealer read a.jsonl but before b.jsonl.
    # The first read sees d1 seq 0,1,2,4 (seq 3 sits in a.jsonl, already read): sealing
    # that would commit head 4 while exporting no seq 3. The sealer must re-read.
    append_more(store, doc_id="d2", n=1, writer_id="b")  # b.jsonl exists, sorts after a.jsonl
    append_more(store, doc_id="d1", n=3, writer_id="a")
    wa = store.log.open_writer("r1", "a")
    original = Path.read_bytes
    fired: list[bool] = []

    def read_bytes(self: Path) -> bytes:
        data = original(self)
        if self.name == "a.jsonl" and not fired:
            fired.append(True)
            wa.append(doc_id="d1", node="n", kind="output")  # seq 3 -> a.jsonl
            wa.close()
            append_more(store, doc_id="d1", n=1, writer_id="b")  # seq 4 -> b.jsonl
        return data

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    s0 = seal_run(store.log, "r1")
    monkeypatch.setattr(Path, "read_bytes", original)
    assert fired
    heads = store.log.doc_head_seqs("r1")
    assert s0.event_count == sum(seq + 1 for seq, _ in heads.values()) == 6
    assert s0.doc_heads == {d: h for d, (_, h) in heads.items()}
    assert sorted(_exported(store)) == [("d1", i) for i in range(5)] + [("d2", 0)]


def _write_gapped_chain(store: SimpleNamespace, doc_id: str, seqs: list[int]) -> None:
    prev, lines = GENESIS, []
    for seq in range(max(seqs) + 1):
        e = Event.build(
            run_id="r1", doc_id=doc_id, seq=seq, ts=TS, node="n", agent=None, attempt=0, kind="output",
            model=None, prompt_id=None, tokens_in=None, tokens_out=None, tokens_cached=None,
            price_basis=None, latency_ms=None, decision=None, cause=None, inputs=[], outputs=[],
            payload={}, prev_hash=prev,
        )
        prev = e.hash
        if seq in seqs:
            lines.append(canonical_json(e.model_dump(mode="json")) + b"\n")
    (store.root / "runs/r1/segments/gap.jsonl").write_bytes(b"".join(lines))


def test_persistent_gap_on_disk_refuses_and_writes_nothing(store_with_events: SimpleNamespace) -> None:
    store = store_with_events
    _write_gapped_chain(store, "g1", [0, 1, 3])
    with pytest.raises(SealRefused, match=r"'g1'.*seq 2.*verify"):
        seal_run(store.log, "r1")
    assert not seal_file(store, 0).exists() and not columns(store, 0).exists()


def test_snapshot_rereads_are_bounded(store_with_events: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_with_events
    _write_gapped_chain(store, "g1", [0, 2])
    reads: list[int] = []
    original = store.log.iter_stored

    def counting(run_id: str):  # type: ignore[no-untyped-def]
        reads.append(1)
        return original(run_id)

    monkeypatch.setattr(store.log, "iter_stored", counting)
    with pytest.raises(SealRefused, match="verify"):
        seal_run(store.log, "r1")
    assert len(reads) == 2  # a stable gap: two agreeing reads, then refuse

    # A log that changes on every read (a writer racing forever) still stops.
    reads.clear()
    n = [0]

    def changing(run_id: str):  # type: ignore[no-untyped-def]
        reads.append(1)
        n[0] += 1
        _write_gapped_chain(store, f"g{n[0] + 1}", [0, 2])  # a new gapped doc every read
        return original(run_id)

    (store.root / "runs/r1/segments/gap.jsonl").unlink()
    monkeypatch.setattr(store.log, "iter_stored", changing)
    with pytest.raises(SealRefused, match="verify"):
        seal_run(store.log, "r1")
    assert len(reads) == seal_module._SNAPSHOT_TRIES
    assert not seal_file(store, 0).exists()


def test_gap_below_a_sealed_head_refuses(store_with_events: SimpleNamespace) -> None:
    # Sealing must not build on a log that lost an event an earlier seal covered.
    store = store_with_events
    seal_run(store.log, "r1")
    seg = store.root / "runs/r1/segments/w0.jsonl"
    kept = [l for l in seg.read_bytes().splitlines(keepends=True) if (json.loads(l)["doc_id"], json.loads(l)["seq"]) != ("d1", 0)]
    seg.write_bytes(b"".join(kept))  # cut d1 seq 0; its sealed head (seq 1) stays
    assert [e.seq for e in store.log.iter_events("r1", "d1")] == [1]
    append_more(store, doc_id="d3", n=1)
    with pytest.raises(SealRefused, match=r"'d1'.*seq 0"):
        seal_run(store.log, "r1")
    assert not seal_file(store, 1).exists()


# --- review fixes: rows are validated before the seal is written ------------------


def _hashed_line(**overrides: Any) -> bytes:
    from mailroom_record.models import event_hash

    raw = Event.build(
        run_id="r1", doc_id="bad", seq=0, ts=TS, node="n", agent=None, attempt=0, kind="output",
        model=None, prompt_id=None, tokens_in=None, tokens_out=None, tokens_cached=None,
        price_basis=None, latency_ms=None, decision=None, cause=None, inputs=[], outputs=[],
        payload={}, prev_hash=GENESIS,
    ).model_dump(mode="json") | overrides
    raw["hash"] = event_hash(raw)
    return canonical_json(raw) + b"\n"


@pytest.mark.parametrize(
    "overrides",
    [{"kind": "bogus"}, {"attempt": -1}, {"schema_version": 99}, {"tokens_in": 2**70}],
    ids=["unknown-kind", "bad-field", "newer-version", "int-too-big-for-parquet"],
)
def test_invalid_but_hashed_line_refuses_and_writes_nothing(
    store_with_events: SimpleNamespace, overrides: dict[str, Any]
) -> None:
    from mailroom_record.log import CorruptLine

    store = store_with_events
    bad = store.root / "runs/r1/segments/bad.jsonl"
    bad.write_bytes(_hashed_line(**overrides))
    with pytest.raises(CorruptLine, match=r"bad.jsonl at byte 0"):
        seal_run(store.log, "r1")
    assert not seal_file(store, 0).exists() and not columns(store, 0).exists()
    with pytest.raises(CorruptLine):  # still refused, never stuck half-written
        seal_run(store.log, "r1")
    bad.unlink()
    s0 = seal_run(store.log, "r1")
    assert s0.n == 0 and s0.event_count == 3 and len(rows(store, 0)) == 3


# --- review fixes: seal chain hashes the stored form ------------------------------


def test_older_version_seal_chains_on_its_stored_hash(
    store_with_events: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hypothetical v0 seal lacked document_ids; the upgrade derives it from doc_heads.
    from mailroom_record import upgrade

    def seal_0_to_1(raw: dict[str, Any]) -> dict[str, Any]:
        raw["document_ids"] = sorted(raw["doc_heads"])
        raw["schema_version"] = 1
        return raw

    monkeypatch.setattr(upgrade, "SEAL_STEPS", {0: seal_0_to_1})
    store = store_with_events
    seal_run(store.log, "r1")
    v0 = json.loads(seal_file(store, 0).read_bytes())
    del v0["document_ids"]
    v0["schema_version"] = 0
    v0["hash"] = seal_hash(v0)
    seal_file(store, 0).write_bytes(canonical_json(v0))
    append_more(store, doc_id="d3", n=1)
    s1 = seal_run(store.log, "r1")
    assert s1.n == 1 and s1.prev_seal_hash == v0["hash"] and s1.event_count == 4
    # A tampered v0 seal is still caught on its stored form.
    seal_file(store, 0).write_bytes(canonical_json(v0 | {"event_count": 7}))
    append_more(store, doc_id="d4", n=1)
    with pytest.raises(SealRefused, match="seal 0 does not hash"):
        seal_run(store.log, "r1")
