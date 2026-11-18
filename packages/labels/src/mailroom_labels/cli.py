"""The ``mailroom-labels`` command line.

``verify``   load and check snapshots against a client profile; print every finding.
             Exit 0 with no error, 1 with any error. Warnings fail it only with
             ``--strict``, and then only those its snapshot's known_warnings do not accept.
``coverage`` verify, then resolve and write the coverage report (JSON and Markdown).
             Exit 1, writing nothing, when verification finds an error.

``import-hub``, ``import-interim``, ``import-near-gold``, ``import-review``
             build a snapshot from local source files (never the network). With
             ``--check`` they build into a temporary directory and compare it byte for
             byte with the committed snapshot instead of writing: exit 0 identical,
             1 different. A source that fails an integrity check exits 1, and so does
             a source with data the importer has to leave out (rejected lines,
             unparseable rows, undecided decisions) unless ``--allow-rejected`` is
             given; then it is listed in the manifest and ``verify`` reports it as an
             error.

Usage errors (a missing option, a profile that does not validate) exit 2.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from mailroom_labels.coverage import coverage as build_coverage
from mailroom_labels.coverage import render_markdown
from mailroom_labels.errors import ProfileError
from mailroom_labels.importers import (
    ImportRefused,
    build_hub,
    build_interim,
    build_near_gold,
    build_review,
    check_snapshot,
)
from mailroom_labels.importers.common import file_sha256, known_warnings_for
from mailroom_labels.importers.hub import load_hub_pins
from mailroom_labels.layers import corpus_index, read_layers
from mailroom_labels.models import SNAPSHOT_ID_PATTERN
from mailroom_labels.profile import ClientProfile, load_profile
from mailroom_labels.quality import QualityReport, check_dirs
from mailroom_labels.quality import verify as quality_verify
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
def verify(
    snapshots: SnapshotArgs,
    profile: ProfileOpt,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict", help="Also exit 1 on a warning that its snapshot's known_warnings does not accept."
        ),
    ] = False,
) -> None:
    """Check snapshots; exit 1 on any error (and, with --strict, on any unknown warning)."""
    prof = _profile(profile)
    report, _ = check_dirs(snapshots, prof)
    _print_report(report)
    if strict and report.unknown_warnings:
        typer.echo(
            f"--strict: {len(report.unknown_warnings)} warning(s) not known for their snapshot", err=True
        )
    raise typer.Exit(quality_verify(report, strict=strict))


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
    typer.echo(
        f"{t['documents']} documents, {t['scorable_documents']} with a scorable field, "
        f"{t['scorable_labels']} scorable labels"
    )


OutOpt = Annotated[
    Path, typer.Option("--out", help="Snapshot directory to write (or to compare with --check).")
]
CheckOpt = Annotated[bool, typer.Option("--check", help="Compare a fresh build with --out; write nothing.")]
IdOpt = Annotated[
    str | None, typer.Option("--snapshot-id", help="Snapshot id (default: the --out directory name).")
]
AllowOpt = Annotated[
    bool,
    typer.Option(
        "--allow-rejected",
        help=(
            "Write the snapshot even though source data was left out "
            "(listed in the manifest; verify then fails)."
        ),
    ),
]
KnownOpt = Annotated[
    Path | None,
    typer.Option(
        "--known-warnings",
        help="YAML {snapshot_id: [{kind, doc, field, reason}]}: warnings accepted in the manifest.",
    ),
]


def _usage_error(message: str) -> typer.Exit:
    typer.echo(f"usage error: {message}", err=True)
    return typer.Exit(2)


def _run_import(out: Path, check: bool, build: Callable[[Path], Any], sid: str) -> None:
    """Build (or ``--check``) a snapshot. A refused source exits 1; a bad option (an
    invalid snapshot id, an unreadable option file, an ``--out`` that holds something else)
    exits 2 with a one-line message, never a traceback."""
    if not re.fullmatch(SNAPSHOT_ID_PATTERN, sid):
        raise _usage_error(
            f"snapshot id {sid!r} (from --snapshot-id or the --out name) must match {SNAPSHOT_ID_PATTERN}"
        )
    try:
        if check:
            result = check_snapshot(out, build)
            for line in result.differences:
                typer.echo(line)
            typer.echo(
                f"{out.name}: "
                + ("identical to a fresh build" if result.identical else "DIFFERS from a fresh build")
            )
            raise typer.Exit(0 if result.identical else 1)
        manifest = build(out)
    except ImportRefused as exc:
        typer.echo(f"import refused: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (ValueError, OSError, yaml.YAMLError) as exc:
        raise _usage_error(f"{exc.__class__.__name__}: {exc}") from exc
    rows = sum(f.rows for f in manifest.files)
    typer.echo(
        f"wrote {out} ({len(manifest.files)} file(s), {rows} row(s), manifest_hash {manifest.manifest_hash})"
    )


@app.command("import-hub")
def import_hub(
    out: OutOpt,
    profile: ProfileOpt,
    dataset_dir: Annotated[
        Path, typer.Option("--dataset-dir", help="manifest.txt and parquet/ground_truth shards.")
    ],
    revision: Annotated[str, typer.Option("--revision", help="Dataset revision (40-hex commit).")],
    pins: Annotated[
        Path,
        typer.Option(
            "--pins",
            help="YAML {revision: {path: sha256}}: the files must be exactly those pinned for --revision.",
        ),
    ],
    repository: Annotated[str, typer.Option("--repository")] = "Lucius-Morningstar/mailroom-dataset",
    default_dir: Annotated[
        Path | None, typer.Option("--default-dir", help="parquet/default shards: run the text check.")
    ] = None,
    snapshot_id: IdOpt = None,
    known_warnings: KnownOpt = None,
    allow_rejected: AllowOpt = False,
    check: CheckOpt = False,
) -> None:
    """Import the Hub ground_truth config (verified against manifest.txt)."""
    prof = _profile(profile)
    sid = snapshot_id or out.name
    try:
        pinned = load_hub_pins(pins)
    except (ImportRefused, OSError, yaml.YAMLError) as exc:
        raise _usage_error(f"--pins {pins}: {exc.__class__.__name__}: {exc}") from exc
    _run_import(
        out,
        check,
        lambda d: build_hub(
            d,
            dataset_dir=dataset_dir,
            profile=prof,
            profile_sha256=file_sha256(profile),
            repository=repository,
            revision=revision,
            default_dir=default_dir,
            snapshot_id=sid,
            known_warnings=known_warnings_for(known_warnings, sid),
            allow_rejected=allow_rejected,
            pins=pinned,
        ),
        sid,
    )


def _overlay_command(name: str, build: Callable[..., Any], what: str) -> None:
    def command(
        out: OutOpt,
        source_dir: Annotated[
            Path, typer.Option("--source-dir", help="The overlay directory (MANIFEST.json + .jsonl).")
        ],
        source_path: Annotated[str, typer.Option("--source-path", help="Its path inside the repository.")],
        repository: Annotated[str, typer.Option("--repository")],
        commit: Annotated[str, typer.Option("--commit", help="The repository commit the files are from.")],
        snapshot_id: IdOpt = None,
        known_warnings: KnownOpt = None,
        allow_rejected: AllowOpt = False,
        check: CheckOpt = False,
    ) -> None:
        sid = snapshot_id or out.name
        _run_import(
            out,
            check,
            lambda d: build(
                d,
                source_dir=source_dir,
                source_path=source_path,
                repository=repository,
                commit=commit,
                snapshot_id=sid,
                known_warnings=known_warnings_for(known_warnings, sid),
                allow_rejected=allow_rejected,
            ),
            sid,
        )

    command.__doc__ = f"Import the sandbox {what} overlay (hashes checked against its MANIFEST.json)."
    app.command(name)(command)


_overlay_command("import-interim", build_interim, "interim")
_overlay_command("import-near-gold", build_near_gold, "near-gold")


def _named(items: list[str], root: Path | None) -> list[tuple[Path, str]]:
    out = []
    for item in items:
        path = (root / item) if root is not None else Path(item)
        out.append((path, item if root is not None else path.name))
    return out


@app.command("import-review")
def import_review(
    out: OutOpt,
    decisions: Annotated[
        list[str],
        typer.Option("--decisions", help="Decision file (review shape), relative to --decisions-root."),
    ] = [],  # noqa: B006
    desk: Annotated[
        list[str], typer.Option("--desk", help="Desk log (human_reviews shape), relative to --desk-root.")
    ] = [],  # noqa: B006
    decisions_root: Annotated[Path | None, typer.Option("--decisions-root")] = None,
    desk_root: Annotated[Path | None, typer.Option("--desk-root")] = None,
    provenance: Annotated[
        str, typer.Option("--provenance", help="JSON object recorded as the source's origin.")
    ] = "{}",
    snapshot_id: IdOpt = None,
    known_warnings: KnownOpt = None,
    allow_rejected: AllowOpt = False,
    check: CheckOpt = False,
) -> None:
    """Import review decisions: human desk decisions are gold, Claude audit decisions near_gold."""
    try:
        prov = json.loads(provenance)
    except json.JSONDecodeError as exc:
        typer.echo(f"--provenance is not JSON: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not isinstance(prov, dict) or not (decisions or desk):
        typer.echo("give --decisions and/or --desk, and --provenance as a JSON object", err=True)
        raise typer.Exit(2)
    sid = snapshot_id or out.name
    _run_import(
        out,
        check,
        lambda d: build_review(
            d,
            decisions=_named(decisions, decisions_root),
            desk=_named(desk, desk_root),
            provenance=prov,
            snapshot_id=sid,
            known_warnings=known_warnings_for(known_warnings, sid),
            allow_rejected=allow_rejected,
        ),
        sid,
    )


def main() -> None:  # pragma: no cover - console entry convenience
    app()
