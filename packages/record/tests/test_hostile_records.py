"""Every reader survives hostile stored records, on every supported Python (3.11 to 3.13).

A damaged store must never crash a reader with an exception nobody expects
(``RecursionError``, ``AttributeError``, ``OverflowError``): strict readers raise the
documented typed errors, tolerant readers (the index rebuild, verify) record a problem and
carry on, and no error text quotes the stored value. Each case here lays the damage on a
real on-disk store and drives every reader over it.
"""

from __future__ import annotations

import json
import traceback
from types import SimpleNamespace

import pytest
from record_testkit import (
    DEEP_BUT_PARSABLE,
    DEEP_UNPARSABLE,
    SENTINEL,
    chain,
    deep_payload_line,
    forge_event,
    hand_line,
    verify,
    write_lines,
)

from mailroom_record.hashing import canonical_json
from mailroom_record.index import Index
from mailroom_record.log import CorruptLine, InvalidEvent
from mailroom_record.models import GENESIS, event_hash
from mailroom_record.seal import SealCorrupt, list_seals, seal_run

UNEXPECTED = (RecursionError, AttributeError, OverflowError)


def _index(store: SimpleNamespace) -> Index:
    return Index(store.root / "index.sqlite")


def _problem_kinds(stats: object) -> list[tuple[str, str | None]]:
    return [(p.kind, p.doc_id) for p in stats.problems]  # type: ignore[attr-defined]


# --- a line nested deeper than validation can recurse ----------------------------------


def test_deep_event_line_is_a_typed_error_or_a_problem_everywhere(store_with_events: SimpleNamespace) -> None:
    s = store_with_events
    # The bad line sits after d2's good head and before d3's events in one segment.
    head = s.log.doc_heads("r1")["d2"]
    seg = s.root / "runs/r1/segments/w9.jsonl"
    seg.write_bytes(deep_payload_line("r1", "d2", 1, head))
    write_lines(s.root, "w9.jsonl", chain("d3", 2))

    # Strict readers: a validation error (a ValueError), never RecursionError.
    with pytest.raises(ValueError) as caught:
        list(s.log.iter_events("r1"))
    assert not isinstance(caught.value, UNEXPECTED)
    with pytest.raises(CorruptLine, match="w9.jsonl at byte 0"):
        seal_run(s.log, "r1")

    # Tolerant readers: the line is a problem; everything else is read.
    scan = s.log.scan("r1", strict=False)
    assert [e.event.doc_id for e in scan.events()] == ["d1", "d1", "d2", "d3", "d3"]
    assert [(b.segment, b.byte_offset, b.kind) for b in scan.bad] == [("w9.jsonl", 0, "invalid_event")]
    stats = _index(s).reindex(s.log)
    assert ("invalid_event", None) in _problem_kinds(stats)
    assert {d.doc_id: d.events for d in _index(s).documents("r1")} == {"d1": 2, "d2": 1, "d3": 2}
    report = verify(s)
    assert ("invalid_event", "d2", 1) in [(p.kind, p.doc_id, p.seq) for p in report.problems]


@pytest.mark.parametrize("what", ["run_header", "seal"])
@pytest.mark.parametrize("damage", ["unparsable", "not_an_object"])
def test_deep_or_non_object_header_and_seal_files_are_reported(
    store_with_events: SimpleNamespace, what: str, damage: str
) -> None:
    s = store_with_events
    seal_run(s.log, "r1", sealed_at="2026-11-18T13:00:00Z")
    path = s.root / "runs/r1" / ("run.json" if what == "run_header" else "seals/0.json")
    path.write_bytes(DEEP_UNPARSABLE if damage == "unparsable" else b"[]")

    stats = _index(s).reindex(s.log)  # never raises on a damaged log
    if what == "run_header":
        with pytest.raises((ValueError, TypeError)) as caught:
            s.log.read_run("r1")
        assert not isinstance(caught.value, UNEXPECTED)
        assert _problem_kinds(stats) == [("corrupt_run_header", None)]
    else:
        with pytest.raises(SealCorrupt, match="0.json"):
            list_seals(s.log, "r1")
        with pytest.raises(SealCorrupt, match="0.json"):
            seal_run(s.log, "r1")
        assert _problem_kinds(stats) == [("seal_corrupt", None)]
    report = verify(s)
    want = "corrupt_run_header" if what == "run_header" else "seal_corrupt"
    assert want in {p.kind for p in report.problems}


# --- error text never quotes a stored value --------------------------------------------


def test_bad_schema_version_is_named_without_its_value(store: SimpleNamespace) -> None:
    s = store
    raw = hand_line("r1", "d1", 0, GENESIS)
    raw["schema_version"] = f"{SENTINEL} pasted into the version field"
    raw["hash"] = event_hash(raw)
    (s.root / "runs/r1/segments").mkdir(parents=True, exist_ok=True)
    (s.root / "runs/r1/segments/w0.jsonl").write_bytes(canonical_json(raw) + b"\n")

    with pytest.raises(CorruptLine) as sealed:
        seal_run(s.log, "r1")
    assert "schema_version" in str(sealed.value) and SENTINEL not in str(sealed.value)
    with pytest.raises(ValueError) as read:
        list(s.log.iter_events("r1"))
    assert SENTINEL not in str(read.value)
    scan = s.log.scan("r1", strict=False)
    list(scan.events())
    assert scan.bad and all(SENTINEL not in b.detail for b in scan.bad)
    stats = _index(s).reindex(s.log)
    assert stats.problems and all(SENTINEL not in p.detail for p in stats.problems)

    # The same holds for a seal file whose version field holds text.
    seal_run_dir = s.root / "runs/r1/seals"
    seal_run_dir.mkdir(parents=True, exist_ok=True)
    (seal_run_dir / "0.json").write_text(json.dumps({"schema_version": SENTINEL}))
    with pytest.raises(SealCorrupt) as bad_seal:
        list_seals(s.log, "r1")
    assert SENTINEL not in str(bad_seal.value)


# --- integers outside int64 (Task 8 review BLOCKING 1) ----------------------------------


@pytest.mark.parametrize("seq", [2**63, 2**64 + 5, 10**30])
def test_seq_beyond_int64_is_an_invalid_event_not_an_overflow(store: SimpleNamespace, seq: int) -> None:
    # The readers pack seq into array("q"); a stored seq past int64 used to abort every
    # ordered read and the whole index rebuild with OverflowError.
    s = store
    write_lines(s.root, "w0.jsonl", chain("d1", 2))
    huge = hand_line("r1", "d2", 0, GENESIS)
    huge["seq"] = seq
    huge["hash"] = event_hash(huge)
    write_lines(s.root, "w0.jsonl", [huge] + chain("d3", 1))
    at = len(b"".join(canonical_json(e.model_dump(mode="json")) + b"\n" for e in chain("d1", 2)))

    with pytest.raises(ValueError) as caught:  # pydantic ValidationError
        list(s.log.iter_events("r1"))
    assert not isinstance(caught.value, UNEXPECTED)
    assert [e.seq for e in s.log.iter_events("r1", "d1")] == [0, 1]  # other documents still read

    scan = s.log.scan("r1", strict=False)
    assert [(e.event.doc_id, e.event.seq) for e in scan.events()] == [("d1", 0), ("d1", 1), ("d3", 0)]
    assert [(b.segment, b.byte_offset, b.kind) for b in scan.bad] == [("w0.jsonl", at, "invalid_event")]

    stats = _index(s).reindex(s.log)
    assert [(p.kind, p.segment, p.byte_offset) for p in stats.problems] == [("invalid_event", "w0.jsonl", at)]
    assert {d.doc_id for d in _index(s).documents("r1")} == {"d1", "d3"}

    with pytest.raises(CorruptLine, match=f"w0.jsonl at byte {at}"):
        seal_run(s.log, "r1")
    report = verify(s)
    assert ("invalid_event", "d2") in [(p.kind, p.doc_id) for p in report.problems]


@pytest.mark.parametrize("field", ["tokens_in", "tokens_out", "tokens_cached", "attempt"])
def test_counts_are_bounded_at_append_so_nothing_unexportable_is_stored(store: SimpleNamespace, field: str) -> None:
    s = store
    with s.log.open_writer("r1", "w0") as w:
        w.append(doc_id="d1", node="n", kind="model_call", **{field: 2**63 - 1})  # the largest int64
        with pytest.raises(ValueError):
            w.append(doc_id="d1", node="n", kind="model_call", **{field: 2**63})
    assert [getattr(e, field) for e in s.log.iter_events("r1")] == [2**63 - 1]
    seal = seal_run(s.log, "r1")  # the Parquet export holds the boundary value
    assert seal.event_count == 1
    _index(s).reindex(s.log)
    assert [d.events for d in _index(s).documents("r1")] == [1]


# --- stored keys never reach an error message (Task 9 review) ---------------------------

LEAKY_KEY = f"{SENTINEL} 4111-1111-1111-1111"


def _assert_no_sentinel(*texts: str) -> None:
    for text in texts:
        assert SENTINEL not in text and "4111" not in text, text


def test_an_unknown_key_in_an_event_is_named_without_its_text(store: SimpleNamespace) -> None:
    s = store
    raw = hand_line("r1", "d1", 0, GENESIS)
    raw[LEAKY_KEY] = 1  # an extra top-level key: pydantic's loc would be the key itself
    raw["payload"] = {LEAKY_KEY: "x"}  # fine: payload keys are not validated
    raw["decision"] = {LEAKY_KEY: LEAKY_KEY}
    raw["hash"] = event_hash(raw)
    write_lines(s.root, "w0.jsonl", [raw])

    with pytest.raises(CorruptLine) as sealed:
        seal_run(s.log, "r1")
    assert "<key>: extra_forbidden" in str(sealed.value)
    scan = s.log.scan("r1", strict=False)
    list(scan.events())
    stats = _index(s).reindex(s.log)
    report = verify(s)
    _assert_no_sentinel(
        str(sealed.value),
        *(b.detail for b in scan.bad),
        *(p.detail for p in stats.problems),
        *(p.detail for p in report.problems),
    )
    assert [b.detail for b in scan.bad] == ["invalid event: <key>: extra_forbidden"]
    assert ("invalid_event", "d1") in [(p.kind, p.doc_id) for p in report.problems]


def test_a_seal_file_with_stored_keys_and_values_is_named_without_them(store_with_events: SimpleNamespace) -> None:
    s = store_with_events
    seal_run(s.log, "r1", sealed_at="2026-11-18T13:00:00Z")
    path = s.root / "runs/r1/seals/0.json"
    raw = json.loads(path.read_bytes())
    raw[LEAKY_KEY] = LEAKY_KEY
    raw["doc_heads"] = {LEAKY_KEY: LEAKY_KEY}
    path.write_bytes(canonical_json(raw))
    with pytest.raises(SealCorrupt) as listed:
        list_seals(s.log, "r1")
    with pytest.raises(SealCorrupt) as sealed:
        seal_run(s.log, "r1")
    stats = _index(s).reindex(s.log)
    report = verify(s)
    _assert_no_sentinel(
        str(listed.value), str(sealed.value), *(p.detail for p in stats.problems), *(p.detail for p in report.problems)
    )
    assert "seal_corrupt" in {p.kind for p in report.problems}


def test_a_decision_nested_too_deep_is_an_invalid_event_for_every_reader(store_with_events: SimpleNamespace) -> None:
    # Task 9 finding: a hash-correct line whose decision nests ~600 deep made the upgrade's
    # copy raise RecursionError out of the tolerant scan, aborting reindex and ensure.
    s = store_with_events
    nested: object = []
    for _ in range(DEEP_BUT_PARSABLE):
        nested = [nested]
    raw = hand_line("r1", "d9", 0, GENESIS)
    raw["decision"] = [nested]
    raw["hash"] = event_hash(raw)
    write_lines(s.root, "w9.jsonl", [raw])
    stats = _index(s).reindex(s.log)  # never aborts
    assert [(p.kind, p.segment) for p in stats.problems] == [("invalid_event", "w9.jsonl")]
    assert _index(s).ensure(s.log) is None
    with pytest.raises(ValueError) as strict:
        list(s.log.iter_events("r1"))
    assert not isinstance(strict.value, UNEXPECTED)
    assert ("invalid_event", "d9", 0) in [(p.kind, p.doc_id, p.seq) for p in verify(s).problems]


# --- strict readers name fields, never stored values -----------------------------------

_LEAKY_FIELDS = {
    "kind": {"kind": SENTINEL},
    "ts": {"ts": SENTINEL},
    "tokens_in": {"tokens_in": SENTINEL},
    "cause_list": {"cause": [SENTINEL]},
    "inputs_key": {"inputs": [{"sha256": "b" * 64, "role": "original", "size": 1, LEAKY_KEY: SENTINEL}]},
    "decision_nan": {"decision": {LEAKY_KEY: float("nan")}},
    "extra_key": {LEAKY_KEY: SENTINEL},
}


def _leaky_line(store: SimpleNamespace, segment: str, fields: dict) -> int:
    """Append a line for D2 seq 0 carrying ``fields`` (NaN allowed); returns its offset."""
    raw = hand_line("r1", "D2", 0, GENESIS) | fields
    path = store.root / "runs/r1/segments" / segment
    at = path.stat().st_size if path.exists() else 0
    with path.open("ab") as f:
        f.write(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    return at


def _leak_free(exc: BaseException) -> str:
    text = "".join(traceback.format_exception(exc))
    _assert_no_sentinel(str(exc), repr(exc), text)
    return str(exc)


@pytest.mark.parametrize("case", sorted(_LEAKY_FIELDS))
def test_every_strict_read_names_an_invalid_event_without_its_stored_values(
    store_with_events: SimpleNamespace, case: str
) -> None:
    s = store_with_events
    idx = _index(s)
    idx.reindex(s.log)  # built before the damage: the read of D2 checks unindexed bytes
    at = _leaky_line(s, "w9.jsonl", _LEAKY_FIELDS[case])
    reads = {
        "iter_events": lambda: list(s.log.iter_events("r1")),
        "iter_events(D2)": lambda: list(s.log.iter_events("r1", "D2")),
        "scan().events()": lambda: list(s.log.scan("r1").events()),
        "scan(D2).events()": lambda: list(s.log.scan("r1", "D2").events()),
        "Index.iter_events (stale)": lambda: list(idx.iter_events(s.log, "r1", "D2")),
        "Index.iter_events (rebuilt)": lambda: (idx.reindex(s.log), list(idx.iter_events(s.log, "r1", "D2"))),
    }
    for name, read in reads.items():
        with pytest.raises(InvalidEvent) as caught:
            read()
        assert isinstance(caught.value, ValueError), name  # callers catching ValueError still do
        msg = _leak_free(caught.value)
        assert msg.startswith(f"w9.jsonl at byte {at}: invalid event: "), (name, msg)


def test_a_run_header_is_named_without_its_stored_values(store: SimpleNamespace) -> None:
    path = store.root / "runs/r1/run.json"
    raw = json.loads(path.read_bytes())
    raw["kind"] = SENTINEL
    raw[LEAKY_KEY] = SENTINEL
    path.write_bytes(canonical_json(raw))
    with pytest.raises(ValueError) as caught:
        store.log.read_run("r1")
    msg = _leak_free(caught.value)
    assert "<key>: extra_forbidden" in msg and "kind:" in msg


@pytest.mark.parametrize("seq", [-1, -(2**63) - 1, 2**63, 2**64 + 5])
def test_a_seq_out_of_range_is_one_invalid_event_everywhere(store: SimpleNamespace, seq: int) -> None:
    # Negative or past int64: the line is d2's but has no place in its chain. verify and
    # the index report the same single kind, and no seq_gap is invented from it.
    s = store
    write_lines(s.root, "w0.jsonl", chain("d2", 1))
    bad = hand_line("r1", "d2", 1, GENESIS)
    bad["seq"] = seq
    bad["hash"] = event_hash(bad)
    write_lines(s.root, "w0.jsonl", [bad] + chain("d3", 1))
    at = len(canonical_json(chain("d2", 1)[0].model_dump(mode="json")) + b"\n")

    report = verify(s)
    lines = [p for p in report.problems if p.kind != "missing_blob"]  # chain() names blobs it never stores
    assert [(p.kind, p.doc_id, p.seq, p.byte_offset) for p in lines] == [("invalid_event", "d2", None, at)]
    assert "seq: not an integer in 0..2**63-1" in lines[0].detail
    stats = _index(s).reindex(s.log)
    assert [(p.kind, p.byte_offset) for p in stats.problems] == [("invalid_event", at)]
