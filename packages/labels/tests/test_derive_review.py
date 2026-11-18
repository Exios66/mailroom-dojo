"""scripts/derive_review_snapshot.py: a new review snapshot whose notes name a role, not a
person's initials, with every other byte of every row unchanged."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from labels_testkit import review_row
from mailroom_labels.importers.common import ImportRefused
from mailroom_labels.importers.review import review_row_order
from mailroom_labels.layers import CURRENT_LAYER_SCHEMA
from mailroom_labels.snapshot import MANIFEST_NAME, load_snapshot, write_snapshot

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "derive_review_snapshot.py"
# Synthetic initials and date; the substitution is what is tested, not anyone's initials.
TAG = "top-reply rule (QXZ 2031-02-03)"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("derive_review_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(doc: str, field: str, note: Any, n: int) -> dict[str, Any]:
    row = review_row(doc, field, f"value {n}")
    row["origin"] = [f"review/decisions.jsonl:{n}"]
    if note is None:
        del row["note"]
    else:
        row["note"] = note
    return row


def _source(root: Path, rows: list[dict[str, Any]], *, kind: str = "review", sid: str = "review-src") -> Path:
    directory = root / sid
    write_snapshot(
        directory,
        {"decisions.jsonl": rows},
        sort_key=review_row_order,
        snapshot_id=sid,
        kind=kind,
        layer_schema=CURRENT_LAYER_SCHEMA[kind],
        source={"origin": "synthetic", "reviewed": "2031-02-03"},
        builder="test",
    )
    return directory


ROWS = [
    _row("D1", "sender", f"{TAG}: the reply on top is the document's message", 1),
    _row("D1", "recipient", f"{TAG}; debatable reading: AB 1999-01-01 is quoted text", 2),
    _row("D2", "sender", "plain note", 3),
    _row("D3", "sender", None, 4),
    _row("D4", "sender", f"see {TAG} later in the text", 5),
]


def _rows(directory: Path) -> list[dict[str, Any]]:
    return [r.data for r in load_snapshot(directory).rows()]


def test_only_the_leading_initials_in_notes_change(tmp_path: Path) -> None:
    script = _script()
    rows = [r for r in ROWS if "AB 1999" not in str(r.get("note")) and "see " not in str(r.get("note"))]
    src = _source(tmp_path, rows)
    out = tmp_path / "review-src-r2"
    manifest = script.derive(src, out, None)
    old, new = _rows(src), _rows(out)
    assert len(new) == len(old) and [review_row_order(r) for r in new] == [review_row_order(r) for r in old]
    for a, b in zip(old, new, strict=True):
        assert {k: v for k, v in a.items() if k != "note"} == {k: v for k, v in b.items() if k != "note"}
        if isinstance(a.get("note"), str):
            assert b["note"] == a["note"].replace("(QXZ 2031-02-03)", "(review desk 2031-02-03)")
        else:
            assert "note" not in b
    assert "QXZ" not in (out / "decisions.jsonl").read_text(encoding="utf-8")
    derived = manifest.source["derived_from"]
    src_manifest = load_snapshot(src).manifest
    assert derived["snapshot_id"] == "review-src" and derived["manifest_hash"] == src_manifest.manifest_hash
    assert derived["notes_changed"] == 1
    sha = src_manifest.files[0].sha256
    assert derived["files"] == [{"path": "decisions.jsonl", "sha256": sha, "rows": 3}]
    assert {k: v for k, v in manifest.source.items() if k != "derived_from"} == dict(src_manifest.source)
    assert manifest.snapshot_id == "review-src-r2" and manifest.kind == "review"


def test_a_note_left_with_initials_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    script = _script()
    for i, bad in enumerate(("AB 1999-01-01", "see")):
        rows = [r for r in ROWS if bad in str(r.get("note"))]
        src = _source(tmp_path / str(i), rows)
        with pytest.raises(ImportRefused, match="initials token"):
            script.derive(src, tmp_path / str(i) / "review-src-r2", None)
        assert not (tmp_path / str(i) / "review-src-r2").exists()


def test_refusals(tmp_path: Path) -> None:
    script = _script()
    src = _source(tmp_path, ROWS[:1])
    with pytest.raises(ImportRefused, match="its own id"):
        script.derive(src, tmp_path / "x" / "review-src", None)
    hub = tmp_path / "hub-src"
    write_snapshot(hub, {}, snapshot_id="hub-src", kind="hub", layer_schema="x", source={}, builder="t")
    with pytest.raises(ImportRefused, match="not a review snapshot"):
        script.derive(hub, tmp_path / "hub-src-r2", None)


def test_known_warnings_are_read_under_the_new_id(tmp_path: Path) -> None:
    script = _script()
    src = _source(tmp_path, ROWS[:1])
    known = tmp_path / "known.yaml"
    entry = "  - {kind: orphan_decision, doc: D1, field: sender, reason: synthetic}\n"
    known.write_text("review-src:\n" + entry.replace("D1", "D9") + "review-src-r2:\n" + entry)
    manifest = script.derive(src, tmp_path / "review-src-r2", known)
    assert [w["doc"] for w in manifest.known_warnings] == ["D1"]


def test_main_writes_then_check_is_identical_and_catches_an_edit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _script()
    src = _source(tmp_path, [ROWS[0], ROWS[2], ROWS[3]])
    out = tmp_path / "review-src-r2"
    assert script.main(["--source", str(src), "--out", str(out)]) == 0
    assert "1 note(s) changed" in capsys.readouterr().out
    assert script.main(["--source", str(src), "--out", str(out), "--check"]) == 0
    assert capsys.readouterr().out.strip() == "identical"
    manifest = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    (out / MANIFEST_NAME).write_text(json.dumps({**manifest, "builder": "edited"}), encoding="utf-8")
    assert script.main(["--source", str(src), "--out", str(out), "--check"]) == 1
    assert "differs from a fresh build: manifest.json" in capsys.readouterr().err
    assert script.main(["--source", str(tmp_path / "nowhere"), "--out", str(tmp_path / "y")]) == 1
