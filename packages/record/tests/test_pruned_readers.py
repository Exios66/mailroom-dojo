"""A pruned run is its own state (adversarial round 3, finding 3).

Pruning removes a run's segments and seals (and with ``keep_columns=False`` its Parquet
files) and leaves ``runs/<run>/prune.json``. Nothing may then read the run as broken (a
``seal_missing`` beside the kept columns) or as a valid empty run (zero events), and no
reader the Dojo could use may hand back its events as an empty list: verify reports
"pruned", and every content read raises ``RunPruned`` naming the archive.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from record_testkit import build_two_run_sealed_store, run_cli

import mailroom_record.verify as verify_module
from mailroom_record import Index, replay_timeline, trace_doc, trace_run, version_chain, verify_run
from mailroom_record.log import RunPruned
from mailroom_record.pack import pack_run, prune_run, unpack_run

MODES = pytest.mark.parametrize("keep_columns", [True, False], ids=["keep-columns", "drop-columns"])


def _pruned(tmp_path: Path, keep_columns: bool) -> SimpleNamespace:
    s = build_two_run_sealed_store(tmp_path / "store")
    s.archive = tmp_path / "r1.tar.zst"
    pack_run(s.root, "r1", s.archive)
    prune_run(s.root, "r1", keep_columns=keep_columns)
    return s


@MODES
def test_verify_reports_a_pruned_run_as_pruned(tmp_path: Path, keep_columns: bool) -> None:
    s = _pruned(tmp_path, keep_columns)
    report = verify_run(*s.parts, "r1")
    assert report.pruned_archive == str(s.archive.resolve()) and report.pruned_seal == 0
    assert not report.ok and report.problems == [] and report.events_checked == 0
    assert verify_run(*s.parts, "r2").ok and verify_run(*s.parts, "r2").pruned_archive is None

    res = run_cli("verify", "r1", root=s.root)
    assert res.exit_code == 0, res.stderr
    assert res.stdout.startswith("PRUNED run r1") and "r1.tar.zst" in res.stdout and "seal 0" in res.stdout
    assert "NOT OK" not in res.stdout and "seal_missing" not in res.stdout
    as_json = json.loads(run_cli("verify", "r1", "--json", root=s.root).stdout)
    assert as_json["pruned_archive"] == str(s.archive.resolve()) and as_json["ok"] is False


@MODES
def test_content_reads_refuse_a_pruned_run(tmp_path: Path, keep_columns: bool) -> None:
    s = _pruned(tmp_path, keep_columns)
    archive = str(s.archive.resolve())
    reads = {
        "iter_events": lambda: list(s.log.iter_events("r1")),
        "iter_events(doc)": lambda: list(s.log.iter_events("r1", "d1")),
        "iter_located": lambda: list(s.log.iter_located("r1")),
        "scan": lambda: s.log.scan("r1"),
        "trace_run": lambda: trace_run(s.log, None, "r1"),
        "replay_timeline": lambda: replay_timeline(s.log, "r1"),
        "version_chain": lambda: version_chain(s.log, "r1", "d1"),
    }
    for name, read in reads.items():
        with pytest.raises(RunPruned, match="pruned") as info:
            read()
        assert archive in str(info.value), name
    for args in (("trace", "run", "r1"), ("versions", "r1", "d1")):
        res = run_cli(*args, root=s.root)
        assert res.exit_code == 1 and "RunPruned" in res.stderr, args
    assert len(list(s.log.iter_events("r2"))) == 3  # other runs read as before


@MODES
def test_the_index_refuses_a_pruned_run_and_indexes_the_rest(tmp_path: Path, keep_columns: bool) -> None:
    s = _pruned(tmp_path, keep_columns)
    index = Index(s.root / "index.sqlite")
    stats = index.reindex(s.log)
    assert stats.pruned_runs == ("r1",) and stats.runs == 1 and stats.documents == 2
    assert index.pruned_runs() == {"r1": str(s.archive.resolve())}
    for read in (lambda: index.documents("r1"), lambda: index.review_queue("r1"), lambda: list(index.iter_events(s.log, "r1", "d1"))):
        with pytest.raises(RunPruned, match="r1.tar.zst"):
            read()
    assert [d.doc_id for d in index.documents("r2")] == ["d1", "d2"]
    assert index.drift(s.log) == []
    res = run_cli("reindex", root=s.root)
    assert res.exit_code == 0 and "pruned (not indexed): r1" in res.stdout


def test_an_index_rebuilt_before_the_prune_answers_from_its_cache_until_a_reindex(tmp_path: Path) -> None:
    """``prune_run`` does not touch the index (a cache the caller places): its ``documents``
    and ``review_queue`` answer from the last rebuild until ``reindex``, and ``drift`` says
    so; ``iter_events`` checks the log and refuses at once (as the README states)."""
    s = build_two_run_sealed_store(tmp_path / "store")
    index = Index(s.root / "index.sqlite")
    index.reindex(s.log)
    before = index.documents("r1")
    pack_run(s.root, "r1", tmp_path / "r1.tar.zst")
    prune_run(s.root, "r1")
    assert index.documents("r1") == before and index.pruned_runs() == {}
    assert index.drift(s.log) != []
    with pytest.raises(RunPruned, match="r1.tar.zst"):
        list(index.iter_events(s.log, "r1", "d1"))
    index.reindex(s.log)
    with pytest.raises(RunPruned, match="r1.tar.zst"):
        index.documents("r1")


@MODES
def test_trace_doc_names_the_pruned_runs_it_could_not_read(tmp_path: Path, keep_columns: bool) -> None:
    s = _pruned(tmp_path, keep_columns)
    t = trace_doc(s.log, Index(s.root / "index.sqlite"), "d1")
    assert [r.run_id for r in t.runs] == ["r2"]
    assert t.pruned_runs == {"r1": str(s.archive.resolve())}
    res = run_cli("trace", "doc", "d1", root=s.root)
    assert res.exit_code == 0 and "pruned runs not read" in res.stdout and "r1" in res.stdout


def test_a_prune_that_lands_during_verify_reads_as_pruned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fault injection: the prune runs while verify scans, deleting the segments under it.
    The result is "pruned", never a list of damage the prune made."""
    s = build_two_run_sealed_store(tmp_path / "store")
    archive = tmp_path / "r1.tar.zst"
    pack_run(s.root, "r1", archive)
    real = verify_module._scan

    def scan_while_pruned(log: object, run_id: str, st: object) -> None:
        if run_id == "r1" and not (s.root / "runs" / "r1" / "prune.json").exists():
            prune_run(s.root, "r1")
        real(log, run_id, st)  # type: ignore[arg-type]

    monkeypatch.setattr(verify_module, "_scan", scan_while_pruned)
    report = verify_run(*s.parts, "r1")
    assert report.pruned_archive == str(archive.resolve()) and report.problems == []


@MODES
def test_unpacking_brings_every_reader_back(tmp_path: Path, keep_columns: bool) -> None:
    s = _pruned(tmp_path, keep_columns)
    unpack_run(s.archive, s.root)
    assert verify_run(*s.parts, "r1").ok and verify_run(*s.parts, "r1").pruned_archive is None
    assert len(list(s.log.iter_events("r1"))) == 3 and trace_run(s.log, None, "r1").events == 3
    index = Index(s.root / "index.sqlite")
    assert index.reindex(s.log).pruned_runs == () and len(index.documents("r1")) == 2
