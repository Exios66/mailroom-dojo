"""Task 10: importing sandbox run folders, and the routing-input audit (synthetic fixtures)."""

from __future__ import annotations

import contextlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from record_testkit import (
    FIXTURE_RUN_ID,
    SENTINEL,
    copy_fixture,
    count_blobs,
    named_blobs,
    read_jsonl,
    run_cli,
    segment_bytes,
    source_ids,
    write_jsonl,
)

from mailroom_record.blobs import BlobStore
from mailroom_record.importer.audit import ROUTING_INPUTS, audit_sandbox_run
from mailroom_record.importer.sandbox_run import (
    ImportConflict,
    SandboxFolderInvalid,
    import_sandbox_run,
)
from mailroom_record.log import EventLog, LogWriter
from mailroom_record.seal import list_seals
from mailroom_record.verify import verify_run

RUN = FIXTURE_RUN_ID
IDS = source_ids(Path(__file__).parent / "fixtures" / "sandbox_run_min")
DOC1, DOC2, DOC3 = IDS.values()


def events_of(store: SimpleNamespace, run_id: str = RUN, doc_id: str | None = None) -> list:
    return list(store.log.iter_events(run_id, doc_id))


# --- the plan's tests -------------------------------------------------------------------


def test_import_fixture_folder(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    r = import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    assert (r.status, r.documents) == ("imported", 3)
    assert r.run_id == RUN
    assert verify_run(tmp_store.log, tmp_store.blobs, r.run_id).ok
    assert "tokens_in per call" in r.absent_fields
    assert r.events == len(events_of(tmp_store))


def test_second_import_is_a_no_op(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:  # Review Focus 2
    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    blobs_before = tmp_store.blobs.size_on_disk()
    segments_before = segment_bytes(tmp_store.root)
    seals_before = list_seals(tmp_store.log, RUN)
    again = import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    assert again.status == "already_imported"
    assert tmp_store.blobs.size_on_disk() == blobs_before
    assert segment_bytes(tmp_store.root) == segments_before
    assert list_seals(tmp_store.log, RUN) == seals_before
    assert (again.documents, again.events) == (3, len(events_of(tmp_store)))


def test_same_document_in_two_runs_is_one_blob(
    tmp_store: SimpleNamespace, fixture_folder: Path, fixture_folder_replicate: Path
) -> None:
    a = import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    b = import_sandbox_run(fixture_folder_replicate, tmp_store.log, tmp_store.blobs)
    assert (a.run_id, b.run_id, b.status) == (RUN, "ar-fixture-r01", "imported")
    assert count_blobs(tmp_store, role="original") == 3
    on_disk = sorted(p.name for p in tmp_store.root.glob("blobs/*/*/*.zst"))
    assert len(on_disk) == len(set(on_disk)) == len(named_blobs(tmp_store.log))


def test_node_decisions_survive(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    ev = [e for e in tmp_store.log.iter_events(RUN, DOC1) if e.node == "judge_verify"]
    assert ev[0].decision[0]["question"] == "extraction_verdict" and ev[0].decision[0]["answer"] == "partial"
    classify = [e for e in events_of(tmp_store, doc_id=DOC1) if e.node == "classify"][0]
    # the row's decisions as given, minus the options text (model output)
    assert classify.decision == [
        {"question": "doc_type", "type": "choice", "answer": "contract", "p": 0.93, "expected": None, "source": "agent"}
    ]
    assert classify.kind == "decision" and classify.agent == "sorter" and classify.model == "vendor/test-model"
    assert classify.latency_ms == 750.0


def test_labels_are_not_imported(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    dumped = json.dumps([e.model_dump() for e in tmp_store.log.iter_events(RUN)])
    assert "expected_fields" not in dumped
    for label_key in ("expected_doc_class", "expected_subclass", "expected_stage", f"{SENTINEL} expected"):
        assert label_key not in dumped
    assert "expected_fields" in (fixture_folder / "dataset.jsonl").read_text()  # positive control
    first = events_of(tmp_store, doc_id=DOC1)[0]
    assert first.payload["draw_class"] == "contract"


def test_missing_optional_files_are_absent_not_errors(
    tmp_store: SimpleNamespace, fixture_folder_no_prompt_lock: Path
) -> None:
    r = import_sandbox_run(fixture_folder_no_prompt_lock, tmp_store.log, tmp_store.blobs)
    assert r.status == "imported" and "prompt_hashes" in r.absent_fields
    assert tmp_store.log.read_run(r.run_id).prompt_hashes == {}


def test_audit_reports_routing_inputs(fixture_folder: Path) -> None:
    a = audit_sandbox_run(fixture_folder)
    assert "extraction_confidence" in a.present and "decision slot outputs" in a.absent


# --- mapping rules ------------------------------------------------------------------------


def test_run_header_follows_the_mapping(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    run = tmp_store.log.read_run(RUN)
    lock = json.loads((fixture_folder / "spec.lock.json").read_text())
    prompt_lock = json.loads((fixture_folder / "prompt.lock.json").read_text())
    assert (run.origin, run.kind, run.backend) == ("imported_sandbox", "full_pipeline", "openrouter")
    assert (run.policy_name, run.policy_hash, run.signed_by) == ("legacy", None, "ZZ")
    assert run.agent_models == lock["agent_models"]
    assert run.prompt_hashes == {a: v["sha256"] for a, v in prompt_lock["agents"].items()}
    assert run.code_version == "abc1234"
    assert run.source["folder"] == fixture_folder.name and run.source["spec_hash"] == lock["spec_hash"]
    assert run.document_ids == [DOC1, DOC2, DOC3]
    assert run.created_at.endswith("Z")


def test_specialist_only_when_there_is_no_classify_node(tmp_store: SimpleNamespace, tmp_path: Path) -> None:
    folder = copy_fixture(tmp_path / "spec-only", run_id="spec-only")
    write_jsonl(folder / "nodes.jsonl", [r for r in read_jsonl(folder / "nodes.jsonl") if r["node"] != "classify"])
    r = import_sandbox_run(folder, tmp_store.log, tmp_store.blobs)
    assert tmp_store.log.read_run(r.run_id).kind == "specialist_only"


def test_event_kinds_follow_the_order_of_the_rules(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    d2 = events_of(tmp_store, doc_id=DOC2)
    review = [e for e in d2 if e.node == "human_review"]
    assert [e.kind for e in review] == ["review"] and review[0].payload["status"] == "paused"
    intake = [e for e in d2 if e.node == "intake"][0]
    assert intake.kind == "handoff" and intake.payload["stage"] == "intake"
    d3 = events_of(tmp_store, doc_id=DOC3)
    failed = [e for e in d3 if e.node == "extract"][0]
    assert failed.kind == "error" and failed.payload["status"] == "error"
    assert failed.cause == "RateLimitError"  # the error's type, never its message text
    assert "synthetic quota note" not in json.dumps([e.model_dump() for e in events_of(tmp_store)])


def test_running_rows_pair_onto_their_finish(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    rows = read_jsonl(fixture_folder / "nodes.jsonl")
    finishing = [r for r in rows if r["status"] != "running"]
    node_events = [e for e in events_of(tmp_store) if e.payload.get("floor") == "node"]
    assert len(node_events) == len(finishing)  # a running row is not an event of its own
    judge = [e for e in events_of(tmp_store, doc_id=DOC1) if e.node == "judge_verify"][0]
    start = [r for r in rows if r["doc"].endswith("a1") and r["node"] == "judge_verify" and r["status"] == "running"][0]
    end = [r for r in rows if r["doc"].endswith("a1") and r["node"] == "judge_verify" and r["status"] == "done"][0]
    assert judge.payload["step"] == start["step"]
    assert _epoch(judge.payload["started_ts"]) == pytest.approx(start["ts"], abs=1e-6)
    assert _epoch(judge.ts) == pytest.approx(end["ts"], abs=1e-6)


def test_in_flight_node_is_one_running_handoff(tmp_store: SimpleNamespace, fixture_folder_crashed: Path) -> None:
    r = import_sandbox_run(fixture_folder_crashed, tmp_store.log, tmp_store.blobs)
    assert verify_run(tmp_store.log, tmp_store.blobs, r.run_id).ok
    d3 = list(tmp_store.log.iter_events(r.run_id, DOC3))
    last = d3[-1]
    assert (last.node, last.kind, last.payload["status"]) == ("extract", "handoff", "running")
    assert not [e for e in d3 if e.payload.get("floor") == "item"]  # no item row for it
    cp = list(tmp_store.log.iter_events(r.run_id, "_run"))[-1]
    assert (cp.node, cp.kind, cp.payload["state"], cp.payload["cursor"]) == ("checkpoint", "handoff", "running", 2)
    assert any("torn" in w for w in r.warnings)


def test_output_event_holds_the_outcome_and_blobs(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    out = events_of(tmp_store, doc_id=DOC1)[-1]
    assert out.kind == "output"
    assert out.decision == {
        "doc_type": "contract", "stage": "archived", "classification_confidence": 0.93,
        "extraction_confidence": 0.81, "review_causes": [], "review_verdict": None, "escalation_reason": None,
    }
    roles = {r.role: r.sha256 for r in out.outputs}
    assert set(roles) == {"extraction", "report"}
    extraction = json.loads(tmp_store.blobs.get(roles["extraction"]))
    assert "_report" not in extraction and extraction["amount"] == 0
    assert tmp_store.blobs.get(roles["report"]).decode().startswith("Report body")
    assert out.payload["legacy_score"]["field_f1"] == 0.5
    assert out.payload["usage_by_agent"]["sorter"]["calls"] == 1
    failed = events_of(tmp_store, doc_id=DOC3)[-1]
    assert (failed.kind, failed.cause, failed.outputs) == ("error", "RateLimitError", [])


def test_run_events_and_checkpoint_are_run_level_handoffs(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    run_events = events_of(tmp_store, doc_id="_run")
    source = read_jsonl(fixture_folder / "events.jsonl")
    assert [e.node for e in run_events] == [s["event"] for s in source] + ["checkpoint"]
    assert {e.kind for e in run_events} == {"handoff"}
    assert {"state": "done", "cursor": 3, "total": 3}.items() <= run_events[-1].payload.items()


def test_no_document_text_in_the_log(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    assert SENTINEL.encode() not in segment_bytes(tmp_store.root)
    # positive control: the text is in the source and reached the blob store
    assert SENTINEL in (fixture_folder / "dataset.jsonl").read_text()
    original = events_of(tmp_store, doc_id=DOC1)[0].outputs[0]
    assert original.role == "original" and SENTINEL.encode() in tmp_store.blobs.get(original.sha256)


# --- faults: crashes, conflicts and damaged folders ----------------------------------------


class _Crash(BaseException):
    """Stands in for the process dying (not an Exception, so nothing catches it)."""


def _crash_after(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    real = LogWriter.append
    count = {"n": 0}

    def append(self: LogWriter, **kw: object) -> object:
        if count["n"] >= n:
            self._buffer.clear()  # the process dies: buffered lines never reach the disk
            raise _Crash
        count["n"] += 1
        event = real(self, **kw)
        self.flush()  # durable at once, so the crash leaves exactly the first n events
        return event

    monkeypatch.setattr(LogWriter, "append", append)


@given(st.integers(min_value=0, max_value=60))
def test_a_crash_mid_import_resumes_to_the_same_record(tmp_path_factory: pytest.TempPathFactory, n: int) -> None:
    base = tmp_path_factory.mktemp("crash")
    folder = copy_fixture(base / "src")
    clean = SimpleNamespace(blobs=BlobStore(base / "clean"))
    clean.log = EventLog(base / "clean", clean.blobs)
    import_sandbox_run(folder, clean.log, clean.blobs)
    blobs = BlobStore(base / "crashed")
    log = EventLog(base / "crashed", blobs)
    with pytest.MonkeyPatch.context() as mp:
        _crash_after(mp, n)
        with contextlib.suppress(_Crash):
            import_sandbox_run(folder, log, blobs)
    r = import_sandbox_run(folder, log, blobs)
    assert r.status in ("imported", "already_imported")
    assert verify_run(log, blobs, RUN).ok
    assert log.doc_heads(RUN) == clean.log.doc_heads(RUN)
    assert list_seals(log, RUN)[-1].doc_heads == log.doc_heads(RUN)
    assert import_sandbox_run(folder, log, blobs).status == "already_imported"


def test_a_crash_before_the_seal_is_finished_by_the_next_import(
    tmp_store: SimpleNamespace, fixture_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mailroom_record.importer.sandbox_run as importer

    def no_seal(*a: object, **k: object) -> None:
        raise _Crash

    monkeypatch.setattr(importer, "seal_run", no_seal)
    with pytest.raises(_Crash):
        import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    monkeypatch.undo()
    assert list_seals(tmp_store.log, RUN) == []
    r = import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    assert r.status == "imported" and verify_run(tmp_store.log, tmp_store.blobs, RUN).ok
    assert list_seals(tmp_store.log, RUN)[-1].doc_heads == tmp_store.log.doc_heads(RUN)


def test_a_changed_folder_with_the_same_run_id_is_refused(
    tmp_store: SimpleNamespace, fixture_folder: Path, tmp_path: Path
) -> None:
    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    changed = tmp_path / "changed" / fixture_folder.name
    shutil.copytree(fixture_folder, changed)
    rows = read_jsonl(changed / "nodes.jsonl")
    rows[5]["ms"] = 1.0
    write_jsonl(changed / "nodes.jsonl", rows)
    before = segment_bytes(tmp_store.root)
    with pytest.raises(ImportConflict, match=RUN):
        import_sandbox_run(changed, tmp_store.log, tmp_store.blobs)
    assert segment_bytes(tmp_store.root) == before


def test_a_corrupt_line_inside_a_file_is_refused(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    lines = (fixture_folder / "nodes.jsonl").read_bytes().splitlines(keepends=True)
    lines[3] = b"{not json\n"
    (fixture_folder / "nodes.jsonl").write_bytes(b"".join(lines))
    with pytest.raises(SandboxFolderInvalid, match=r"nodes\.jsonl line 4"):
        import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    assert not (tmp_store.root / "runs").exists()


def test_a_row_naming_an_unknown_document_is_refused(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    rows = read_jsonl(fixture_folder / "items.jsonl")
    rows[0]["item_id"] = "DOC-ffffffffffffffff"
    write_jsonl(fixture_folder / "items.jsonl", rows)
    with pytest.raises(SandboxFolderInvalid, match="DOC-ffffffffffffffff"):
        import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)


def test_a_folder_without_a_dataset_is_refused(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    (fixture_folder / "dataset.jsonl").unlink()
    with pytest.raises(SandboxFolderInvalid, match="dataset.jsonl"):
        import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)


def test_errors_never_quote_document_text(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    rows = read_jsonl(fixture_folder / "dataset.jsonl")
    rows.append(dict(rows[0], id="DOC-0000000000000dd4"))  # the same content twice in one draw
    write_jsonl(fixture_folder / "dataset.jsonl", rows)
    with pytest.raises(SandboxFolderInvalid) as exc:
        import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    assert SENTINEL not in str(exc.value)


# --- the audit ----------------------------------------------------------------------------


def test_audit_counts_nodes_and_decisions(fixture_folder: Path) -> None:
    a = audit_sandbox_run(fixture_folder)
    assert (a.run_id, a.documents) == (RUN, 3)
    rows = read_jsonl(fixture_folder / "nodes.jsonl")
    assert a.nodes_seen["classify"] == sum(r["node"] == "classify" and r["status"] != "running" for r in rows)
    assert a.decisions_seen == {
        "doc_type": 3, "extraction_confidence": 1, "sorter_review": 1, "extraction_verdict": 1, "arbiter_route": 1,
    }
    assert sorted(a.present + a.absent) == sorted(ROUTING_INPUTS)
    assert set(a.absent) == {"decision slot outputs", "boss approval"}


def test_audit_sees_inputs_disappear(fixture_folder: Path) -> None:
    (fixture_folder / "nodes.jsonl").unlink()
    lock = json.loads((fixture_folder / "spec.lock.json").read_text())
    lock.pop("effective_confidence")
    (fixture_folder / "spec.lock.json").write_text(json.dumps(lock))
    a = audit_sandbox_run(fixture_folder)
    for gone in ("judge verdict per pass", "arbiter route per pass", "retry counts", "effective thresholds"):
        assert gone in a.absent
    assert "classification_confidence" in a.present  # the items still carry it


# --- command line -------------------------------------------------------------------------


def test_cli_import_and_audit(tmp_store: SimpleNamespace, fixture_folder: Path) -> None:
    first = run_cli("import", "sandbox-run", str(fixture_folder), root=tmp_store.root)
    assert first.exit_code == 0, first.stderr
    assert "imported" in first.stdout and RUN in first.stdout
    again = run_cli("import", "sandbox-run", str(fixture_folder), "--json", root=tmp_store.root)
    assert again.exit_code == 0 and json.loads(again.stdout)["status"] == "already_imported"
    audit = run_cli("audit", "sandbox-run", str(fixture_folder), "--json")
    assert audit.exit_code == 0 and "decision slot outputs" in json.loads(audit.stdout)["absent"]
    for out in (first.stdout, again.stdout, audit.stdout):
        assert SENTINEL not in out


def test_cli_import_of_a_bad_folder_is_one_line_exit_1(tmp_store: SimpleNamespace, tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    res = run_cli("import", "sandbox-run", str(tmp_path / "empty"), root=tmp_store.root)
    assert res.exit_code == 1 and "dataset.jsonl" in res.stderr and "Traceback" not in res.stderr


def _epoch(ts: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


# --- a pruned run (adversarial round 1, finding 2) --------------------------------------


def _run_tree(root: Path) -> dict[str, bytes]:
    run_dir = root / "runs" / RUN
    return {str(p.relative_to(run_dir)): p.read_bytes() for p in sorted(run_dir.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("interrupted", [False, True], ids=["pruned", "prune-interrupted"])
def test_reimport_of_a_pruned_run_refuses_and_writes_nothing(
    tmp_store: SimpleNamespace, fixture_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    """Import, pack, prune, import again: the segments are gone, so the import cannot check
    them against the folder. It refuses with RunPruned naming the archive to unpack, and
    writes nothing; after the unpack the same folder is already_imported."""
    import mailroom_record.pack as pack_module
    from mailroom_record.pack import pack_run, prune_run, unpack_run

    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    archive = tmp_path / "run.tar.zst"
    pack_run(tmp_store.root, RUN, archive)
    if interrupted:  # the prune dies after deleting the segments, before recording "done"
        real = pack_module._write_replace

        def write_replace(path: Path, body: bytes) -> None:
            if b'"done"' in body:
                raise OSError("injected: killed mid-prune")
            real(path, body)

        monkeypatch.setattr(pack_module, "_write_replace", write_replace)
        with pytest.raises(OSError, match="injected"):
            prune_run(tmp_store.root, RUN)
        monkeypatch.undo()
    else:
        prune_run(tmp_store.root, RUN)
    before = _run_tree(tmp_store.root)
    blobs_before = sorted(p.name for p in (tmp_store.root / "blobs").rglob("*") if p.is_file())

    # RunPruned is an ImportConflict, so callers that catch ImportConflict still stop
    with pytest.raises(ImportConflict, match="unpack") as info:
        import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    assert type(info.value).__name__ == "RunPruned"
    assert str(archive) in str(info.value)
    assert _run_tree(tmp_store.root) == before
    assert sorted(p.name for p in (tmp_store.root / "blobs").rglob("*") if p.is_file()) == blobs_before

    unpack_run(archive, tmp_store.root)
    assert import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs).status == "already_imported"
    assert verify_run(tmp_store.log, tmp_store.blobs, RUN).ok
