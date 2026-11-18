"""Values a reader must refuse as data, never raise on: JSON nested deeper than
``snapshot.MAX_JSON_DEPTH``, and (defence in depth) any value the record models refuse.

Every path a scorer or an operator takes is covered: the row stream, the layer reader, the
quality gate, ``mailroom-labels verify --strict`` and ``load_label_set``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from labels_testkit import (
    PROFILE_PATH,
    hub_row,
    make_snapshot,
    profile,
    rehash_manifest,
    review_row,
    standard_set,
)
from mailroom_labels import snapshot as snapshot_module
from mailroom_labels.cli import app
from mailroom_labels.errors import CorruptLine
from mailroom_labels.labelset import QualityGateFailed, load_label_set
from mailroom_labels.layers import read_layer
from mailroom_labels.quality import check_dirs
from mailroom_labels.snapshot import MAX_JSON_DEPTH, load_snapshot

DEPTHS = [300, 5000]


def _nested(depth: int) -> str:
    """A JSON value of ``depth`` nested lists."""
    return "[" * depth + "]" * depth


def _review_line(value: str) -> bytes:
    """One review decision whose entry value is the raw JSON ``value`` (row depth: 2 + value)."""
    return (
        '{"doc_id":"D1","field":"sender","verdict":"correct",'
        '"reviewer":{"model":"synthetic-auditor","role":"audit"},'
        '"entry":{"value":' + value + "}}\n"
    ).encode()


def _append(directory: Path, line: bytes, file: str = "rows.jsonl") -> None:
    with (directory / file).open("ab") as fh:
        fh.write(line)
    rehash_manifest(directory)


@pytest.mark.parametrize("depth", DEPTHS)
def test_a_row_nested_too_deeply_is_a_corrupt_line(tmp_path: Path, depth: int) -> None:
    d = make_snapshot(tmp_path, "review", [review_row("D1", "sent_on", "2020-01-03")])
    _append(d, _review_line(_nested(depth)))
    with pytest.raises(CorruptLine) as info:
        list(load_snapshot(d).rows())
    assert "nested too deeply" in info.value.detail and info.value.line == 2
    read = read_layer(load_snapshot(d))  # the reader never raises on it
    assert [f.kind for f in read.findings] == ["corrupt_line"]
    assert [r.field for r in read.records] == ["sent_on"]  # its neighbour still reads


def test_the_depth_cap_is_exact(tmp_path: Path) -> None:
    """A row exactly ``MAX_JSON_DEPTH`` deep reads; one level more is a corrupt line."""
    d = make_snapshot(tmp_path, "review", [review_row("D1", "sent_on", "2020-01-03")])
    _append(d, _review_line(_nested(MAX_JSON_DEPTH - 2)))
    assert len(list(load_snapshot(d).rows())) == 2
    _append(d, _review_line(_nested(MAX_JSON_DEPTH - 1)))
    with pytest.raises(CorruptLine, match="nested too deeply"):
        list(load_snapshot(d).rows())


@pytest.mark.parametrize("depth", DEPTHS)
def test_verify_strict_reports_a_deep_row_and_exits_one(tmp_path: Path, depth: int) -> None:
    dirs = standard_set(tmp_path)
    _append(dirs[3], _review_line(_nested(depth)))
    result = CliRunner().invoke(app, ["verify", "--strict", "--profile", str(PROFILE_PATH), *map(str, dirs)])
    assert result.exit_code == 1, result.output
    assert not isinstance(result.exception, Exception) or isinstance(result.exception, SystemExit)
    assert "ERROR corrupt_line" in result.output and "nested too deeply" in result.output


@pytest.mark.parametrize("depth", DEPTHS)
def test_load_label_set_refuses_a_deep_row_with_the_gate_error(tmp_path: Path, depth: int) -> None:
    dirs = standard_set(tmp_path)
    _append(dirs[3], _review_line(_nested(depth)))
    with pytest.raises(QualityGateFailed) as info:
        load_label_set(dirs, profile())
    assert [f.kind for f in info.value.report.errors] == ["corrupt_line"]


@pytest.mark.parametrize("kind", ["review", "hub"])
def test_a_value_the_record_model_refuses_is_a_malformed_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Defence in depth: with the depth cap lifted, a value pydantic refuses (too deep for
    its validator) is a ``malformed_record`` finding, the row yields nothing (not even the
    labels of its earlier fields, nor a corpus entry) and its neighbours still read."""
    monkeypatch.setattr(snapshot_module, "MAX_JSON_DEPTH", 100_000)
    deep: object = []
    for _ in range(300):
        deep = [deep]
    if kind == "hub":
        rows = [hub_row("D1", fields={"amount": 1, "sender": deep}), hub_row("D2", fields={"sender": "Bo"})]
    else:
        rows = [review_row("D1", "sender", deep), review_row("D2", "sender", "Bo")]
    read = read_layer(load_snapshot(make_snapshot(tmp_path, kind, rows)))
    assert [(f.kind, f.doc) for f in read.findings] == [("malformed_record", "D1")]
    assert [(r.document_id, r.field) for r in read.records] == [("D2", "sender")]
    assert [c.document_id for c in read.corpus] == (["D2"] if kind == "hub" else [])
    report, _ = check_dirs([tmp_path / f"{kind}-test"], profile())
    assert "malformed_record" in {f.kind for f in report.errors}


@pytest.mark.parametrize("value", ['"\\ud800"', '"a\\udfffb"', '["ok", {"k": "\\ud83d"}]'])
def test_a_string_canonical_json_cannot_encode_is_a_corrupt_line(tmp_path: Path, value: str) -> None:
    """A lone surrogate escape parses, but ``mailroom_record.hashing.canonical_json`` cannot
    encode it as UTF-8, so no record could hash it: the line is corrupt, and the gate says so."""
    dirs = standard_set(tmp_path)
    _append(dirs[3], _review_line(value))
    with pytest.raises(CorruptLine, match="lone surrogate"):
        list(load_snapshot(dirs[3]).rows())
    with pytest.raises(QualityGateFailed) as info:
        load_label_set(dirs, profile())
    assert [f.kind for f in info.value.report.errors] == ["corrupt_line"]


def test_a_valid_surrogate_pair_still_reads(tmp_path: Path) -> None:
    d = make_snapshot(tmp_path, "review", [review_row("D1", "sent_on", "2020-01-03")])
    _append(d, _review_line('"\\ud83d\\ude00"'))
    assert [r.data["entry"]["value"] for r in load_snapshot(d).rows()][1] == "\U0001f600"
