"""The ``mailroom-labels`` command line.

``verify``   load and check snapshots against a client profile; print every finding.
             Exit 0 with no error, 1 with any error (warnings never fail it).
``coverage`` verify, then resolve and write the coverage report (JSON and Markdown).
             Exit 1, writing nothing, when verification finds an error.

Usage errors (a missing option, a profile that does not validate) exit 2.
Importers arrive with the real data (plan Task 2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from mailroom_labels.coverage import coverage as build_coverage
from mailroom_labels.coverage import render_markdown
from mailroom_labels.errors import ProfileError
from mailroom_labels.layers import corpus_index, read_layers
from mailroom_labels.profile import ClientProfile, load_profile
from mailroom_labels.quality import QualityReport, check_dirs
from mailroom_labels.resolve import resolve
from mailroom_labels.snapshot import snapshot_refs

__all__ = ["app"]

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Label snapshots: verify and report.")

SnapshotArgs = Annotated[list[Path], typer.Argument(help="Snapshot directories (each holds manifest.json).")]
ProfileOpt = Annotated[Path, typer.Option("--profile", help="Client profile.yaml.")]


def _profile(path: Path) -> ClientProfile:
    try:
        return load_profile(path)
    except ProfileError as exc:
        typer.echo(f"profile error: {exc}", err=True)
        raise typer.Exit(2) from exc


def _print_report(report: QualityReport) -> None:
    for f in report.findings:
        where = " ".join(x for x in (f.doc, f.field, f.layer) if x)
        known = " (known)" if f.known else ""
        typer.echo(f"{f.severity.upper()} {f.kind}{known}: {where} {f.detail} [{f.location or '-'}]".rstrip())
    typer.echo(f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)")


@app.command()
def verify(snapshots: SnapshotArgs, profile: ProfileOpt) -> None:
    """Check snapshots; exit 1 on any error."""
    prof = _profile(profile)
    report, _ = check_dirs(snapshots, prof)
    _print_report(report)
    raise typer.Exit(0 if report.ok else 1)


@app.command()
def coverage(
    snapshots: SnapshotArgs,
    profile: ProfileOpt,
    json_out: Annotated[Path, typer.Option("--json-out", help="Where to write the JSON report.")],
    md_out: Annotated[Path, typer.Option("--md-out", help="Where to write the Markdown report.")],
) -> None:
    """Verify, then write the coverage report; exit 1 (writing nothing) on any error."""
    prof = _profile(profile)
    report, loaded = check_dirs(snapshots, prof)
    if not report.ok:
        _print_report(report)
        typer.echo("coverage not written: fix the errors first", err=True)
        raise typer.Exit(1)
    read = read_layers(loaded)
    docs = resolve(read.records, prof, corpus_index(read.corpus))
    result = build_coverage(docs, prof, snapshot_refs(loaded))
    json_out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_out.write_text(render_markdown(result), encoding="utf-8")
    t = result["totals"]
    typer.echo(f"{t['documents']} documents, {t['scorable_documents']} with a scorable field, "
               f"{t['scorable_labels']} scorable labels")


def main() -> None:  # pragma: no cover - console entry convenience
    app()
