from __future__ import annotations

import errno
import math
import multiprocessing
import os
import stat
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mailroom_common import jsonl
from mailroom_common.errors import IntegrityError
from mailroom_common.jsonl import BadLine, append_jsonl, iter_jsonl, parse_json, write_jsonl_atomic

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4),
    max_leaves=20,
)


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


# --- append_jsonl ------------------------------------------------------------------------


def test_append_writes_one_canonical_line_per_call(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    append_jsonl(path, {"b": 1, "a": "é"})
    append_jsonl(str(path), [1, None])
    assert path.read_bytes() == '{"a":"é","b":1}\n[1,null]\n'.encode()


def test_append_keeps_unicode_line_separators_inside_one_line(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    row = {"text": "a b c\x85d\nend"}
    append_jsonl(path, row)
    assert path.read_bytes().count(b"\n") == 1
    assert list(iter_jsonl(path)) == [row]


@pytest.mark.parametrize(("row", "error"), [({"x": math.nan}, ValueError), ({1: "a"}, TypeError)])
def test_append_refuses_unencodable_rows_and_writes_nothing(
    tmp_path: Path, row: Any, error: type[Exception]
) -> None:
    path = tmp_path / "log.jsonl"
    append_jsonl(path, {"ok": 1})
    with pytest.raises(error):
        append_jsonl(path, row)
    assert path.read_bytes() == b'{"ok":1}\n'


def test_append_refuses_a_line_break_in_the_encoding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jsonl, "canonical_json", lambda obj: b'{"a":\n1}')
    path = tmp_path / "log.jsonl"
    with pytest.raises(IntegrityError, match="contains a line break"):
        append_jsonl(path, {"a": 1})
    assert not path.exists()


def test_append_uses_a_single_write_and_fsyncs_only_on_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[int] = []
    fsyncs: list[int] = []
    real_write, real_fsync = os.write, os.fsync

    def spy_write(fd: int, data: bytes) -> int:
        writes.append(len(data))
        return real_write(fd, data)

    def spy_fsync(fd: int) -> None:
        fsyncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "write", spy_write)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    path = tmp_path / "log.jsonl"
    append_jsonl(path, {"payload": "x" * 100_000})
    assert writes == [path.stat().st_size]
    assert fsyncs == []
    append_jsonl(path, {"a": 1}, fsync=True)
    assert len(writes) == 2
    assert len(fsyncs) == 1


def test_short_write_is_continued_until_the_line_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "log.jsonl"
    append_jsonl(path, {"seq": 1})
    calls: list[int] = []
    real_write = os.write

    def dribble(fd: int, data: bytes) -> int:
        calls.append(len(data))
        return real_write(fd, bytes(data[:4]))

    monkeypatch.setattr(os, "write", dribble)
    append_jsonl(path, {"seq": 2})
    monkeypatch.undo()
    assert calls == [10, 6, 2]
    assert list(iter_jsonl(path)) == [{"seq": 1}, {"seq": 2}]


def test_write_that_makes_no_progress_raises_and_leaves_a_torn_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "log.jsonl"
    append_jsonl(path, {"seq": 1})
    real_write = os.write
    calls: list[int] = []

    def stall(fd: int, data: bytes) -> int:
        calls.append(len(data))
        return real_write(fd, bytes(data[:5])) if len(calls) == 1 else 0

    monkeypatch.setattr(os, "write", stall)
    with pytest.raises(OSError, match="no progress") as info:
        append_jsonl(path, {"seq": 2})
    assert info.value.filename == str(path)
    monkeypatch.undo()
    rows = list(iter_jsonl(path, tolerant=True))
    assert rows == [{"seq": 1}, BadLine(line_no=2, raw=b'{"seq', error=rows[1].error, truncated_tail=True)]
    with pytest.raises(IntegrityError, match="torn line"):
        append_jsonl(path, {"seq": 3})


def test_append_after_a_torn_tail_is_refused_and_leaves_the_file_unchanged(tmp_path: Path) -> None:
    path = _write(tmp_path / "log.jsonl", b'{"n":1}\n{"n":2,"x":"partial')
    before = path.read_bytes()
    with pytest.raises(IntegrityError, match="ends in a torn line and must be repaired") as info:
        append_jsonl(path, {"n": 3})
    assert info.value.context == {"path": str(path)}
    assert path.read_bytes() == before
    rows = list(iter_jsonl(path, tolerant=True))
    assert rows[0] == {"n": 1}
    assert isinstance(rows[1], BadLine)
    assert rows[1].truncated_tail


def test_append_to_an_empty_file_and_after_a_complete_line_is_allowed(tmp_path: Path) -> None:
    path = _write(tmp_path / "log.jsonl", b"")
    append_jsonl(path, {"n": 1})
    append_jsonl(path, {"n": 2})
    assert path.read_bytes() == b'{"n":1}\n{"n":2}\n'


def test_disk_full_propagates_and_closes_the_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    real_close = os.close

    def full(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    def spy_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "write", full)
    monkeypatch.setattr(os, "close", spy_close)
    with pytest.raises(OSError) as info:
        append_jsonl(tmp_path / "log.jsonl", {"a": 1})
    assert info.value.errno == errno.ENOSPC
    assert len(closed) == 1


def test_fsync_error_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(fd: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "fsync", broken)
    with pytest.raises(OSError) as info:
        append_jsonl(tmp_path / "log.jsonl", {"a": 1}, fsync=True)
    assert info.value.errno == errno.EIO


def _assert_all_rows_intact(path: Path, writers: int, per_writer: int, payload_size: int) -> None:
    raw_lines = path.read_bytes().split(b"\n")
    assert raw_lines[-1] == b""
    assert len(raw_lines) - 1 == writers * per_writer
    seen = set()
    for row in iter_jsonl(path):  # strict: any torn or interleaved line raises
        assert row["payload"] == row["writer"][0] * payload_size
        seen.add((row["writer"], row["seq"]))
    assert seen == {(f"{w}", s) for w in "abcdefgh"[:writers] for s in range(per_writer)}


def test_concurrent_appends_from_threads_never_interleave(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    writers, per_writer, payload_size = 8, 150, 16_384
    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def work(name: str) -> None:
        try:
            barrier.wait()
            for seq in range(per_writer):
                append_jsonl(path, {"writer": name, "seq": seq, "payload": name * payload_size})
        except BaseException as exc:  # noqa: BLE001  (re-raised in the main thread below)
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(name,)) for name in "abcdefgh"[:writers]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    _assert_all_rows_intact(path, writers, per_writer, payload_size)


def test_concurrent_appends_from_processes_never_interleave(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    writers, per_writer, payload_size = 4, 100, 65_536
    jobs = [
        (str(path), {"writer": name, "seq": seq, "payload": name * payload_size})
        for seq in range(per_writer)
        for name in "abcd"[:writers]
    ]
    with multiprocessing.get_context("spawn").Pool(writers) as pool:
        pool.starmap(append_jsonl, jobs, chunksize=1)
    _assert_all_rows_intact(path, writers, per_writer, payload_size)


# --- write_jsonl_atomic ------------------------------------------------------------------


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def test_atomic_write_creates_and_replaces(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    assert write_jsonl_atomic(path, [{"b": 2, "a": 1}, "x"]) == 2
    assert path.read_bytes() == b'{"a":1,"b":2}\n"x"\n'
    assert write_jsonl_atomic(path, iter([{"new": True}])) == 1
    assert path.read_bytes() == b'{"new":true}\n'
    assert write_jsonl_atomic(path, []) == 0
    assert path.read_bytes() == b""
    assert _leftovers(tmp_path) == []


def test_atomic_write_fsyncs_file_and_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[bool] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        synced.append(os.path.isdir(f"/proc/self/fd/{fd}") if os.path.exists("/proc/self/fd") else False)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    write_jsonl_atomic(tmp_path / "rows.jsonl", [{"a": 1}])
    assert len(synced) == 2
    if os.path.exists("/proc/self/fd"):
        assert synced == [False, True]


def _rows_then_bad_row() -> Iterator[Any]:
    yield {"fine": 1}
    yield {"fine": 2}
    yield {"bad": math.nan}


def _rows_then_crash() -> Iterator[Any]:
    yield {"fine": 1}
    raise RuntimeError("producer died")


@pytest.mark.parametrize(
    ("rows", "error", "match"),
    [
        (_rows_then_bad_row, ValueError, "Out of range float"),
        (_rows_then_crash, RuntimeError, "producer died"),
        (lambda: [{"ok": 1}, {2: "non-str key"}], TypeError, "keys must be str"),
    ],
)
def test_atomic_write_failure_leaves_old_file_intact(
    tmp_path: Path, rows: Any, error: type[Exception], match: str
) -> None:
    path = tmp_path / "rows.jsonl"
    write_jsonl_atomic(path, [{"old": 1}, {"old": 2}])
    before = path.read_bytes()
    with pytest.raises(error, match=match):
        write_jsonl_atomic(path, rows())
    assert path.read_bytes() == before
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("broken", ["fsync", "replace"])
def test_atomic_write_os_failure_leaves_old_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    path = tmp_path / "rows.jsonl"
    write_jsonl_atomic(path, [{"old": 1}])

    def fail(*args: Any) -> None:
        raise OSError(errno.EIO, f"{broken} failed")

    monkeypatch.setattr(os, broken, fail)
    with pytest.raises(OSError, match=f"{broken} failed"):
        write_jsonl_atomic(path, [{"new": 1}])
    monkeypatch.undo()
    assert path.read_bytes() == b'{"old":1}\n'
    assert _leftovers(tmp_path) == []


def test_atomic_write_keeps_the_mode_of_the_file_it_replaces(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    write_jsonl_atomic(path, [{"a": 1}])
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    path.chmod(0o640)
    write_jsonl_atomic(path, [{"a": 2}])
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    path.chmod(0o604)
    write_jsonl_atomic(path, [{"a": 3}])
    assert stat.S_IMODE(path.stat().st_mode) == 0o604


def test_atomic_write_through_a_symlink_replaces_the_target_and_keeps_the_link(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real = real_dir / "rows.jsonl"
    write_jsonl_atomic(real, [{"a": 1}])
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    assert write_jsonl_atomic(link, [{"a": 2}]) == 1
    assert link.is_symlink()
    assert os.readlink(link) == str(real)
    assert list(iter_jsonl(real)) == [{"a": 2}]
    assert _leftovers(tmp_path) == []
    assert _leftovers(real_dir) == []


def test_atomic_write_into_missing_directory_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write_jsonl_atomic(tmp_path / "nope" / "rows.jsonl", [{"a": 1}])


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=60)
@given(st.lists(json_values, max_size=8))
def test_round_trip_through_atomic_write_and_append(tmp_path: Path, rows: list[Any]) -> None:
    path = tmp_path / "round.jsonl"
    write_jsonl_atomic(path, rows)
    assert list(iter_jsonl(path)) == rows
    for row in rows:
        append_jsonl(path, row)
    assert list(iter_jsonl(path)) == rows + rows


# --- iter_jsonl --------------------------------------------------------------------------


def test_empty_file_yields_nothing(tmp_path: Path) -> None:
    assert list(iter_jsonl(_write(tmp_path / "e.jsonl", b""))) == []


def test_blank_lines_crlf_and_bom_are_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path / "f.jsonl", b'\xef\xbb\xbf{"a":1}\r\n\r\n   \n\t\n[2]\r\n{"c":3}')
    assert list(iter_jsonl(path)) == [{"a": 1}, [2], {"c": 3}]


def test_bom_only_counts_at_the_start_of_the_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "f.jsonl", b'{"a":1}\n\xef\xbb\xbf{"b":2}\n')
    with pytest.raises(IntegrityError, match="not valid JSON") as info:
        list(iter_jsonl(path))
    assert info.value.context == {"path": str(path), "line": 2}


def test_strict_mode_reports_path_and_physical_line_number(tmp_path: Path) -> None:
    path = _write(tmp_path / "f.jsonl", b'{"a":1}\n\n\n{"a":\n{"b":2}\n')
    with pytest.raises(IntegrityError, match="bad JSONL line: not valid JSON") as info:
        list(iter_jsonl(path))
    assert info.value.context == {"path": str(path), "line": 4}
    assert f"path='{path}'" in str(info.value) and "line=4" in str(info.value)


def test_strict_mode_is_lazy(tmp_path: Path) -> None:
    path = _write(tmp_path / "f.jsonl", b'{"a":1}\nnot json\n')
    rows = iter_jsonl(path)
    assert next(rows) == {"a": 1}
    with pytest.raises(IntegrityError):
        next(rows)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (b'{"a":1,"a":2}', "duplicate object key 'a'"),
        (b'{"a":NaN}', "NaN is not valid JSON"),
        (b"[Infinity]", "Infinity is not valid JSON"),
        (b'{"a":1} trailing', "Extra data"),
        (b"{'a': 1}", "Expecting property name"),
    ],
)
def test_strict_json_refusals(tmp_path: Path, line: bytes, message: str) -> None:
    path = _write(tmp_path / "f.jsonl", line + b"\n")
    with pytest.raises(IntegrityError, match=message):
        list(iter_jsonl(path))
    [bad] = list(iter_jsonl(path, tolerant=True))
    assert isinstance(bad, BadLine) and message in bad.error and bad.raw == line
    assert bad.truncated_tail is False


def test_non_utf8_is_reported_not_fatal_in_tolerant_mode(tmp_path: Path) -> None:
    path = _write(tmp_path / "f.jsonl", b'{"a":1}\n"ab\xff\xfe"\n{"c":3}\n')
    rows = list(iter_jsonl(path, tolerant=True))
    assert rows == [
        {"a": 1},
        BadLine(line_no=2, raw=b'"ab\xff\xfe"', error="not valid UTF-8 at byte 3"),
        {"c": 3},
    ]
    with pytest.raises(IntegrityError, match="not valid UTF-8 at byte 3") as info:
        list(iter_jsonl(path))
    assert info.value.context["line"] == 2


def test_truncated_tail_is_flagged_in_tolerant_mode_and_raised_in_strict(tmp_path: Path) -> None:
    path = _write(tmp_path / "f.jsonl", b'{"a":1}\n{"b":2}\n{"c":')
    rows = list(iter_jsonl(path, tolerant=True))
    assert rows[:2] == [{"a": 1}, {"b": 2}]
    tail = rows[2]
    assert isinstance(tail, BadLine)
    assert (tail.line_no, tail.raw, tail.truncated_tail) == (3, b'{"c":', True)
    with pytest.raises(IntegrityError) as info:
        list(iter_jsonl(path))
    assert info.value.context == {"path": str(path), "line": 3, "truncated_tail": True}


def test_bad_line_with_newline_is_not_a_truncated_tail(tmp_path: Path) -> None:
    path = _write(tmp_path / "f.jsonl", b'{"c":\n')
    [bad] = list(iter_jsonl(path, tolerant=True))
    assert isinstance(bad, BadLine) and bad.truncated_tail is False


def test_parseable_last_line_without_newline_is_a_row(tmp_path: Path) -> None:
    assert list(iter_jsonl(_write(tmp_path / "f.jsonl", b'{"a":1}\n{"b":2}'))) == [{"a": 1}, {"b": 2}]


def test_oversized_line_is_skipped_without_losing_the_lines_after_it(tmp_path: Path) -> None:
    big = b'{"x":"' + b"y" * 5000 + b'"}'
    path = _write(tmp_path / "f.jsonl", b'{"a":1}\n' + big + b'\n{"b":2}\n')
    rows = list(iter_jsonl(path, tolerant=True, max_line_bytes=1000))
    assert rows[0] == {"a": 1} and rows[2] == {"b": 2}
    bad = rows[1]
    assert isinstance(bad, BadLine)
    assert bad.line_no == 2 and bad.error == "line is longer than max_line_bytes=1000"
    assert bad.raw == big[:1000] and not bad.truncated_tail
    with pytest.raises(IntegrityError, match="longer than max_line_bytes=1000") as info:
        list(iter_jsonl(path, max_line_bytes=1000))
    assert info.value.context["line"] == 2


def test_oversized_last_line_without_newline(tmp_path: Path) -> None:
    path = _write(tmp_path / "f.jsonl", b'{"a":1}\n' + b"z" * 3_000_000)
    rows = list(iter_jsonl(path, tolerant=True, max_line_bytes=100))
    assert rows[0] == {"a": 1}
    assert len(rows) == 2
    tail = rows[1]
    assert isinstance(tail, BadLine)
    assert tail.raw == b"z" * 100 and tail.line_no == 2
    assert tail.error == "line is longer than max_line_bytes=100"


def test_line_exactly_at_the_limit_is_accepted(tmp_path: Path) -> None:
    line = b'"' + b"a" * 98 + b'"'
    assert len(line) == 100
    path = _write(tmp_path / "f.jsonl", line + b"\n" + line)
    assert list(iter_jsonl(path, max_line_bytes=100)) == ["a" * 98, "a" * 98]
    with pytest.raises(IntegrityError, match="longer than"):
        list(iter_jsonl(path, max_line_bytes=99))


def test_reader_memory_is_bounded_by_max_line_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path / "f.jsonl", b"q" * 5_000_000 + b"\n[1]\n")
    biggest: list[int] = []
    real_read_line = jsonl._read_line

    def spy(handle: Any, max_line_bytes: int) -> Any:
        real_readline = handle.readline

        class Handle:
            def readline(self, size: int = -1) -> bytes:
                assert size > 0, "unbounded readline"
                chunk: bytes = real_readline(size)
                biggest.append(len(chunk))
                return chunk

        return real_read_line(Handle(), max_line_bytes)  # type: ignore[arg-type]

    monkeypatch.setattr(jsonl, "_read_line", spy)
    rows = list(iter_jsonl(path, tolerant=True, max_line_bytes=4096))
    assert rows[-1] == [1]
    assert max(biggest) <= 1 << 20


def test_open_errors_propagate(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(iter_jsonl(tmp_path / "missing.jsonl"))
    with pytest.raises(IsADirectoryError):
        list(iter_jsonl(tmp_path))


def test_max_line_bytes_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 1, got 0"):
        list(iter_jsonl(_write(tmp_path / "f.jsonl", b"[1]\n"), max_line_bytes=0))


def test_parse_json_accepts_plain_json() -> None:
    assert parse_json(b'{"a":[1,2.5,null,true]}') == {"a": [1, 2.5, None, True]}
    with pytest.raises(ValueError, match="duplicate object key 'k'"):
        parse_json('{"o":{"k":1,"k":1}}')


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_a_line_exactly_at_the_limit_is_accepted_with_lf_or_crlf(tmp_path: Path, ending: bytes) -> None:
    body = b'{"k":"' + b"x" * 10 + b'"}'
    path = tmp_path / "limit.jsonl"
    path.write_bytes(body + ending + b'{"n":2}\n')
    rows = list(iter_jsonl(path, max_line_bytes=len(body)))
    assert rows == [{"k": "x" * 10}, {"n": 2}]


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_a_line_one_byte_over_the_limit_is_reported_and_the_next_line_still_reads(
    tmp_path: Path, ending: bytes
) -> None:
    body = b'{"k":"' + b"x" * 10 + b'"}'
    path = tmp_path / "over.jsonl"
    path.write_bytes(body + b" " + ending + b'{"n":2}\n')
    rows = list(iter_jsonl(path, tolerant=True, max_line_bytes=len(body)))
    assert isinstance(rows[0], BadLine) and "longer than max_line_bytes" in rows[0].error
    assert rows[1] == {"n": 2}
