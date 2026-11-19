"""Verification of chains, seals, column files and blobs (ADR-0002, ADR-0003, ADR-0005).

Every test builds a real store on disk (several documents, writers, segments and seals),
damages it the way a crash, bit rot or an attacker would, and asserts the exact problems
reported, and that the documents it did not touch still verify (no masking, no false
positives).
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import signal
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import zstandard
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from record_testkit import (
    DEEP_UNPARSABLE,
    SENTINEL,
    Faults,
    append_in_child,
    append_until_killed,
    blob_bytes,
    build_sealed_store,
    delete_document,
    delete_line,
    failing,
    find_line,
    flip_byte,
    forge_event,
    deep_payload_line,
    hand_line,
    join_all,
    kinds,
    last_seq,
    remove_blob_file,
    replace_bytes,
    rewrite_line,
    run_fields,
    seal_until_stopped,
    start_handoff_workers,
    stop_workers,
    stored_v0_event_dict,
    two_output_blobs,
    verify,
)

from mailroom_record.blobs import BlobStore
from mailroom_record.hashing import canonical_json
from mailroom_record.log import CorruptLine, EventLog, ForkedChain, OverlongLine, RawLine, TornTail, iter_raw_segment
from mailroom_record.models import GENESIS, Run, event_hash, seal_hash
from mailroom_record.seal import EVENT_SCHEMA, SealRefused, seal_run
from mailroom_record import verify as verify_module
from mailroom_record.verify import PROBLEM_KINDS, Problem, verify_run

TOTAL_EVENTS = 14  # d1: 5, d2: 3, d3: 3, d4: 3 (see build_sealed_store)


@pytest.fixture
def sealed_store(tmp_path: Path) -> SimpleNamespace:
    return build_sealed_store(tmp_path / "store")


def run_dir(store: SimpleNamespace) -> Path:
    return store.root / "runs" / store.run_id


def seal_path(store: SimpleNamespace, n: int) -> Path:
    return run_dir(store) / "seals" / f"{n}.json"


def columns_path(store: SimpleNamespace, n: int) -> Path:
    return run_dir(store) / "columns" / f"events-{n}.parquet"


def docs_named(report: Any) -> set[str | None]:
    return {p.doc_id for p in report.problems if p.fails}


def assert_no_leak(report: Any) -> None:
    text = repr(report)
    assert SENTINEL not in text


# --- the plan's tests ------------------------------------------------------------------


def test_clean_run_verifies(sealed_store: SimpleNamespace) -> None:
    r = verify_run(*sealed_store.parts, "r1")
    assert r.ok and r.problems == []
    assert (r.events_checked, r.documents_checked, r.seals_checked) == (TOTAL_EVENTS, 4, 3)
    assert r.tombstoned_blobs == 0 and r.closed


def test_edited_event_is_bad_hash(sealed_store: SimpleNamespace) -> None:
    rewrite_line(sealed_store, doc_id="d1", seq=1, cause="tampered")
    r = verify(sealed_store)
    assert kinds(r) == {"bad_hash", "broken_link"}
    assert failing(r) == [("bad_hash", "d1", 1), ("broken_link", "d1", 2)]
    seg, off, _ = find_line(sealed_store, "d1", 1)
    assert (r.problems[0].segment, r.problems[0].byte_offset) == (seg.name, off)


def test_deleted_middle_event_is_seq_gap(sealed_store: SimpleNamespace) -> None:
    delete_line(sealed_store, doc_id="d1", seq=1)
    r = verify(sealed_store)
    assert "seq_gap" in kinds(r)
    # Exactly that: no broken_link on seq 2 (its predecessor is gone, not wrong), and the
    # damaged document's column rows are not compared, so nothing else fires.
    assert failing(r) == [("seq_gap", "d1", 1)]


def test_deleted_whole_document_is_caught_by_seal(sealed_store: SimpleNamespace) -> None:
    delete_document(sealed_store, "d2")
    r = verify(sealed_store)
    assert "document_missing_from_log" in kinds(r)
    assert failing(r) == [("document_missing_from_log", "d2", None)]
    assert r.problems[0].seal == 0  # the first seal that lists it
    assert r.documents_checked == 3


def test_cut_tail_is_caught_by_seal(sealed_store: SimpleNamespace) -> None:
    delete_line(sealed_store, doc_id="d1", seq=last_seq(sealed_store, "d1"))
    r = verify(sealed_store)
    assert "truncated_after_seal" in kinds(r)
    assert failing(r) == [("truncated_after_seal", "d1", None)]
    assert r.problems[0].seal == 1


def test_tombstoned_blob_passes_missing_blob_fails(sealed_store: SimpleNamespace) -> None:  # Review Focus 3
    a, b = two_output_blobs(sealed_store)
    sealed_store.blobs.tombstone(a, "deletion request")
    remove_blob_file(sealed_store, b)
    r = verify(sealed_store)
    assert r.tombstoned_blobs == 1
    assert [p.kind for p in r.problems] == ["missing_blob"]
    # It names the event that refers to the lost blob.
    doc, seq = next((d, s) for d, s, sha in sealed_store.outputs if sha == b)
    assert (r.problems[0].doc_id, r.problems[0].seq) == (doc, seq)
    assert b in r.problems[0].detail


# --- chains: the edges of each kind ----------------------------------------------------


def test_edited_event_with_recomputed_hash_breaks_the_next_link(sealed_store: SimpleNamespace) -> None:
    # An attacker who recomputes the edited event's hash still breaks the link after it.
    rewrite_line(sealed_store, doc_id="d3", seq=0, recompute_hash=True, node="forged")
    r = verify(sealed_store)
    assert failing(r) == [("broken_link", "d3", 1)]


def test_edited_sealed_head_with_recomputed_hash_is_truncated_after_seal(sealed_store: SimpleNamespace) -> None:
    # The head has no successor to break; the seal still commits the old head hash.
    rewrite_line(sealed_store, doc_id="d4", seq=2, recompute_hash=True, tokens_in=999)
    r = verify(sealed_store)
    assert failing(r) == [("truncated_after_seal", "d4", None)]


def test_first_event_must_start_from_genesis(sealed_store: SimpleNamespace) -> None:
    rewrite_line(sealed_store, doc_id="d2", seq=0, recompute_hash=True, prev_hash="1" * 64)
    r = verify(sealed_store)
    assert ("broken_link", "d2", 0) in failing(r)
    assert docs_named(r) == {"d2"}


def test_deleted_run_of_events_is_one_gap(sealed_store: SimpleNamespace) -> None:
    # d1's sealed heads are seq 2 (seal 0) and seq 4 (seals 1, 2): delete 0 and 1 below them.
    delete_line(sealed_store, "d1", 0)
    delete_line(sealed_store, "d1", 1)
    r = verify(sealed_store)
    assert failing(r) == [("seq_gap", "d1", 0)]
    assert "seqs 0..1" in r.problems[0].detail


def test_huge_seq_is_one_gap_without_exhausting_memory(sealed_store: SimpleNamespace) -> None:
    # One line claiming seq 10**9: the gap is found by walking stored seqs, not range(10**9).
    append_raw_after_head(sealed_store, "d4", seq=10**9)
    start = time.monotonic()
    r = verify(sealed_store)
    assert time.monotonic() - start < 5
    # The forged line hashes, but its prev_hash names seq 2, not seq 999999999.
    assert failing(r) == [("seq_gap", "d4", 3)]
    assert "seqs 3..999999999" in r.problems[0].detail


def test_forked_chain_is_reported_not_raised(sealed_store: SimpleNamespace) -> None:
    # A second owner wrote seq 3 of d2 as well (past every seal): seal_run refuses it,
    # verify reports it and still checks every other document.
    s = sealed_store
    head = s.log.doc_head_seqs("r1")["d2"]
    with s.log.open_writer("r1", "w5") as w:
        w.append(doc_id="d2", node="left", kind="decision")
    forge_event(s, "d2", head[0] + 1, head[1], node="right")
    with pytest.raises(ForkedChain):
        seal_run(s.log, "r1")
    r = verify(s)
    assert failing(r) == [("forked_chain", "d2", head[0] + 1)]
    assert "stored 2 times" in r.problems[0].detail


def test_fork_inside_the_sealed_range_keeps_later_links_checked(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    seq0 = json.loads(find_line(s, "d1", 0)[2])
    forge_event(s, "d1", 1, seq0["hash"], node="other-branch")
    r = verify(s)
    # seq 2 links to one of the two seq-1 events, so there is no broken_link.
    assert failing(r) == [("forked_chain", "d1", 1)]


def test_duplicated_line_is_a_fork(sealed_store: SimpleNamespace) -> None:
    seg, _, line = find_line(sealed_store, "d4", 1)
    with seg.open("ab") as f:
        f.write(line + b"\n")
    assert failing(verify(sealed_store)) == [("forked_chain", "d4", 1)]


def append_raw_after_head(store: SimpleNamespace, doc_id: str, **changes: Any) -> None:
    """Append, past the last seal, a line that hashes and links but carries ``changes``."""
    seq, head = store.log.doc_head_seqs("r1")[doc_id]
    raw = json.loads(find_line(store, doc_id, seq)[2]) | {"seq": seq + 1, "prev_hash": head} | changes
    raw["hash"] = event_hash(raw)
    with (run_dir(store) / "segments" / "w8.jsonl").open("ab") as f:
        f.write(canonical_json(raw) + b"\n")


def test_line_with_a_valid_hash_but_an_invalid_field_is_invalid_event(sealed_store: SimpleNamespace) -> None:
    append_raw_after_head(sealed_store, "d3", kind="not-a-kind", node=SENTINEL)
    r = verify(sealed_store)
    assert failing(r) == [("invalid_event", "d3", 3)]
    assert "kind" in r.problems[0].detail
    assert_no_leak(r)


def test_sealed_line_rewritten_into_an_invalid_event(sealed_store: SimpleNamespace) -> None:
    # The same edit on a sealed head also loses the head the seals committed.
    rewrite_line(sealed_store, "d3", 2, recompute_hash=True, kind="not-a-kind", node=SENTINEL)
    r = verify(sealed_store)
    assert failing(r) == [("invalid_event", "d3", 2), ("truncated_after_seal", "d3", None)]
    assert_no_leak(r)


def test_newer_schema_version_is_invalid_event_not_a_crash(sealed_store: SimpleNamespace) -> None:
    append_raw_after_head(sealed_store, "d3", schema_version=99)
    r = verify(sealed_store)
    assert failing(r) == [("invalid_event", "d3", 3)]
    assert "UnknownSchemaVersion" in r.problems[0].detail


def test_event_moved_from_another_run_is_invalid_event(sealed_store: SimpleNamespace) -> None:
    append_raw_after_head(sealed_store, "d3", run_id="r2")
    r = verify(sealed_store)
    assert failing(r) == [("invalid_event", "d3", 3)]
    assert "'r2'" in next(p.detail for p in r.problems if p.kind == "invalid_event")


def test_same_meaning_different_bytes_is_caught(sealed_store: SimpleNamespace) -> None:
    # The parsed event (and so its hash) is unchanged, but a writer never writes this.
    seg, off, line = find_line(sealed_store, "d2", 1)
    replace_bytes(seg, off, len(line), line.replace(b',"seq":', b', "seq":'))
    r = verify(sealed_store)
    assert failing(r) == [("corrupt_line", "d2", 1)]
    assert "canonical" in r.problems[0].detail


def test_nan_in_a_line_is_bad_hash_not_a_crash(sealed_store: SimpleNamespace) -> None:
    seg, off, line = find_line(sealed_store, "d3", 2)
    raw = json.loads(line)
    text = line.replace(f'"latency_ms":{raw["latency_ms"]!r}'.encode(), b'"latency_ms":NaN')
    assert text != line
    replace_bytes(seg, off, len(line), text)
    r = verify(sealed_store)
    assert ("bad_hash", "d3", 2) in failing(r) and docs_named(r) == {"d3"}


def test_v0_event_verifies_on_its_stored_form(store: SimpleNamespace) -> None:  # ADR-0005
    seg = store.root / "runs/r1/segments/w0.jsonl"
    v0 = stored_v0_event_dict(doc_id="d1", seq=0, inputs=[], prev_hash=GENESIS)
    seg.write_bytes(canonical_json(v0) + b"\n")
    with store.log.open_writer("r1", "w1") as w:
        w.append(doc_id="d1", node="after", kind="decision")
    seal_run(store.log, "r1")
    r = verify_run(store.log, store.blobs, "r1")
    assert r.ok and r.problems == [] and r.events_checked == 2
    # Tampering with the old line is still caught on the stored form.
    seg.write_bytes(canonical_json(v0 | {"tokens_in": 1}) + b"\n")
    assert failing(verify_run(store.log, store.blobs, "r1")) == [("bad_hash", "d1", 0), ("broken_link", "d1", 1)]


# --- corrupt lines and torn tails ----------------------------------------------------


def test_garbage_line_mid_segment_is_reported_and_the_rest_verifies(sealed_store: SimpleNamespace) -> None:
    seg, off, _ = find_line(sealed_store, "d2", 0)
    replace_bytes(seg, off, 0, b"\x00\xffgarbage " + SENTINEL.encode() + b"\n")
    r = verify(sealed_store)
    assert failing(r) == [("corrupt_line", None, None)]
    p = r.problems[0]
    assert (p.segment, p.byte_offset) == (seg.name, off)
    assert r.events_checked == TOTAL_EVENTS  # every event around it was still checked
    assert_no_leak(r)


def test_torn_bytes_mid_file_then_later_appends(sealed_store: SimpleNamespace) -> None:
    # A crash tore a line; something later appended whole lines after the torn bytes.
    # The torn bytes glue onto the next line, so that event is lost: reported, not fatal.
    seg, off, _ = find_line(sealed_store, "d1", 1)
    replace_bytes(seg, off, 0, b'{"run_id":"r1","doc_')
    r = verify(sealed_store)
    assert failing(r) == [("corrupt_line", None, None), ("seq_gap", "d1", 1)]
    assert (r.problems[0].segment, r.problems[0].byte_offset) == (seg.name, off)
    assert r.events_checked == TOTAL_EVENTS - 1


def test_strict_readers_refuse_what_verify_reports(sealed_store: SimpleNamespace) -> None:
    seg, off, _ = find_line(sealed_store, "d2", 0)
    replace_bytes(seg, off, 0, b"not json\n")
    with pytest.raises(ValueError):
        list(sealed_store.log.iter_stored("r1"))
    assert failing(verify(sealed_store)) == [("corrupt_line", None, None)]


def test_torn_tail_is_benign_while_the_run_is_live(store_with_events: SimpleNamespace) -> None:
    s = store_with_events
    seg = s.root / "runs/r1/segments/w0.jsonl"
    with seg.open("ab") as f:
        f.write(b'{"run_id":"r1","doc_')
    r = verify_run(s.log, s.blobs, "r1")
    assert r.ok and not r.closed
    assert [(p.kind, p.fails, p.segment) for p in r.problems] == [("torn_tail", False, "w0.jsonl")]
    # Once the experiment run is sealed it is closed: the same bytes are final, reported,
    # and still not a failure (they were never a whole event; nothing committed is lost).
    seal_run(s.log, "r1")
    r = verify_run(s.log, s.blobs, "r1")
    assert r.closed and r.ok
    assert [(p.kind, p.fails) for p in r.problems] == [("torn_tail", False)]
    assert "final" in r.problems[0].detail and "no committed event is lost" in r.problems[0].detail


def test_torn_tail_never_fails_a_service_period_run(tmp_path: Path) -> None:
    s = build_sealed_store(tmp_path / "svc", kind="service_period")
    with (run_dir(s) / "segments" / "w2.jsonl").open("ab") as f:
        f.write(b'{"torn')
    r = verify(s)
    assert r.ok and not r.closed
    assert [(p.kind, p.fails) for p in r.problems] == [("torn_tail", False)]


def test_crash_mid_append_then_recovery_then_seal(store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fault matrix: a flush that writes half a line, then hits ENOSPC (a crash mid-append).
    faults = Faults(monkeypatch, store.root)
    w = store.log.open_writer("r1", "w0", flush_every=100)
    for d in ("d1", "d2", "d1"):
        w.append(doc_id=d, node="n0", kind="decision")
    w.flush()
    faults.fail("write", faults.calls["write"] + 1, "short+ENOSPC")
    for d in ("d1", "d2", "d3"):
        w.append(doc_id=d, node="maybe-lost", kind="decision")
    with pytest.raises(OSError):
        w.flush()
    w.close()
    assert faults.fired
    on_disk = len(list(store.log.iter_stored("r1")))  # the whole lines the short write landed
    assert 3 <= on_disk < 6
    r = verify_run(store.log, store.blobs, "r1")
    assert r.ok and r.events_checked == on_disk
    assert [(p.kind, p.fails) for p in r.problems] == [("torn_tail", False)]
    heads = store.log.doc_heads("r1")
    # Reopen: the writer starts a new segment and continues every chain past the torn bytes.
    with store.log.open_writer("r1", "w0") as w2:
        assert w2.segment.name == "w0.1.jsonl"
        after = [w2.append(doc_id=d, node="after", kind="decision") for d in ("d1", "d2")]
    assert [e.prev_hash for e in after] == [heads["d1"], heads["d2"]]
    seal_run(store.log, "r1")
    r = verify_run(store.log, store.blobs, "r1")
    # Every chain, seal and column file verifies; the crash stays visible on the closed
    # run as a reported, non-failing torn tail (a recovered crash, nothing committed lost).
    assert r.events_checked == on_disk + 2 and r.seals_checked == 1
    assert r.ok and failing(r) == []
    assert [(p.kind, p.segment, p.fails) for p in r.problems] == [("torn_tail", "w0.jsonl", False)]


def test_killed_writer_process_then_recovery_verifies(store: SimpleNamespace) -> None:
    docs = ["k-d0", "k-d1"]
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=append_until_killed, args=(str(store.root), "r1", "w0", docs, 4))
    proc.start()
    seg = store.root / "runs/r1/segments/w0.jsonl"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and (not seg.exists() or seg.stat().st_size < 6 * 1024 * 1024):
        time.sleep(0.01)
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(timeout=30)
    assert proc.exitcode == -signal.SIGKILL
    torn = store.log.torn_tails("r1")
    with store.log.open_writer("r1", "w1") as w:
        for d in docs:
            w.append(doc_id=d, node="after_kill", kind="handoff")
    seal_run(store.log, "r1")
    r = verify_run(store.log, store.blobs, "r1")
    assert r.events_checked == sum(1 for _ in store.log.iter_stored("r1")) >= 4
    assert r.ok and failing(r) == []
    assert [(p.kind, p.segment, p.byte_offset) for p in r.problems] == [
        ("torn_tail", t.segment, t.byte_offset) for t in torn
    ]


def test_parallel_writer_processes_verify(store: SimpleNamespace) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=append_in_child, args=(str(store.root), "r1", f"p{k}", [f"p{k}-d{j}" for j in range(3)], 20))
        for k in range(3)
    ]
    try:
        for p in procs:
            p.start()
    finally:
        codes = join_all(procs, timeout=120)  # a worker that overruns is killed and reaped
    assert codes == [0] * len(procs)
    seal_run(store.log, "r1")
    r = verify_run(store.log, store.blobs, "r1")
    assert r.ok and r.problems == []
    assert (r.events_checked, r.documents_checked) == (3 * 3 * 20, 9)


# --- blobs -----------------------------------------------------------------------------


def test_bit_rotted_blob_is_corrupt_blob(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    sha = s.originals["d4"]  # named by every event of d4
    path = s.blobs.content_path(sha)
    flip_byte(path, path.stat().st_size // 2, 0x40)
    r = verify(s)
    assert failing(r) == [("corrupt_blob", "d4", 0)]
    assert "named by 3 events" in r.problems[0].detail
    # put() does not repair present content, so the damage stays until someone acts.
    s.blobs.put(blob_bytes("original d4"), "original")
    assert failing(verify(s)) == [("corrupt_blob", "d4", 0)]
    assert_no_leak(r)


def test_blob_replaced_by_other_valid_content_is_corrupt_blob(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    sha = s.outputs[0][2]
    s.blobs.content_path(sha).write_bytes(zstandard.ZstdCompressor().compress(b"other bytes"))
    r = verify(s)
    assert [p.kind for p in r.problems] == ["corrupt_blob"]
    assert "another SHA-256" in r.problems[0].detail


def test_truncated_blob_is_corrupt_blob(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    path = s.blobs.content_path(s.originals["d2"])
    path.write_bytes(path.read_bytes()[:7])
    assert failing(verify(s)) == [("corrupt_blob", "d2", 0)]


def test_content_next_to_its_tombstone_is_reported(sealed_store: SimpleNamespace) -> None:
    # A crash between writing the tombstone and unlinking the content, or content restored.
    s = sealed_store
    sha = s.outputs[1][2]
    saved = s.blobs.content_path(sha).read_bytes()
    s.blobs.tombstone(sha, "deletion request")
    assert verify(s).ok
    s.blobs.content_path(sha).write_bytes(saved)
    r = verify(s)
    assert [p.kind for p in r.problems] == ["tombstone_with_content"]
    assert r.tombstoned_blobs == 0
    assert_no_leak(r)


def test_one_missing_blob_named_by_many_events_is_one_problem(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    remove_blob_file(s, s.originals["d1"])
    r = verify(s)
    assert failing(r) == [("missing_blob", "d1", 0)]
    assert "named by 5 events" in r.problems[0].detail


# --- seals -----------------------------------------------------------------------------


def edit_seal(store: SimpleNamespace, n: int, rehash: bool, **changes: Any) -> None:
    path = seal_path(store, n)
    raw = json.loads(path.read_bytes()) | changes
    if rehash:
        raw["hash"] = seal_hash(raw)
    path.write_bytes(canonical_json(raw))


def test_edited_seal_does_not_hash(sealed_store: SimpleNamespace) -> None:
    edit_seal(sealed_store, 1, rehash=False, sealed_at="2026-11-18T14:30:00Z")
    r = verify(sealed_store)
    assert [(p.kind, p.seal) for p in r.problems] == [("seal_mismatch", 1)]
    assert "does not hash" in r.problems[0].detail


def test_rehashed_seal_breaks_the_next_seal_link(sealed_store: SimpleNamespace) -> None:
    edit_seal(sealed_store, 1, rehash=True, sealed_at="2026-11-18T14:30:00Z")
    r = verify(sealed_store)
    assert [(p.kind, p.seal) for p in r.problems] == [("seal_mismatch", 2)]
    assert "does not chain" in r.problems[0].detail


def test_seal_with_a_wrong_event_count_is_mismatch(sealed_store: SimpleNamespace) -> None:
    edit_seal(sealed_store, 2, rehash=True, event_count=13)
    r = verify(sealed_store)
    assert [(p.kind, p.seal) for p in r.problems] == [("seal_mismatch", 2)]
    assert "counts 13" in r.problems[0].detail


def test_seal_whose_document_list_disagrees_with_its_heads(sealed_store: SimpleNamespace) -> None:
    edit_seal(sealed_store, 2, rehash=True, document_ids=["d1", "d2", "d3"])  # hashes; the list is wrong
    r = verify(sealed_store)
    assert [(p.kind, p.seal) for p in r.problems] == [("seal_mismatch", 2)]


def test_later_seal_that_drops_a_document_or_goes_backwards(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    raw = json.loads(seal_path(s, 2).read_bytes())
    heads = dict(raw["doc_heads"])
    heads.pop("d4")
    heads["d1"] = json.loads(find_line(s, "d1", 2)[2])["hash"]  # an earlier head than seal 1's
    edit_seal(s, 2, rehash=True, doc_heads=heads, document_ids=sorted(heads), event_count=9)
    r = verify(s)
    assert [(p.kind, p.doc_id, p.seal) for p in r.problems] == [
        ("seal_mismatch", "d1", 2),  # an earlier head than seal 1 committed
        ("seal_mismatch", "d4", 2),  # dropped
    ]


def test_corrupt_seal_file_is_seal_corrupt(sealed_store: SimpleNamespace) -> None:
    seal_path(sealed_store, 1).write_bytes(b'{"n": 1, "trunc')
    r = verify(sealed_store)
    # Seal 2's link to seal 1 and its column rows cannot be checked; nothing else fails.
    assert [(p.kind, p.seal) for p in r.problems] == [("seal_corrupt", 1)]


def test_seal_file_with_another_number_is_seal_corrupt(sealed_store: SimpleNamespace) -> None:
    shutil.copy(seal_path(sealed_store, 0), seal_path(sealed_store, 1))
    r = verify(sealed_store)
    assert [(p.kind, p.seal) for p in r.problems] == [("seal_corrupt", 1)]


def test_deleted_middle_seal_is_seal_missing(sealed_store: SimpleNamespace) -> None:
    seal_path(sealed_store, 1).unlink()
    r = verify(sealed_store)
    assert [(p.kind, p.seal) for p in r.problems] == [("seal_missing", 1)]


def test_deleted_last_seal_is_found_by_its_column_file(sealed_store: SimpleNamespace) -> None:
    seal_path(sealed_store, 2).unlink()
    r = verify(sealed_store)
    assert [(p.kind, p.seal) for p in r.problems] == [("seal_missing", 2)]
    assert "column file remains" in r.problems[0].detail


def test_huge_seal_number_is_cheap(sealed_store: SimpleNamespace) -> None:
    shutil.copy(seal_path(sealed_store, 2), seal_path(sealed_store, 10**9))
    start = time.monotonic()
    r = verify(sealed_store)
    assert time.monotonic() - start < 5
    assert sorted((p.kind, p.seal) for p in r.problems) == [("seal_corrupt", 10**9), ("seal_missing", 3)]
    assert "seals 3..999999999" in next(p.detail for p in r.problems if p.kind == "seal_missing")


@pytest.mark.parametrize("recompute", [False, True])
def test_seal_refuses_to_commit_over_damage_and_verify_reports_it(sealed_store: SimpleNamespace, recompute: bool) -> None:
    # Tamper an unsealed event, append after it, seal: seal_run re-hashes and re-links the
    # lines it seals, so it refuses instead of committing the damage; verify reports it.
    s = sealed_store
    with s.log.open_writer("r1", "w6") as w:
        w.append(doc_id="d4", node="a", kind="decision")
        w.append(doc_id="d4", node="b", kind="decision")
    rewrite_line(s, "d4", 3, recompute_hash=recompute, cause="tampered")
    with s.log.open_writer("r1", "w6") as w:
        w.append(doc_id="d4", node="c", kind="decision")
    want_msg = r"'d4' seq 4 .*prev_hash" if recompute else r"'d4' seq 3 .*does not hash"
    with pytest.raises(SealRefused, match=want_msg):
        seal_run(s.log, "r1")
    r = verify(s)
    want = [("broken_link", "d4", 4)] if recompute else [("bad_hash", "d4", 3), ("broken_link", "d4", 4)]
    assert failing(r) == want
    assert r.seals_checked == 3


def test_unsealed_events_after_the_last_seal_are_fine(sealed_store: SimpleNamespace) -> None:
    # A closed run gets no later seal: events past its last seal are committed by none,
    # so verify notes each such document and how many (never failing the run).
    s = sealed_store
    with s.log.open_writer("r1", "w7") as w:
        w.append(doc_id="d5", node="n", kind="decision")
        w.append(doc_id="d1", node="n", kind="decision")
        w.append(doc_id="d1", node="n", kind="decision")
    r = verify(s)
    assert r.ok and r.closed and r.events_checked == TOTAL_EVENTS + 3
    assert [(p.kind, p.doc_id, p.seq, p.seal, p.fails) for p in r.problems] == [
        ("unsealed_events", "d1", 5, 2, False),
        ("unsealed_events", "d5", 0, 2, False),
    ]
    assert r.problems[0].detail.startswith("2 events from seq 5 after seal 2")
    assert r.problems[1].detail.startswith("1 event from seq 0 after seal 2")
    # Losing them leaves nothing to detect, which is why the note exists: cut d1's last
    # event and only the count changes.
    seg = s.log.segment_paths("r1")
    w7 = next(p for p in seg if p.name == "w7.jsonl")
    data = w7.read_bytes()
    w7.write_bytes(data[: data.rstrip(b"\n").rfind(b"\n") + 1])
    r = verify(s)
    assert r.ok and [(p.kind, p.doc_id, p.detail.split(" after")[0]) for p in r.problems] == [
        ("unsealed_events", "d1", "1 event from seq 5"),
        ("unsealed_events", "d5", "1 event from seq 0"),
    ]
    seal_run(s.log, "r1")  # sealing again commits them: nothing left to note
    assert verify(s).problems == []


def test_unsealed_events_are_not_noted_on_a_run_that_is_not_closed(tmp_path: Path) -> None:
    # A service_period run is sealed on a timer and never closed: a later seal commits them.
    s = build_sealed_store(tmp_path / "svc", kind="service_period")
    with s.log.open_writer("r1", "w7") as w:
        w.append(doc_id="d1", node="n", kind="decision")
    r = verify(s)
    assert r.ok and not r.closed and r.problems == []


def test_unsealed_events_are_judged_against_the_highest_head_any_seal_commits(sealed_store: SimpleNamespace) -> None:
    # Seal 2 drops d4 and takes d1 back to an earlier head: both fail as seal_mismatch, and
    # neither document gets a note, since seal 1 committed their events.
    s = sealed_store
    raw = json.loads(seal_path(s, 2).read_bytes())
    heads = dict(raw["doc_heads"])
    heads.pop("d4")
    heads["d1"] = json.loads(find_line(s, "d1", 2)[2])["hash"]
    edit_seal(s, 2, rehash=True, doc_heads=heads, document_ids=sorted(heads), event_count=9)
    r = verify(s)
    assert "unsealed_events" not in kinds(r)
    # With the last seal unreadable, what it committed is unknown: no note either.
    with s.log.open_writer("r1", "w7") as w:
        w.append(doc_id="d1", node="n", kind="decision")
    seal_path(s, 2).write_bytes(b"{")
    r = verify(s)
    assert "seal_corrupt" in kinds(r) and "unsealed_events" not in kinds(r)


# --- column files ----------------------------------------------------------------------


def test_missing_column_file(sealed_store: SimpleNamespace) -> None:
    columns_path(sealed_store, 1).unlink()
    r = verify(sealed_store)
    assert [(p.kind, p.seal) for p in r.problems] == [("columns_missing", 1)]
    columns_path(sealed_store, 2).unlink()
    r = verify(sealed_store)
    assert [(p.kind, p.seal) for p in r.problems] == [("columns_missing", 1), ("columns_missing", 2)]
    assert all("seal_run rebuilds it" in p.detail for p in r.problems)
    # The rebuild the detail promises: every lost file comes back and matches the log.
    seal_run(sealed_store.log, "r1")
    assert verify(sealed_store).problems == []


@pytest.mark.parametrize("damage", ["truncate", "zero_middle", "garbage"])
def test_corrupt_column_file_is_mismatch_not_crash(sealed_store: SimpleNamespace, damage: str) -> None:
    path = columns_path(sealed_store, 0)
    data = path.read_bytes()
    if damage == "truncate":
        path.write_bytes(data[: len(data) // 2])
    elif damage == "zero_middle":
        mid = len(data) // 3
        path.write_bytes(data[:mid] + b"\x00" * 64 + data[mid + 64 :])
    else:
        path.write_bytes(b"PAR1 garbage")
    r = verify(sealed_store)
    assert r.problems and {(p.kind, p.seal) for p in r.problems} == {("columns_mismatch", 0)}


def rewrite_columns(store: SimpleNamespace, n: int, edit: Any, schema: pa.Schema = EVENT_SCHEMA) -> None:
    rows = pq.read_table(columns_path(store, n)).to_pylist()
    rows = edit(rows)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), columns_path(store, n))


def test_tampered_column_value_names_the_row(sealed_store: SimpleNamespace) -> None:
    def edit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for row in rows:
            if (row["doc_id"], row["seq"]) == ("d3", 1):
                row["tokens_in"] = 999
        return rows

    rewrite_columns(sealed_store, 0, edit)
    r = verify(sealed_store)
    assert [(p.kind, p.doc_id, p.seq, p.seal) for p in r.problems] == [("columns_mismatch", "d3", 1, 0)]


def test_dropped_and_extra_column_rows(sealed_store: SimpleNamespace) -> None:
    def edit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = [r for r in rows if (r["doc_id"], r["seq"]) != ("d4", 1)]
        extra = dict(kept[0], doc_id="ghost", seq=0)
        return kept + [extra]

    rewrite_columns(sealed_store, 1, edit)
    r = verify(sealed_store)
    assert sorted((p.kind, p.doc_id, p.seq) for p in r.problems) == [
        ("columns_mismatch", "d4", 1),
        ("columns_mismatch", "ghost", 0),
    ]


def test_column_row_of_an_event_another_seal_covers(sealed_store: SimpleNamespace) -> None:
    # Seal 1's file repeats a row seal 0 already exported.
    first = pq.read_table(columns_path(sealed_store, 0)).to_pylist()[0]
    rewrite_columns(sealed_store, 1, lambda rows: rows + [first])
    r = verify(sealed_store)
    assert [(p.kind, p.doc_id, p.seal) for p in r.problems] == [("columns_mismatch", first["doc_id"], 1)]


def test_column_file_with_a_payload_column_is_mismatch(sealed_store: SimpleNamespace) -> None:
    schema = EVENT_SCHEMA.append(pa.field("payload", pa.string()))
    rewrite_columns(sealed_store, 2, lambda rows: [r | {"payload": SENTINEL} for r in rows], schema)
    r = verify(sealed_store)
    assert [(p.kind, p.seal) for p in r.problems] == [("columns_mismatch", 2)]
    assert_no_leak(r)


def test_log_damage_and_column_damage_in_one_run_are_both_found(sealed_store: SimpleNamespace) -> None:
    # No masking: d1's chain is damaged (its rows are not compared), d3's column row is
    # tampered (its chain is fine) and d4's blob is gone. Each is reported, nothing else.
    s = sealed_store
    rewrite_line(s, "d1", 3, cause="tampered")

    def edit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r | {"node": "x"} if r["doc_id"] == "d3" else r for r in rows]

    rewrite_columns(s, 0, edit)
    remove_blob_file(s, s.originals["d4"])
    r = verify(s)
    assert sorted((p.kind, p.doc_id, p.seq) for p in r.problems) == [
        ("bad_hash", "d1", 3),
        ("broken_link", "d1", 4),
        ("columns_mismatch", "d3", 0),
        ("missing_blob", "d4", 0),
    ]


# --- run header, unknown runs, leaks ----------------------------------------------------


def test_unknown_run_raises(sealed_store: SimpleNamespace) -> None:
    with pytest.raises(FileNotFoundError):
        verify_run(*sealed_store.parts, "nope")
    with pytest.raises(ValueError):
        verify_run(*sealed_store.parts, "../r1")


@pytest.mark.parametrize("damage", ["delete", "garbage", "other_run"])
def test_damaged_run_header_is_reported(sealed_store: SimpleNamespace, damage: str) -> None:
    path = run_dir(sealed_store) / "run.json"
    if damage == "delete":
        path.unlink()
    elif damage == "garbage":
        path.write_bytes(b"\x00not json")
    else:
        path.write_bytes(canonical_json(Run(**run_fields(run_id="r2")).model_dump(mode="json")))
    r = verify(sealed_store)
    assert failing(r) == [("corrupt_run_header", None, None)]
    assert r.events_checked == TOTAL_EVENTS and r.seals_checked == 3  # the rest still checked


def test_report_carries_no_document_text_under_heavy_damage(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    rewrite_line(s, "d1", 0, decision={"text": SENTINEL})
    rewrite_line(s, "d2", 1, recompute_hash=True, latency_ms=SENTINEL)
    seg, off, _ = find_line(s, "d3", 0)
    replace_bytes(seg, off, 0, SENTINEL.encode() + b"\n")
    for sha in s.originals.values():
        flip_byte(s.blobs.content_path(sha), 20, 0x10)
    seal_path(s, 1).write_bytes(SENTINEL.encode())
    r = verify(s)
    assert not r.ok and {"bad_hash", "invalid_event", "corrupt_line", "corrupt_blob", "seal_corrupt"} <= kinds(r)
    assert_no_leak(r)


def test_every_problem_kind_is_produced_by_some_test() -> None:
    # Coverage target (docs/test-strategy-record.md): every Problem.kind has a test.
    source = Path(__file__).read_text()
    for kind in PROBLEM_KINDS:
        assert f'"{kind}"' in source, kind


def test_the_readme_lists_every_problem_kind() -> None:
    import re

    text = (Path(__file__).parents[1] / "README.md").read_text()
    block = text[text.index("Problem kinds:") : text.index("The module", text.index("Problem kinds:"))]
    assert set(re.findall(r"`([a-z_]+)`", block)) == set(PROBLEM_KINDS)


# --- large runs ------------------------------------------------------------------------


def test_verify_streams_large_payloads(store: SimpleNamespace) -> None:
    # 96 events of ~256 KB each (24 MB of lines): verify holds about one line at a time.
    pad = "p" * (256 * 1024)
    with store.log.open_writer("r1", "w0", flush_every=8) as w:
        for i in range(96):
            w.append(doc_id=f"d{i % 4}", node="n", kind="decision", payload={"pad": pad, "i": i})
    seal_run(store.log, "r1")
    tracemalloc.start()
    try:
        r = verify_run(store.log, store.blobs, "r1")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert r.ok and r.events_checked == 96
    assert peak < 6 * 1024 * 1024, f"peak {peak} bytes: verify is holding payloads"


# --- properties ------------------------------------------------------------------------

# One history step: (writer index, document index, number of events), or None for a seal.
STEP = st.one_of(st.none(), st.tuples(st.integers(0, 2), st.integers(0, 3), st.integers(1, 3)))


def play(root: Path, steps: list[Any], kind: str = "specialist_only") -> SimpleNamespace:
    blobs = BlobStore(root)
    log = EventLog(root, blobs)
    log.create_run(Run(**run_fields(kind=kind)))
    for i, step in enumerate(steps):
        if step is None:
            seal_run(log, "r1", sealed_at="2026-11-18T13:00:00Z")
            continue
        w_i, d_i, n = step
        with log.open_writer("r1", f"w{w_i}", flush_every=2) as w:
            for j in range(n):
                outs = [blobs.put(f"blob {i} {j}".encode(), "extraction")] if j == n - 1 else []
                w.append(doc_id=f"d{d_i}", node=f"s{i}", kind="output", outputs=outs, payload={"j": j})
    return SimpleNamespace(root=root, blobs=blobs, log=log, run_id="r1")


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(steps=st.lists(STEP, min_size=1, max_size=8), data=st.data())
def test_clean_histories_verify_and_any_flipped_byte_is_caught(
    tmp_path_factory: pytest.TempPathFactory, steps: list[Any], data: st.DataObject
) -> None:
    s = play(tmp_path_factory.mktemp("prop"), steps)
    r = verify(s)
    assert r.ok and [p for p in r.problems if p.kind not in ("torn_tail", "unsealed_events")] == []
    n_events = sum(step[2] for step in steps if step is not None)
    assert r.events_checked == n_events
    # A sealed experiment run is closed: exactly the events after the last seal are noted.
    unsealed: dict[str, int] = {}
    if None in steps:
        last = max(i for i, step in enumerate(steps) if step is None)
        for _, d_i, n in (step for step in steps[last + 1 :] if step is not None):
            unsealed[f"d{d_i}"] = unsealed.get(f"d{d_i}", 0) + n
    noted = {p.doc_id: int(p.detail.split(" ")[0]) for p in r.problems if p.kind == "unsealed_events"}
    assert noted == unsealed and all(not p.fails for p in r.problems)

    files = sorted(s.log.segment_paths("r1")) + sorted((s.root / "runs/r1/seals").glob("*.json"))
    files = [f for f in files if f.stat().st_size > 0]
    if not files:
        return
    target = data.draw(st.sampled_from(files), label="file")
    size = target.stat().st_size
    offset = data.draw(st.integers(0, size - 1), label="offset")
    mask = data.draw(st.integers(1, 255), label="mask")
    flip_byte(target, offset, mask)
    r = verify(s)
    caught = not r.ok
    in_torn_tail = any(
        p.kind == "torn_tail" and p.segment == target.name and p.byte_offset <= offset for p in r.problems
    )
    assert caught or in_torn_tail, (target.name, offset, mask, r.problems)


DAMAGE = st.sampled_from(["delete", "truncate", "flip", "garbage", "empty", "deep"])


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(data=st.data())
def test_verify_never_raises_on_any_damage(tmp_path_factory: pytest.TempPathFactory, data: st.DataObject) -> None:
    # Random destructive operations on any stored file (segments, seals, column files, blobs,
    # tombstones, the run header), deep nesting included: verify always returns a report and
    # never raises.
    s = build_sealed_store(tmp_path_factory.mktemp("never"))
    files = sorted(p for p in s.root.rglob("*") if p.is_file() and not p.name.startswith(".") and p.suffix != ".tmp")
    for _ in range(data.draw(st.integers(1, 4), label="n")):
        # Draw everything up front, independent of file sizes (they vary with timestamps).
        target = data.draw(st.sampled_from(files), label="file")
        how = data.draw(DAMAGE, label="how")
        where = data.draw(st.integers(0, 2**32), label="where")
        mask = data.draw(st.integers(1, 255), label="mask")
        junk = data.draw(st.binary(max_size=64), label="junk")
        if not target.exists():
            continue
        size = target.stat().st_size
        if how == "delete":
            target.unlink()
        elif how == "truncate":
            target.write_bytes(target.read_bytes()[: where % max(1, size)])
        elif how == "flip" and size:
            flip_byte(target, where % size, mask)
        elif how == "garbage":
            target.write_bytes(junk)
        elif how == "empty":
            target.write_bytes(b"")
        elif how == "deep":  # nesting deeper than the JSON parser allows, as a whole line
            target.write_bytes(DEEP + b"\n")
    r = verify(s)
    assert isinstance(r.ok, bool) and all(isinstance(p, Problem) for p in r.problems)
    assert all(p.kind in PROBLEM_KINDS for p in r.problems)
    assert_no_leak(r)


# --- fix pass: verify never raises on deep nesting (Task 7 review, BLOCKING 1) ----------

DEEP = DEEP_UNPARSABLE  # deeper than the JSON parser allows


def _deep_payload_line(store: SimpleNamespace, doc_id: str, seq: int, prev: str) -> bytes:
    # Parses, hashes and is canonical on every supported Python, but nests deeper than
    # validation can recurse.
    return deep_payload_line("r1", doc_id, seq, prev)


@pytest.mark.parametrize("where", ["segment", "segment_payload", "seal", "run_header", "tombstone"])
def test_deep_nesting_is_reported_not_raised(sealed_store: SimpleNamespace, where: str) -> None:
    s = sealed_store
    if where == "segment":
        with (run_dir(s) / "segments" / "w0.jsonl").open("ab") as f:
            f.write(DEEP + b"\n")
        want = ("corrupt_line", None, None)
    elif where == "segment_payload":
        head = s.log.doc_heads("r1")["d2"]
        with (run_dir(s) / "segments" / "w0.jsonl").open("ab") as f:
            f.write(_deep_payload_line(s, "d2", last_seq(s, "d2") + 1, head))
        want = ("invalid_event", "d2", 3)
    elif where == "seal":
        seal_path(s, 2).write_bytes(DEEP)
        want = ("seal_corrupt", None, None)
    elif where == "run_header":
        (run_dir(s) / "run.json").write_bytes(DEEP)
        want = ("corrupt_run_header", None, None)
    else:
        sha = s.originals["d1"]
        s.blobs.tombstone(sha, "erasure request")
        s.blobs.tombstone_path(sha).write_bytes(DEEP)
        want = ("corrupt_tombstone", "d1", 0)
    r = verify(s)
    assert want in failing(r), failing(r)
    assert_no_leak(r)


@pytest.mark.parametrize("deep", ["unparsable", "payload"])
def test_seal_run_refuses_deep_nesting_without_recursion_error(store_with_events: SimpleNamespace, deep: str) -> None:
    s = store_with_events
    seg = s.root / "runs/r1/segments/deep.jsonl"
    if deep == "unparsable":
        seg.write_bytes(DEEP + b"\n")
    else:
        seg.write_bytes(_deep_payload_line(s, "g", 0, GENESIS))
    with pytest.raises(CorruptLine, match="deep.jsonl at byte 0"):
        seal_run(s.log, "r1")


# --- fix pass: a run written while it is verified (Task 7 review, MAJOR 2) --------------


def _after_segment(monkeypatch: pytest.MonkeyPatch, name: str, action: Any) -> list[bool]:
    """Run ``action`` once, right after verify has scanned segment ``name``."""
    original = verify_module.iter_raw_segment
    done: list[bool] = []

    def hooked(path: Path, *args: Any, **kwargs: Any) -> Any:
        yield from original(path, *args, **kwargs)
        if path.name == name and not done:
            done.append(True)
            action()

    monkeypatch.setattr(verify_module, "iter_raw_segment", hooked)
    return done


def test_seal_made_during_the_scan_is_not_a_false_failure(
    sealed_store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Once verify has scanned w0.jsonl, a writer appends there (d1 and a new document) and
    # the timer seals. That seal commits heads the scan never saw; reading it would report
    # truncated_after_seal and document_missing_from_log on a healthy run.
    s = sealed_store

    def write_and_seal() -> None:
        with s.log.open_writer("r1", "w0") as w:
            w.append(doc_id="d1", node="late", kind="decision")
            w.append(doc_id="d9", node="late", kind="decision")
        seal_run(s.log, "r1")

    done = _after_segment(monkeypatch, "w0.jsonl", write_and_seal)
    r = verify(s)
    assert done and len(list((run_dir(s) / "seals").glob("*.json"))) == 4
    assert r.ok and r.problems == [], failing(r)
    monkeypatch.undo()
    assert verify(s).ok  # and the quiet run verifies, with the new seal


def test_handoff_during_the_scan_is_not_a_seq_gap(sealed_store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    # w0 (already scanned) takes d1 for one event, then hands it to w2 (scanned later):
    # the scan sees d1's next seq but not the one before it.
    s = sealed_store

    def handoff() -> None:
        with s.log.open_writer("r1", "w0") as w:
            w.append(doc_id="d1", node="handoff", kind="decision")
        with s.log.open_writer("r1", "w2") as w:
            w.append(doc_id="d1", node="handoff", kind="decision")

    done = _after_segment(monkeypatch, "w0.jsonl", handoff)
    r = verify(s)
    assert done
    # The run is closed (sealed), so the two late events are noted, never a gap.
    assert r.ok and [(p.kind, p.doc_id, p.seq) for p in r.problems] == [("unsealed_events", "d1", 5)], failing(r)
    # The rescan keeps seqs up to the head the first scan saw (the event in w2), and the
    # one in w0 below it was on disk by then: both are checked.
    assert r.events_checked == TOTAL_EVENTS + 2


def test_real_gap_is_still_reported_when_the_scan_races_a_handoff(
    sealed_store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = sealed_store
    delete_line(s, "d1", 1)  # a real loss below a sealed head (not a head itself)

    def handoff() -> None:
        with s.log.open_writer("r1", "w0") as w:
            w.append(doc_id="d1", node="handoff", kind="decision")
        with s.log.open_writer("r1", "w2") as w:
            w.append(doc_id="d1", node="handoff", kind="decision")

    _after_segment(monkeypatch, "w0.jsonl", handoff)
    r = verify(s)
    assert failing(r) == [("seq_gap", "d1", 1)]


def test_seal_without_its_column_file_yet_is_recoverable(sealed_store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    # A crash (or a seal_run still running) between the seal file and its column file.
    s = sealed_store
    with s.log.open_writer("r1", "w5") as w:
        w.append(doc_id="d1", node="n", kind="decision")
    from mailroom_record import seal as seal_mod

    def crash(path: Path, table: Any, *args: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(seal_mod, "_write_columns", crash)
    with pytest.raises(KeyboardInterrupt):
        seal_run(s.log, "r1")
    monkeypatch.undo()
    r = verify(s)
    assert r.ok, failing(r)
    assert [(p.kind, p.seal, p.fails) for p in r.problems] == [("columns_missing", 3, False)]
    assert "seal_run rebuilds it" in r.problems[0].detail
    # The run is closed: no later seal will come, so the detail asks for one more seal_run.
    assert "closing seal of a closed run" in r.problems[0].detail
    assert "run seal_run once more" in r.problems[0].detail
    seal_run(s.log, "r1")  # the recovery the detail names
    assert verify(s).problems == []
    # An older seal's lost column file still fails: no crash leaves that state.
    columns_path(s, 1).unlink()
    assert failing(verify(s)) == [("columns_missing", None, None)]


def test_latest_column_file_missing_on_an_open_run_waits_for_the_next_seal(tmp_path: Path) -> None:
    # On a service_period run (never closed) the next timed seal rebuilds it: no extra advice.
    s = build_sealed_store(tmp_path / "svc", kind="service_period")
    columns_path(s, 2).unlink()
    r = verify(s)
    assert r.ok and not r.closed
    assert [(p.kind, p.seal, p.fails) for p in r.problems] == [("columns_missing", 2, False)]
    assert "(the latest seal: a crash or a seal in progress, recoverable)" in r.problems[0].detail
    assert "closed run" not in r.problems[0].detail


def test_verify_a_busy_run_while_another_process_seals(store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    # Real processes: four writers hand their documents between writer ids on every event,
    # another process seals every 50 ms, and verify scans slowly (20 ms per segment).
    # The run never has a problem, so every report must be ok.
    import multiprocessing as mp

    stop, procs = start_handoff_workers(store.root, 4)
    ctx = mp.get_context("spawn")
    sealer = ctx.Process(target=seal_until_stopped, args=(str(store.root), "r1", stop, 0.05))
    sealer.start()
    original = verify_module.iter_raw_segment

    def slow(path: Path, *args: Any, **kwargs: Any) -> Any:
        time.sleep(0.02)
        yield from original(path, *args, **kwargs)

    monkeypatch.setattr(verify_module, "iter_raw_segment", slow)
    reports = []
    first_seal = store.root / "runs" / "r1" / "seals" / "0.json"
    try:
        # Readiness: the sealer has committed its first seal (seal files are written whole
        # and atomically), so every report below has a seal to check.
        ready_by = time.monotonic() + 60
        while not first_seal.exists():
            assert time.monotonic() < ready_by, "the sealer process never wrote its first seal"
            assert sealer.is_alive() or first_seal.exists(), "the sealer process exited before its first seal"
            time.sleep(0.01)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and len(reports) < 6:
            reports.append(verify_run(store.log, store.blobs, "r1"))
    finally:
        stop_workers(stop, procs + [sealer])
    bad = [failing(r) for r in reports if not r.ok]
    assert bad == [], f"{len(bad)} of {len(reports)} reports failed a healthy run: {bad[0]}"
    assert any(r.seals_checked for r in reports)
    monkeypatch.undo()
    seal_run(store.log, "r1")
    r = verify_run(store.log, store.blobs, "r1")
    assert r.ok and r.problems == []


# --- fix pass: damage is scoped to the seals it touches (Task 7 review, MINOR 3) ------


def _tamper_column_row(path: Path, doc_id: str, seq: int, **changes: Any) -> None:
    rows = pq.read_table(path).to_pylist()
    for row in rows:
        if (row["doc_id"], row["seq"]) == (doc_id, seq):
            row.update(changes)
    pq.write_table(pa.Table.from_pylist(rows, schema=EVENT_SCHEMA), path)


def test_damage_after_the_last_seal_does_not_mask_a_tampered_column_file(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    _tamper_column_row(columns_path(s, 0), "d1", 1, node="EDITED")
    # An unsealed, invalid (but hashed and linked) event on the same document.
    head = s.log.doc_heads("r1")["d1"]
    forge_event(s, "d1", last_seq(s, "d1") + 1, head)
    rewrite_line(s, "d1", 5, recompute_hash=True, attempt=-1)
    r = verify(s)
    assert ("invalid_event", "d1", 5) in failing(r)
    assert ("columns_mismatch", "d1", 1) in failing(r), failing(r)


def test_damage_inside_a_seal_still_skips_only_that_seal(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    rewrite_line(s, "d1", 4, recompute_hash=False, cause="tampered")  # in seal 1's rows of d1
    _tamper_column_row(columns_path(s, 0), "d1", 0, node="EDITED")  # seal 0's rows of d1
    r = verify(s)
    assert ("bad_hash", "d1", 4) in failing(r)
    assert ("columns_mismatch", "d1", 0) in failing(r)
    assert not any(p.kind == "columns_mismatch" and p.seal == 1 for p in r.problems)


# --- fix pass: tombstones are parsed (Task 7 review, MINOR 4) ---------------------------


@pytest.mark.parametrize("bad", ["empty", "directory", "not_json", "other_hash", "no_reason", "list"])
def test_a_tombstone_that_is_not_a_deletion_record_fails(sealed_store: SimpleNamespace, bad: str) -> None:
    s = sealed_store
    sha = s.originals["d3"]
    s.blobs.tombstone(sha, "erasure request")
    path = s.blobs.tombstone_path(sha)
    record = json.loads(path.read_bytes())
    if bad == "directory":
        path.unlink()
        path.mkdir()
    else:
        path.write_bytes({
            "empty": b"",
            "not_json": b"\xff not json",
            "other_hash": canonical_json(record | {"sha256": "0" * 64}),
            "no_reason": canonical_json(record | {"reason": "  "}),
            "list": b"[]",
        }[bad])
    r = verify(s)
    assert failing(r) == [("corrupt_tombstone", "d3", 0)]
    assert r.tombstoned_blobs == 0


def test_a_lawful_tombstone_passes(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    s.blobs.tombstone(s.originals["d3"], "erasure request")
    r = verify(s)
    assert r.ok and r.tombstoned_blobs == 1


def test_tombstoned_between_the_two_checks_counts_as_tombstoned(
    sealed_store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BlobStore.tombstone writes the record, then unlinks the content. Verify looks at the
    # tombstone path, then the content path: a deletion in between shows neither at first.
    s = sealed_store
    sha = s.originals["d3"]
    tomb = s.blobs.tombstone_path(sha)
    original_exists = Path.exists
    seen = []

    def exists(self: Path) -> bool:
        if self == tomb and not seen:
            seen.append(True)
            s.blobs.tombstone(sha, "erasure request")  # lands right after the first look
            return False
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", exists)
    r = verify(s)
    monkeypatch.undo()
    assert seen
    assert r.ok and r.problems == [] and r.tombstoned_blobs == 1


# --- fix pass: invalid values, canonical forms, hostile limits (MINOR 5, 6, 7) ---------


def test_integer_parquet_cannot_hold_is_invalid_event(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    head = s.log.doc_heads("r1")["d4"]
    forge_event(s, "d4", last_seq(s, "d4") + 1, head, tokens_in=2**70)
    r = verify(s)
    assert failing(r) == [("invalid_event", "d4", 3)]
    assert "tokens_in" in r.problems[0].detail  # the model bounds every count to int64
    with pytest.raises(CorruptLine):  # seal_run refuses the same line
        seal_run(s.log, "r1")


def test_seal_file_not_in_canonical_form_is_corrupt(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    raw = json.loads(seal_path(s, 1).read_bytes())
    seal_path(s, 1).write_bytes(json.dumps(raw, indent=2).encode())  # same content, other bytes
    r = verify(s)
    assert ("seal_corrupt", None, None) in failing(r)
    assert any("canonical" in p.detail for p in r.problems if p.kind == "seal_corrupt")


def test_run_header_not_in_canonical_form_is_corrupt(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    path = run_dir(s) / "run.json"
    path.write_bytes(json.dumps(json.loads(path.read_bytes()), indent=2).encode())
    r = verify(s)
    assert failing(r) == [("corrupt_run_header", None, None)]
    assert "canonical" in r.problems[0].detail


def test_overlong_line_is_reported_without_being_held(store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_module, "MAX_LINE", 1024 * 1024)
    with store.log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="before", kind="decision")
    big = store.root / "runs/r1/segments/big.jsonl"
    with big.open("wb") as f:
        for _ in range(24):  # a 24 MB line with no newline until its end
            f.write(b"x" * (1024 * 1024))
        f.write(b"\n")
        f.write(canonical_json(hand_line("r1", "d2", 0, GENESIS)) + b"\n")  # a whole event after it
    tracemalloc.start()
    try:
        r = verify_run(store.log, store.blobs, "r1")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert failing(r) == [("corrupt_line", None, None)]
    assert (r.problems[0].segment, r.problems[0].byte_offset) == ("big.jsonl", 0)
    assert "a line of 25165824 bytes, over the 1048576-byte limit" in r.problems[0].detail
    assert r.events_checked == 2  # the event after it was still checked
    assert peak < 6 * 1024 * 1024, f"peak {peak} bytes: the long line was held"


def test_reader_limit_edges(tmp_path: Path) -> None:
    seg = tmp_path / "s.jsonl"
    seg.write_bytes(b"abcd\n" + b"x" * 10 + b"\n" + b"y" * 4 + b"\n" + b"z" * 10)
    items = list(iter_raw_segment(seg, max_line=4))
    assert items == [
        RawLine("s.jsonl", 0, b"abcd"),           # exactly the limit: read
        OverlongLine("s.jsonl", 5, 10),          # over it: skipped, size known
        RawLine("s.jsonl", 16, b"yyyy"),
        TornTail("s.jsonl", 21),                 # an over-long tail is still a torn tail
    ]
    assert list(iter_raw_segment(seg)) == [
        RawLine("s.jsonl", 0, b"abcd"), RawLine("s.jsonl", 5, b"x" * 10), RawLine("s.jsonl", 16, b"yyyy"),
        TornTail("s.jsonl", 21),
    ]


@pytest.mark.parametrize("which", ["segments", "seals", "columns"])
def test_unlistable_directories_are_reported(
    sealed_store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    s = sealed_store
    target = run_dir(s) / which
    original = Path.iterdir

    def iterdir(self: Path) -> Any:
        if self == target:
            raise PermissionError(13, "injected")
        return original(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    r = verify(s)
    monkeypatch.undo()
    want = {"segments": "corrupt_line", "seals": "seal_corrupt", "columns": "columns_missing"}[which]
    assert (want, None, None) in failing(r)
    assert any(f"{which}/ cannot be listed" in p.detail for p in r.problems)


# --- PR #2 re-review: probes that fail, races, forks after the last seal (items 12-17) ---


def _stat_fails_for(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    original = Path.stat

    def stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == target:
            raise PermissionError(13, "injected")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)


@pytest.mark.parametrize("which", ["content", "tombstone"])
def test_a_blob_probe_that_raises_is_reported_for_that_blob(
    sealed_store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    s = sealed_store
    sha = s.originals["d3"]
    path = s.blobs.content_path(sha) if which == "content" else s.blobs.tombstone_path(sha)
    _stat_fails_for(monkeypatch, path)
    r = verify(s)
    monkeypatch.undo()
    assert failing(r) == [("corrupt_blob", "d3", 0)]
    assert "cannot be checked (PermissionError, errno 13)" in r.problems[0].detail


def test_a_column_file_that_cannot_be_checked_is_reported_for_that_seal(
    sealed_store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = sealed_store
    _stat_fails_for(monkeypatch, columns_path(s, 1))
    r = verify(s)
    monkeypatch.undo()
    assert [(p.kind, p.seal, p.fails) for p in r.problems] == [("columns_mismatch", 1, True)]
    assert "cannot be checked (PermissionError, errno 13)" in r.problems[0].detail


@pytest.mark.parametrize("n", [1, 2])
def test_a_directory_at_a_column_file_path_fails(sealed_store: SimpleNamespace, n: int) -> None:
    # The latest seal's missing file is a recoverable note; a directory there is not
    # (seal_run cannot write the file over it).
    s = sealed_store
    columns_path(s, n).unlink()
    columns_path(s, n).mkdir()
    r = verify(s)
    assert [(p.kind, p.seal, p.fails) for p in r.problems] == [("columns_mismatch", n, True)]
    assert "is not a regular file" in r.problems[0].detail


@pytest.mark.parametrize("tombstoned", [True, False])
def test_content_deleted_while_it_is_read(
    sealed_store: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tombstoned: bool
) -> None:
    # The content probe saw the blob; the deletion completes before verify opens it.
    s = sealed_store
    sha = s.originals["d3"]
    content = s.blobs.content_path(sha)
    original = Path.open
    fired: list[bool] = []

    def open_(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == content and not fired:
            fired.append(True)
            if tombstoned:
                s.blobs.tombstone(sha, "erasure request")
            else:
                content.unlink()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_)
    r = verify(s)
    monkeypatch.undo()
    assert fired
    if tombstoned:
        assert r.ok and r.problems == [] and r.tombstoned_blobs == 1
    else:
        assert failing(r) == [("missing_blob", "d3", 0)]


def test_a_fork_after_the_last_seal_keeps_the_sealed_rows_compared(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    _tamper_column_row(columns_path(s, 0), "d1", 1, node="EDITED")
    head, seq = s.log.doc_heads("r1")["d1"], last_seq(s, "d1") + 1
    forge_event(s, "d1", seq, head, node="one")
    forge_event(s, "d1", seq, head, segment="w8.jsonl", node="two")
    r = verify(s)
    assert ("forked_chain", "d1", seq) in failing(r)
    assert ("columns_mismatch", "d1", 1) in failing(r), failing(r)


def test_one_event_naming_a_blob_twice_counts_once(store: SimpleNamespace) -> None:
    ref = store.blobs.put(blob_bytes("seen twice"), "original")
    with store.log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="n", kind="output", inputs=[ref], outputs=[ref.model_copy(update={"role": "extraction"})])
    remove_blob_file(store, ref.sha256)
    r = verify(store)
    assert failing(r) == [("missing_blob", "d1", 0)]
    assert "(named by 1 event)" in r.problems[0].detail


def test_a_fifo_at_the_tombstone_path_is_reported_not_blocked_on(sealed_store: SimpleNamespace) -> None:
    s = sealed_store
    sha = s.outputs[1][2]
    s.blobs.tombstone(sha, "deletion request")
    tomb = s.blobs.tombstone_path(sha)
    tomb.unlink()
    os.mkfifo(tomb)
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("verify blocked on a FIFO")))
    signal.alarm(20)
    try:
        r = verify(s)
    finally:
        signal.alarm(0)
    assert [p.kind for p in r.problems] == ["corrupt_tombstone"]
    assert "not a regular file" in r.problems[0].detail
