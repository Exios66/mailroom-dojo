"""Task 9: the ``mailroom-record`` CLI: exit codes, JSON output, and that it never leaks content."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from record_testkit import (
    SENTINEL,
    append_torn_tail,
    build_review_store,
    build_sealed_store,
    forge_event,
    hand_line,
    insert_line_after,
    rewrite_line,
    run_cli,
    run_fields,
)

from mailroom_record import cli
from mailroom_record.blobs import BlobStore
from mailroom_record.log import EventLog
from mailroom_record.models import BLOB_ROLES, GENESIS, Run

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.fixture
def review_store(tmp_path: Path) -> SimpleNamespace:
    return build_review_store(tmp_path / "store")


@pytest.fixture
def sealed_store(tmp_path: Path) -> SimpleNamespace:
    return build_sealed_store(tmp_path / "store")


def _clean(res) -> None:
    """No traceback and no stored content in any output."""
    out = res.stdout + res.stderr
    assert "Traceback" not in out and SENTINEL not in out, out


# --- the plan's sketches ---------------------------------------------------------------


def test_cli_verify_exit_codes(sealed_store: SimpleNamespace) -> None:
    assert run_cli("verify", "r1", root=sealed_store.root).exit_code == 0
    rewrite_line(sealed_store, doc_id="d1", seq=0, cause="x")
    assert run_cli("verify", "r1", root=sealed_store.root).exit_code == 1


def test_cli_never_prints_blob_content(review_store: SimpleNamespace) -> None:
    out = run_cli("trace", "doc", "d1", root=review_store.root).stdout
    assert "SECRET-DOCUMENT-TEXT" not in out  # the fixture's original blob
    assert review_store.shas["original"] in out  # positive control: the blob is named, by hash


# --- every command, text and JSON --------------------------------------------------------


def test_verify_lists_each_problem_and_its_json_parses(sealed_store: SimpleNamespace) -> None:
    sealed_store.blobs.content_path(sealed_store.outputs[0][2]).unlink()
    append_torn_tail(sealed_store)
    res = run_cli("verify", "r1", root=sealed_store.root)
    assert res.exit_code == 1 and res.stdout.startswith("run r1: NOT OK (1 failing problem)")
    assert "missing_blob doc d1" in res.stdout and "torn_tail (does not fail)" in res.stdout
    res = run_cli("verify", "r1", "--json", root=sealed_store.root)
    report = json.loads(res.stdout)
    assert res.exit_code == 1 and report["ok"] is False and report["run_id"] == "r1"
    assert sorted(p["kind"] for p in report["problems"]) == ["missing_blob", "torn_tail"]
    _clean(res)


def test_seal_command_seals_once_then_reports_nothing_new(review_store: SimpleNamespace) -> None:
    root = review_store.root
    res = run_cli("seal", "r1", root=root)
    assert res.exit_code == 0 and res.stdout.startswith("sealed run r1: seal 0, 11 events in total, 3 documents")
    res = run_cli("seal", "r1", "--json", root=root)
    body = json.loads(res.stdout)
    assert res.exit_code == 0 and body["new"] is False and body["seal"]["n"] == 0
    with review_store.log.open_writer("r1", "w1") as w:
        w.append(doc_id="d3", node="classify", kind="decision")
    res = run_cli("seal", "r1", root=root)
    assert res.exit_code == 0 and "seal 1, 12 events" in res.stdout
    assert run_cli("verify", "r1", root=root).exit_code == 0
    _clean(res)


def test_seal_refuses_a_damaged_log_with_one_line_and_exit_1(review_store: SimpleNamespace) -> None:
    rewrite_line(review_store, doc_id="d1", seq=2, cause=f"{SENTINEL} tampered")  # stale hash
    res = run_cli("seal", "r1", root=review_store.root)
    assert res.exit_code == 1 and res.stdout == ""
    assert res.stderr.startswith("error: SealRefused:") and res.stderr.count("\n") == 1
    _clean(res)
    assert list((review_store.log.run_dir("r1") / "seals").glob("*.json")) == []  # nothing sealed


def test_seal_refuses_a_forked_chain(review_store: SimpleNamespace) -> None:
    prev = next(e for e in review_store.log.iter_events("r1", "d2") if e.seq == 0).hash
    forge_event(review_store, "d2", 1, prev)
    res = run_cli("seal", "r1", root=review_store.root)
    assert res.exit_code == 1 and res.stderr.count("\n") == 1
    _clean(res)


def test_reindex_reports_counts_and_problems(review_store: SimpleNamespace) -> None:
    insert_line_after(review_store, "d2", 0, f"{SENTINEL} not json".encode())
    res = run_cli("reindex", root=review_store.root)
    assert res.exit_code == 0
    assert "1 runs, 3 documents, 11 events, 0 seals, 0 torn tails, 1 problems" in res.stdout
    assert "corrupt_line run r1 segment w0.jsonl byte" in res.stdout
    stats = json.loads(run_cli("reindex", "--json", root=review_store.root).stdout)
    assert (stats["events"], [p["kind"] for p in stats["problems"]]) == (11, ["corrupt_line"])
    assert (review_store.root / cli.INDEX_FILE).is_file()
    _clean(res)


def test_trace_and_versions_json_is_complete(review_store: SimpleNamespace) -> None:
    root = review_store.root
    run = json.loads(run_cli("trace", "run", "r1", "--json", root=root).stdout)
    assert [d["doc_id"] for d in run["documents"]] == ["d1", "d2", "d3"] and run["damaged"] is False
    assert run["documents"][0]["attempts"]["extract"] == 2 and run["documents"][1]["in_review"] is True
    assert run["documents"][0]["last_decision"]["digest"]["keys"] == ["state"]
    doc = json.loads(run_cli("trace", "doc", "d1", "--json", root=root).stdout)
    assert [r["run_id"] for r in doc["runs"]] == ["r1"] and len(doc["runs"][0]["steps"]) == 7
    assert [v["role"] for v in doc["runs"][0]["versions"]][-1] == "human_correction"
    ver = json.loads(run_cli("versions", "r1", "d1", "--json", root=root).stdout)
    assert [v["order"] for v in ver["versions"]] == list(range(6)) and ver["damage"] == [] and ver["events"] == 7
    assert {v["state"] for v in ver["versions"]} == {"present"}


def test_trace_run_text_names_documents_nodes_and_review(review_store: SimpleNamespace) -> None:
    res = run_cli("trace", "run", "r1", root=review_store.root)
    assert res.exit_code == 0
    assert "nodes: intake > transcribe > extract > archive > human_review" in res.stdout
    assert "attempts: extract 2" in res.stdout and "IN REVIEW" in res.stdout
    assert "last cause: seq 2 at extract: text of" in res.stdout
    _clean(res)


# --- usage errors: exit 2, one line, never a traceback --------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("verify", "../r1"),
        ("verify", "a/b"),
        ("seal", ".."),
        ("trace", "run", "r 1"),
        ("versions", "-", "d1"),
        ("verify", "r9"),
        ("seal", "r9"),
        ("trace", "run", "r9"),
        ("versions", "r9", "d1"),
        ("trace", "doc", "nope"),
        ("versions", "r1", "nope"),
    ],
)
def test_bad_or_unknown_ids_exit_2(review_store: SimpleNamespace, args: tuple[str, ...]) -> None:
    res = run_cli(*args, root=review_store.root)
    assert res.exit_code == 2, (res.stdout, res.stderr)
    assert res.stdout == "" and res.stderr.startswith("usage error: ") and res.stderr.count("\n") == 1
    _clean(res)


@pytest.mark.parametrize("which", ["missing", "file"])
def test_a_root_that_is_not_a_directory_exits_2(tmp_path: Path, which: str) -> None:
    root = tmp_path / "nowhere"
    if which == "file":
        root.write_text("x")
    for args in (("verify", "r1"), ("seal", "r1"), ("reindex",), ("trace", "run", "r1"), ("trace", "doc", "d1"), ("versions", "r1", "d1")):
        res = run_cli(*args, root=root)
        assert res.exit_code == 2 and res.stderr == f"usage error: no store at {root}: not a directory\n", args
    assert not root.is_dir()  # nothing was created


@pytest.mark.parametrize("args", [("verify", "r1"), ("trace", "run"), ("bogus",), ("versions", "r1"), ("verify", "r1", "--nope")])
def test_click_usage_errors_exit_2(review_store: SimpleNamespace, args: tuple[str, ...]) -> None:
    res = run_cli(*args, root=None if args == ("verify", "r1") else review_store.root)  # first: no --root
    assert res.exit_code == 2
    _clean(res)


def test_an_unexpected_error_is_one_line_exit_1_without_content(review_store: SimpleNamespace, monkeypatch) -> None:
    def boom(*a, **k):
        raise ValueError(f"{SENTINEL} inside an exception message")

    monkeypatch.setattr(cli, "trace_run", boom)
    res = run_cli("trace", "run", "r1", root=review_store.root)
    assert (res.exit_code, res.stdout, res.stderr) == (1, "", "error: ValueError\n")


def test_trace_falls_back_to_the_log_when_the_index_cannot_be_built(review_store: SimpleNamespace) -> None:
    (review_store.root / cli.INDEX_FILE).mkdir()  # an index path that can never be a file
    res = run_cli("trace", "doc", "d1", root=review_store.root)
    assert res.exit_code == 0 and "index unavailable" in res.stdout and "(read from the log)" in res.stdout
    _clean(res)


# --- the leak property ---------------------------------------------------------------------

_op = st.tuples(
    st.integers(0, 2),  # document
    st.sampled_from(["intake", "extract", "judge", "human_review"]),
    st.sampled_from(["handoff", "decision", "output", "review", "error"]),
    st.integers(0, 2),  # attempt
    st.none() | st.sampled_from(BLOB_ROLES),  # output blob role
    st.sampled_from(["none", "object", "list", "strings"]),  # decision shape
    st.booleans(),  # cause
    st.booleans(),  # name the previous blob as an input
)
_damage = st.lists(
    st.sampled_from(["tombstone", "remove", "garbage", "invalid", "invalid_key", "torn", "seal", "fork"]), max_size=3
)


def _write_history(root: Path, ops: list, damage: list[str]) -> SimpleNamespace:
    blobs = BlobStore(root)
    log = EventLog(root, blobs)
    log.create_run(Run(**run_fields(document_ids=["d0", "d1", "d2"])))
    store = SimpleNamespace(root=root, blobs=blobs, log=log, run_id="r1")
    last_blob: dict[str, object] = {}
    shas: list[str] = []
    with log.open_writer("r1", "w0") as w:
        for i, (doc, node, kind, attempt, role, shape, with_cause, chain_input) in enumerate(ops):
            doc_id = f"d{doc}"
            outputs = []
            if role is not None:
                ref = blobs.put(f"{SENTINEL} blob {i}".encode(), role)
                outputs, shas = [ref], shas + [ref.sha256]
            inputs = [last_blob[doc_id]] if chain_input and doc_id in last_blob else []
            decision = {
                "none": None,
                "object": {"answer": f"{SENTINEL} {i}", "p": 0.5},
                "list": [{"question": "q", "answer": f"{SENTINEL} {i}", "options": [SENTINEL]}],
                "strings": [SENTINEL, f"{SENTINEL} {i}"],
            }[shape]
            w.append(
                doc_id=doc_id, node=node, kind=kind, attempt=attempt, inputs=inputs, outputs=outputs,
                decision=decision, cause=f"{SENTINEL} cause {i}" if with_cause else None,
                payload={"text": f"{SENTINEL} {i}", f"{SENTINEL}-key": i},
            )
            if outputs:
                last_blob[doc_id] = outputs[0]
    for j, d in enumerate(damage):
        seg = log.segment_paths("r1")[-1]
        if d == "tombstone" and shas and blobs.exists(shas[j % len(shas)]) != "missing":
            blobs.tombstone(shas[j % len(shas)], f"{SENTINEL} reason")
        elif d == "remove" and shas and blobs.exists(shas[-1 - j % len(shas)]) == "present":
            blobs.content_path(shas[-1 - j % len(shas)]).unlink()
        elif d == "garbage":
            with seg.open("ab") as f:
                f.write(f'{{"doc_id": "d0", "seq": 1, "cause": "{SENTINEL}\n'.encode())
        elif d == "invalid":
            bad = {"doc_id": "d1", "seq": 0, "kind": SENTINEL, "cause": SENTINEL, "payload": {"t": SENTINEL}}
            with seg.open("ab") as f:
                f.write(json.dumps(bad).encode() + b"\n")
        elif d == "invalid_key":
            # a whole, otherwise valid event with the sentinel as a top-level KEY: a validator
            # that names the offending field must not carry the stored key name out
            bad = hand_line("r1", "d2", 0, GENESIS) | {f"{SENTINEL}-key": 1}
            with seg.open("ab") as f:
                f.write(json.dumps(bad).encode() + b"\n")
        elif d == "torn":
            append_torn_tail(store, f'{{"cause": "{SENTINEL}'.encode())
            break  # a torn tail is the end of the segment
        elif d == "seal":
            run_cli("seal", "r1", root=root)
        elif d == "fork":
            heads = [e for e in log.scan("r1", "d0", strict=False).events()]
            if heads:
                e = heads[0].event
                forge_event(store, "d0", e.seq, e.prev_hash, decision={"answer": SENTINEL}, cause=SENTINEL)
    return store


@given(ops=st.lists(_op, min_size=1, max_size=10), damage=_damage)
def test_cli_output_never_holds_stored_content(ops: list, damage: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_history(root, ops, damage)
        stored = b"".join(p.read_bytes() for p in (root / "runs" / "r1" / "segments").iterdir())
        assert SENTINEL.encode() in stored  # positive control: the needle is in the record
        docs = sorted({f"d{op[0]}" for op in ops})
        commands = [("verify", "r1"), ("trace", "run", "r1"), ("reindex",), ("seal", "r1")]
        commands += [("trace", "doc", d) for d in docs] + [("versions", "r1", d) for d in docs]
        seen_doc = False
        for args in commands:
            for extra in ((), ("--json",)):
                res = run_cli(*args, *extra, root=root)
                assert res.exit_code in (0, 1, 2), (args, res.exit_code)
                out = res.stdout + res.stderr
                assert SENTINEL not in out and "SECRET" not in out, (args, out)
                assert "Traceback" not in out
                seen_doc = seen_doc or docs[0] in res.stdout
        assert seen_doc  # positive control: the output does show the record


# --- the installed command ---------------------------------------------------------------


def test_pyproject_declares_the_console_script() -> None:
    meta = tomllib.loads((PACKAGE / "pyproject.toml").read_text())
    assert meta["project"]["scripts"] == {"mailroom-record": "mailroom_record.cli:app"}


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in [str(PACKAGE / "src"), env.get("PYTHONPATH", "")] if p)
    return env


def test_python_dash_m_runs_the_cli(review_store: SimpleNamespace) -> None:
    res = subprocess.run(
        [sys.executable, "-m", "mailroom_record", "trace", "doc", "d1", "--root", str(review_store.root)],
        capture_output=True, text=True, env=_env(), timeout=60,
    )
    assert res.returncode == 0 and "document d1: 1 run" in res.stdout and SENTINEL not in res.stdout + res.stderr


def test_installed_console_script(review_store: SimpleNamespace) -> None:
    beside = Path(sys.executable).parent / "mailroom-record"
    exe = str(beside) if beside.exists() else shutil.which("mailroom-record")
    if exe is None:
        pytest.skip("the mailroom-record console script is not installed: pip install -e packages/record")
    root = str(review_store.root)
    ok = subprocess.run([exe, "verify", "r1", "--root", root], capture_output=True, text=True, env=_env(), timeout=60)
    assert ok.returncode == 0 and ok.stdout.startswith("run r1: ok"), ok.stderr
    rewrite_line(review_store, doc_id="d1", seq=1, cause="x")
    bad = subprocess.run([exe, "verify", "r1", "--root", root], capture_output=True, text=True, env=_env(), timeout=60)
    assert bad.returncode == 1 and "bad_hash doc d1 seq 1" in bad.stdout
    usage = subprocess.run([exe, "trace", "run", "../x", "--root", root], capture_output=True, text=True, env=_env(), timeout=60)
    assert usage.returncode == 2 and usage.stderr.startswith("usage error: ") and "Traceback" not in usage.stderr
    for r in (ok, bad, usage):
        assert SENTINEL not in r.stdout + r.stderr


# --- hostile ids cannot forge output (review M-1) ------------------------------------------

import unicodedata  # noqa: E402

_HOSTILE = ["\n", "\r", "\x1b", "\x1b[31m", "\x1b]8;;x\x07", "\x00", "\x07", "\x85", "\x9b", "‮", "⁦",
            "​", " ", " ", "﻿", "\t", "run r1: ok"]
_hostile_text = st.lists(st.sampled_from(["a", "b"] + _HOSTILE), min_size=1, max_size=6).map("".join)
_UNSAFE = {"Cc", "Cf", "Zl", "Zp", "Cs"}


def _write_named(root: Path, docs: list[str], node: str, agent: str, keys: list[str]) -> None:
    blobs = BlobStore(root)
    log = EventLog(root, blobs)
    log.create_run(Run(**run_fields()))
    with log.open_writer("r1", "w0") as w:
        for i, doc in enumerate(docs):
            ref = blobs.put(f"blob {i}".encode(), "original")
            w.append(doc_id=doc, node=node, agent=agent, kind="decision", outputs=[ref],
                     decision={k: 1 for k in keys}, cause="c")
            w.append(doc_id=doc, node=node, agent=agent, kind="review", inputs=[ref], decision=[{k: 2} for k in keys])


def _unsafe_chars(text: str) -> list[str]:
    return [c for c in text if c != "\n" and unicodedata.category(c) in _UNSAFE]


@given(
    docs=st.lists(_hostile_text, min_size=2, max_size=2, unique=True),
    node=_hostile_text,
    agent=_hostile_text,
    keys=st.lists(_hostile_text, min_size=1, max_size=2, unique=True),
)
def test_hostile_ids_cannot_forge_text_output(docs: list[str], node: str, agent: str, keys: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        hostile, benign = Path(tmp) / "hostile", Path(tmp) / "benign"
        _write_named(hostile, docs, node, agent, keys)
        _write_named(benign, ["doc0", "doc1"], "node", "agent", [f"k{i}" for i in range(len(keys))])
        names = {"hostile": docs, "benign": ["doc0", "doc1"]}
        commands = [("verify", "r1"), ("trace", "run", "r1"), ("reindex",)]
        commands += [("trace", "doc", i) for i in range(2)] + [("versions", "r1", i) for i in range(2)]
        for args in commands:
            outs = {}
            for which, root in (("hostile", hostile), ("benign", benign)):
                argv = [names[which][a] if isinstance(a, int) else a for a in args]
                res = run_cli(*argv, root=root)
                assert res.exit_code == 0, (which, args, res.stderr)
                assert _unsafe_chars(res.stdout + res.stderr) == [], (args, res.stdout)
                outs[which] = res.stdout
            assert outs["hostile"].count("\n") == outs["benign"].count("\n"), (args, outs["hostile"])


def test_escaping_covers_every_unsafe_category() -> None:
    assert cli._printable("ok d1 caf\u00e9") == "ok d1 caf\u00e9"
    assert cli._printable("a\nb\rc\x1b[31m\x00\x85\u202e\u2028\u2029\ufeff\ud800\U000e0001") == (
        "a\\x0ab\\x0dc\\x1b[31m\\x00\\x85\\u202e\\u2028\\u2029\\ufeff\\ud800\\U000e0001"
    )


def test_a_lone_surrogate_id_is_escaped_not_a_crash(tmp_path: Path) -> None:
    """A hand-written line can hold an id no writer could store (a lone surrogate): it is not a
    valid event, so it shows as invalid_event, and an id like it on the command line is escaped."""
    from mailroom_record.models import GENESIS
    from record_testkit import hand_line

    blobs = BlobStore(tmp_path)
    EventLog(tmp_path, blobs).create_run(Run(**run_fields()))
    text = json.dumps(hand_line("r1", "dX", 0, GENESIS)).replace('"dX"', '"d\\ud800\\u202e"')
    (tmp_path / "runs" / "r1" / "segments" / "w0.jsonl").write_bytes(text.encode() + b"\n")
    res = run_cli("trace", "run", "r1", root=tmp_path)
    assert res.exit_code == 0 and "invalid_event" in res.stdout, res.stderr
    assert _unsafe_chars(res.stdout + res.stderr) == []
    res = run_cli("trace", "doc", "d\ud800\u202e\x1b[31m", root=tmp_path)
    assert res.exit_code == 2 and res.stderr.count("\n") == 1 and _unsafe_chars(res.stderr) == []
