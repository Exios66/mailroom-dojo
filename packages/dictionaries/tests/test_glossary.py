"""The glossary: validation on real files, rendering, and the committed docs/glossary.md."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from mailroom_common.errors import ConfigError
from mailroom_dictionaries import load_glossary, render_glossary
from mailroom_dictionaries.glossary import GLOSSARY_PATH

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render_glossary.py"
REQUIRED_TERMS = {
    "Sorter",
    "Specialist",
    "Hand-off",
    "Run record",
    "Event",
    "Hash chain",
    "Seal",
    "Blob",
    "Tombstone",
    "Label snapshot",
    "Hub",
    "Interim",
    "Near-gold",
    "Gold",
    "Scorable field",
    "Field type",
    "Closed label",
    "Wrong archive",
    "Review rate",
    "Utility",
    "Routing policy",
    "Decision slot",
    "Review bin",
    "Client profile",
    "Prompt set",
    "Backend",
    "Draw",
    "Stage gate",
}


def glossary_file(tmp_path: Path, terms: list[dict[str, Any]]) -> Path:
    path = tmp_path / "glossary.yaml"
    path.write_text(yaml.safe_dump({"terms": terms}, sort_keys=False), encoding="utf-8")
    return path


def term(name: str, **extra: Any) -> dict[str, Any]:
    return {"term": name, "definition": f"{name} means something.", "source": "a source", **extra}


def render_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)


def test_shipped_glossary_has_the_required_terms_sorted() -> None:
    glossary = load_glossary()
    names = [t.term for t in glossary.terms]
    assert REQUIRED_TERMS <= set(names)
    assert names == sorted(names, key=str.casefold)
    assert all(t.source.strip() and t.definition.strip() for t in glossary.terms)


def test_committed_glossary_page_is_current() -> None:
    result = render_script("--check")
    assert result.returncode == 0, result.stderr


def test_render_links_see_also_to_heading_anchors(tmp_path: Path) -> None:
    path = glossary_file(tmp_path, [term("Hash chain", see_also=["Seal"], aliases=["chain"]), term("Seal")])
    text = render_glossary(load_glossary(path))
    assert (
        "## Hash chain\n\nHash chain means something.\n\nAlso called: chain.\n\nSee also: [Seal](#seal)."
        in text
    )
    assert text.index("## Hash chain") < text.index("## Seal")
    assert text.endswith("Source: a source\n")


@pytest.mark.parametrize(
    ("terms", "match"),
    [
        ([term("Seal"), term("seal")], "'seal' repeats 'Seal'"),
        ([term("Seal"), term("Blob", aliases=["SEAL"])], "'SEAL' repeats 'Seal'"),
        ([term("Seal", aliases=["x", "X"])], "'X' repeats 'x'"),
        ([term("Seal", definition="  ")], "term 'Seal': definition is empty"),
        ([term("Seal", source="")], "term 'Seal': source is empty"),
        ([term(" ")], "term ' ': term is empty"),
        ([term("Seal", aliases=[" "])], "an alias is empty"),
        ([term("Seal", see_also=["Blob"])], "see_also 'Blob' is not a term"),
        ([term("Seal", see_also=["seal"])], "see_also 'seal' is not a term"),
        ([term("Seal", see_also=["Seal"])], "see_also names the term itself"),
        ([term("Seal", colour="x")], r"terms\.0\.colour: Extra inputs are not permitted"),
        ([{"term": "Seal", "definition": "d"}], r"terms\.0\.source: Field required"),
        ([], "glossary has no terms"),
    ],
)
def test_invalid_glossaries(tmp_path: Path, terms: list[dict[str, Any]], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_glossary(glossary_file(tmp_path, terms))


def test_invalid_glossary_file(tmp_path: Path) -> None:
    path = tmp_path / "glossary.yaml"
    path.write_text("terms:\n  - term: Seal\n    term: Seal\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate mapping key 'term'"):
        load_glossary(path)
    path.write_text("- just a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid glossary file"):
        load_glossary(path)


def test_check_fails_on_a_stale_or_missing_page_and_render_fixes_it(tmp_path: Path) -> None:
    out = tmp_path / "glossary.md"
    missing = render_script("--check", "--output", str(out))
    assert missing.returncode == 1 and "stale:" in missing.stderr and not out.exists()
    out.write_text("# old\n", encoding="utf-8")
    stale = render_script("--check", "--output", str(out))
    assert stale.returncode == 1 and out.read_text(encoding="utf-8") == "# old\n"
    written = render_script("--output", str(out))
    assert written.returncode == 0
    assert out.read_text(encoding="utf-8") == render_glossary(load_glossary(GLOSSARY_PATH))
    assert render_script("--check", "--output", str(out)).returncode == 0


def test_script_reports_an_invalid_glossary(tmp_path: Path) -> None:
    bad = glossary_file(tmp_path, [term("Seal", see_also=["Nope"])])
    result = render_script("--glossary", str(bad), "--output", str(tmp_path / "out.md"))
    assert result.returncode == 1 and "see_also 'Nope' is not a term" in result.stderr
    assert not (tmp_path / "out.md").exists()
