"""Write the label data-quality report and the coverage report for a committed snapshot set.

    python packages/labels/scripts/labels_report.py \
        --profile config/clients/mailroom-dataset/profile.yaml \
        --crosscheck docs/reports/labels/sandbox-crosscheck-2026-11-18.json \
        --case DOC-672cec2c85c83c39:action_items --date 2026-11-18 --out-dir docs/reports/labels \
        labels/hub-ccd040f6 labels/interim-2026-11-01 labels/near-gold-2026-11-17 \
        labels/gold-review-2026-11-18-r2

Writes ``data-quality-<date>.md``, ``coverage-<date>.md`` and ``coverage-<date>.json``. Every
number comes from the snapshots (and, for the sandbox cross-check section, from the
``--crosscheck`` JSON, which a scratch script outside this repository produces by running
the sandbox's own merge; see that file's ``method``). ``--scrub-crosscheck`` rewrites that
JSON in place with every label value replaced by its token before it is committed. Never
edit the reports by hand.

The reports carry our own tokens only (ids, field and class names, ranks, counts, hashes),
never document or model free text: a label value is printed as its JSON type, its size and
the first 12 hex digits of the sha256 of its canonical JSON (``_token``), and a list in the
import accounting as its length.
Exit 0: written; 1: the set fails verification (nothing written); 2: usage.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from mailroom_labels.coverage import coverage, render_markdown
from mailroom_labels.layers import corpus_index, hash_matches, read_layers
from mailroom_labels.profile import load_profile
from mailroom_labels.quality import check_dirs, known_entry
from mailroom_labels.resolve import resolve
from mailroom_labels.snapshot import snapshot_refs
from mailroom_record.hashing import canonical_json

FILL_RANKS = ("interim:gap_fill", "interim:derived")


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out + [""]


_TOKEN_KEY = "$token"
#: The exact text ``_token`` generates. A ``{"$token": ...}`` object is trusted only when its
#: text has this shape; any other text under that key is a raw label that happens to look like
#: a token, and is tokenised like any other value.
_TOKEN_TEXT = re.compile(
    r"null|true|false"
    r"|string of \d+ chars, sha256:[0-9a-f]{12}"
    r"|list of \d+, sha256:[0-9a-f]{12}"
    r"|object of \d+ keys, sha256:[0-9a-f]{12}"
    r"|number, sha256:[0-9a-f]{12}"
)


def _token(value: Any) -> str:
    """A label value as our own token: its JSON type, size and a short hash, never its text.
    ``null``, ``true`` and ``false`` are printed as they are; they carry no document text.
    A value already scrubbed by ``scrub_crosscheck`` (``{"$token": ...}``) prints its token."""
    if (
        isinstance(value, dict)
        and set(value) == {_TOKEN_KEY}
        and isinstance(value[_TOKEN_KEY], str)
        and _TOKEN_TEXT.fullmatch(value[_TOKEN_KEY])
    ):
        return value[_TOKEN_KEY]
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    digest = sha256(canonical_json(value)).hexdigest()[:12]
    if isinstance(value, str):
        return f"string of {len(value)} chars, sha256:{digest}"
    if isinstance(value, list):
        return f"list of {len(value)}, sha256:{digest}"
    if isinstance(value, dict):
        return f"object of {len(value)} keys, sha256:{digest}"
    return f"number, sha256:{digest}"


_WORD = re.compile(r"[a-z_:]+")


def _outcome(value: Any) -> str:
    """A cross-check outcome ``[rank, presence, value]``: the rank and presence words (our
    own vocabulary) as they are, the value as a token. Anything else is one token."""
    if isinstance(value, list) and len(value) == 3:
        words = value[:2]
        if all(w is None or (isinstance(w, str) and _WORD.fullmatch(w)) for w in words):
            return ", ".join([*(json.dumps(w) if w is None else w for w in words), _token(value[2])])
    return _token(value)


def _scrubbed(value: Any) -> dict[str, str]:
    return {_TOKEN_KEY: _token(value)}


def _scrubbed_outcome(value: Any) -> Any:
    if isinstance(value, list) and len(value) == 3:
        words = value[:2]
        if all(w is None or (isinstance(w, str) and _WORD.fullmatch(w)) for w in words):
            return [*words, _scrubbed(value[2])]
    return _scrubbed(value)


def scrub_crosscheck(crosscheck: dict[str, Any]) -> dict[str, Any]:
    """The cross-check with every label value replaced by its token (``{"$token": ...}``).

    The scratch writer emits raw sandbox and audit values; the committed file holds only
    tokens. The report built from the scrubbed file is byte-identical to the one built from
    the raw file, and scrubbing twice changes nothing."""
    out = json.loads(json.dumps(crosscheck))
    for examples in out["examples"].values():
        for e in examples:
            e["sandbox"] = _scrubbed_outcome(e["sandbox"])
            e["ours"] = _scrubbed_outcome(e["ours"])
    a = out["audit_vs_sandbox_gold_catalog"]
    for x in a["different"]:
        for key in ("audit", "catalog", "near_gold_overlay"):
            x[key] = _scrubbed(x[key])
    a["not_in_catalog"] = [_scrubbed(x) for x in a["not_in_catalog"]]
    return out


def _counted(value: Any) -> Any:
    """Import accounting with every list replaced by its length: the keys are our own names
    and the leaves counts, but a list entry can quote a source value."""
    if isinstance(value, dict):
        return {k: _counted(v) for k, v in value.items()}
    if isinstance(value, list):
        return {"entries": len(value)}
    if isinstance(value, str):
        return _token(value)
    return value


def build(
    dirs: list[Path], profile_path: Path, crosscheck: dict | None, cases: list[tuple[str, str]], date: str
) -> tuple[str, dict, str] | None:
    profile = load_profile(profile_path)
    report, loaded = check_dirs(dirs, profile)
    if not report.ok:
        return None
    read = read_layers(loaded)
    index = corpus_index(read.corpus)
    docs = resolve(read.records, profile, index)
    by_doc = {d.document_id: d for d in docs}
    refs = snapshot_refs(loaded)
    cov = coverage(docs, profile, refs)
    md: list[str] = [f"# Label data quality: {date}", ""]
    md += ["Generated by `packages/labels/scripts/labels_report.py`; do not edit by hand.", ""]

    # --- snapshots
    md += ["## Snapshots", ""]
    md += _table(
        ["Snapshot", "Kind", "Files", "Rows", "Bytes", "Manifest hash"],
        [
            [
                s.snapshot_id,
                s.kind,
                len(s.manifest.files),
                sum(f.rows for f in s.manifest.files),
                sum(f.bytes for f in s.manifest.files),
                f"`{s.manifest_hash[:16]}…`",
            ]
            for s in loaded
        ],
    )

    # --- per-layer counts by class
    md += [
        "## Labels per layer and class",
        "",
        "Documents a layer labels and the labels it carries, by the class the Hub index gives the "
        "document (the layer's own class for a document the index lacks). Hub labels are counted "
        "by rank: `hub` populated, `hub:absent` documented absence or not_applicable.",
        "",
    ]
    layer_docs: dict[tuple[str, str], set] = defaultdict(set)
    layer_labels: Counter = Counter()
    for r in read.records:
        entry = index.get(r.document_id)
        cls = entry.doc_class if entry else (r.doc_class or "(unknown)")
        layer_docs[(r.snapshot_id, cls)].add(r.document_id)
        layer_labels[(r.snapshot_id, cls, r.rank)] += 1
    rows = []
    for (sid, cls), ds in sorted(layer_docs.items()):
        ranks = sorted((rank, n) for (s2, c2, rank), n in layer_labels.items() if (s2, c2) == (sid, cls))
        rows.append([sid, cls, len(ds), sum(n for _, n in ranks), ", ".join(f"{k} {n}" for k, n in ranks)])
    md += _table(["Snapshot", "Class", "Documents", "Labels", "By rank"], rows)
    hub_docs = Counter(e.doc_class for e in index.values())
    md += [
        f"Hub corpus index: {len(index)} documents ("
        + ", ".join(f"{k} {v}" for k, v in sorted(hub_docs.items()))
        + ").",
        "",
    ]

    # --- quality findings
    md += [
        "## Quality gate",
        "",
        f"`mailroom-labels verify`: {len(report.errors)} error(s), {len(report.warnings)} warning(s).",
        "",
    ]
    if report.warnings:
        md += _table(
            ["Kind", "Document", "Field", "Layer", "Known", "Reason"],
            [
                [
                    f.kind,
                    f.doc or "",
                    f.field or "",
                    f.layer or "",
                    "yes" if f.known else "**no**",
                    (known_entry(f, loaded) or {}).get("reason", ""),
                ]
                for f in report.warnings
            ],
        )

    # --- staleness and verification
    md += ["## Content hashes and verification", ""]
    stale_rows = []
    for snap in loaded:
        per = defaultdict(set)
        for r in read.records:
            if r.snapshot_id != snap.snapshot_id or r.layer == "hub":
                continue
            entry = index.get(r.document_id)
            if r.content_sha256 is None:
                per["no hash"].add(r.document_id)
            elif entry is None:
                per["not in index"].add(r.document_id)
            elif hash_matches(r.content_sha256, entry.content_sha256):
                per["prefix of content_sha256"].add(r.document_id)
            else:
                per["stale"].add(r.document_id)
        if snap.kind != "hub":
            stale_rows.append(
                [snap.snapshot_id]
                + [len(per[k]) for k in ("prefix of content_sha256", "stale", "no hash", "not in index")]
            )
    md += _table(
        [
            "Snapshot",
            "Docs whose hash is a prefix of the Hub content_sha256",
            "Stale",
            "No hash",
            "Not in the Hub index",
        ],
        stale_rows,
    )
    hub_snap = next((s for s in loaded if s.kind == "hub"), None)
    if hub_snap is not None:
        tc = hub_snap.manifest.source.get("text_check", {})
        md += [f"Hub text check at import: {json.dumps(tc, sort_keys=True)}.", ""]
    mism = Counter()
    for r in read.records:
        entry = index.get(r.document_id)
        if entry is None or r.layer == "hub":
            continue
        if r.doc_class is not None and r.doc_class != entry.doc_class:
            mism[(r.snapshot_id, "class")] += 1
        if r.subclass is not None and r.subclass != entry.subclass:
            mism[(r.snapshot_id, f"subclass {r.subclass} -> {entry.subclass}")] += 1
    md += ["Class and subclass spellings against the Hub index (labels):", ""]
    md += _table(
        ["Snapshot", "Disagreement", "Labels"],
        [[s, k, n] for (s, k), n in sorted(mism.items())] or [["-", "none", 0]],
    )
    unverified = [d for d in docs if not d.verified]
    md += [f"Unverified documents: {len(unverified)}.", ""]
    md += _table(
        ["Document", "Class", "Reasons", "Resolved fields kept for review"],
        [[d.document_id, d.doc_class, ", ".join(d.unverified_reasons), len(d.fields)] for d in unverified],
    )

    # --- precedence measurements
    md += ["## Precedence on the real data", ""]
    absent_fills = Counter()
    hub_overridden = 0
    for d in docs:
        for name, lab in d.fields.items():
            ranks = {f.rank for f in lab.layers_seen if not f.stale}
            if "hub:absent" in ranks:
                for f in lab.layers_seen:
                    if f.rank in FILL_RANKS and not f.stale:
                        hub_presence = next(x.presence for x in lab.layers_seen if x.rank == "hub:absent")
                        absent_fills[(d.doc_class, name, f.rank, hub_presence, lab.rank == f.rank)] += 1
            if "hub" in ranks and lab.rank not in ("hub", "review:gold", "interim:correction"):
                hub_overridden += 1
    md += ["Interim fills on a Hub absence (they replace it, as the sandbox's `apply_overlay` does):", ""]
    md += _table(
        ["Class", "Field", "Interim rank", "Hub presence", "Fill wins", "Labels"],
        [[*k[:4], "yes" if k[4] else "no", n] for k, n in sorted(absent_fills.items())] or [["-"] * 5 + [0]],
    )
    md += [
        "Populated Hub labels replaced by anything but a gold decision or an interim correction: "
        f"{hub_overridden}.",
        "",
    ]

    audit_vs = Counter()
    audit_rows = []
    for d in docs:
        for name, lab in d.fields.items():
            audits = [f for f in lab.layers_seen if f.rank == "review:near_gold"]
            for a in audits:
                for other in lab.layers_seen:
                    if (
                        other.rank
                        in (
                            "hub",
                            "interim:correction",
                            "interim:gap_fill",
                            "interim:derived",
                            "hub:absent",
                            "near_gold",
                        )
                        and not other.stale
                    ):
                        same = canonical_json(other.value) == canonical_json(a.value)
                        audit_vs[
                            (
                                other.rank,
                                "same value" if same else "different value",
                                "audit wins" if lab.rank == "review:near_gold" else f"{lab.rank} wins",
                            )
                        ] += 1
                        if not same and other.rank in ("hub", "interim:correction"):
                            audit_rows.append(
                                [d.document_id, name, other.rank, _token(other.value), _token(a.value)]
                            )
    md += ["Claude audit decisions (`review:near_gold`) against the other layers on the same label:", ""]
    md += _table(
        ["Other layer", "Values", "Outcome", "Labels"], [[*k, n] for k, n in sorted(audit_vs.items())]
    )
    md += [
        "Audit decisions that disagree with a populated Hub value or an interim correction: "
        f"{len(audit_rows)} "
        "(not applied; findings for the owner).",
        "",
    ]
    if audit_rows:
        md += _table(["Document", "Field", "Kept layer", "Kept value", "Audit value"], audit_rows)

    human = [
        (d.document_id, n, lab)
        for d in docs
        for n, lab in d.fields.items()
        if any(f.rank == "review:gold" for f in lab.layers_seen)
    ]
    md += [
        f"Human desk decisions: {len(human)}; "
        f"resolved to `gold`: {sum(lab.status == 'gold' for _, _, lab in human)}.",
        "",
    ]
    md += _table(
        ["Document", "Field", "Status", "Value", "Reviewer"],
        [
            [doc, n, lab.status, _token(lab.value), lab.reviewer]
            for doc, n, lab in sorted(human, key=lambda t: t[:2])
        ],
    )

    for doc_id, field in cases:
        d = by_doc.get(doc_id)
        md += [f"### Case {doc_id} {field}", ""]
        lab = d.fields.get(field) if d else None
        if lab is None:
            md += ["Not resolved (no trusted label).", ""]
            continue
        md += [
            f"Resolved: {_token(lab.value)} (rank `{lab.rank}`, status `{lab.status}`, "
            f"source `{lab.source}`). Every layer seen:",
            "",
        ]
        md += _table(
            ["Rank", "Source", "Value", "Stale"],
            [[f.rank, f.source, _token(f.value), f.stale] for f in lab.layers_seen],
        )

    # --- import accounting
    md += ["## Import accounting (nothing dropped silently)", ""]
    for snap in loaded:
        src = snap.manifest.source
        keep = {
            k: v
            for k, v in src.items()
            if k
            in (
                "omitted_not_applicable_placeholders",
                "excluded_metadata_keys",
                "presence_mapped",
                "decoded_json_lists",
                "undecodable_list_values",
                "presence_without_value",
                "not_applicable_placeholders_nulled",
                "unparseable_gt_fields",
                "rejected_lines",
                "superseded",
                "not_imported",
                "merged_duplicates",
                "decisions_by_reviewer",
                "label_counts",
            )
        }
        md += [
            f"### {snap.snapshot_id}",
            "",
            "```json",
            json.dumps(_counted(keep), indent=1, sort_keys=True, ensure_ascii=False),
            "```",
            "",
        ]

    # --- cross-check
    if crosscheck is not None:
        md += [
            "## Cross-check against the sandbox's own merge",
            "",
            f"Method: {crosscheck['method']}.",
            "",
            f"Documents compared: {crosscheck['documents_compared']}; fields the sandbox applied: "
            f"{json.dumps(crosscheck['sandbox_fields_applied'], sort_keys=True)}.",
            "",
        ]
        md += _table(["Outcome", "Fields"], [[k, v] for k, v in crosscheck["comparison"].items()])
        for kind, examples in crosscheck["examples"].items():
            md += [f"Examples, {kind}:", ""]
            md += _table(
                ["Document", "Class", "Field", "Sandbox", "Ours"],
                [
                    [e["doc"], e["class"], e["field"], _outcome(e["sandbox"]), _outcome(e["ours"])]
                    for e in examples
                ],
            )
        md += ["Effect of the review layer (not part of the sandbox merge):", ""]
        md += _table(["Outcome", "Labels"], [[k, v] for k, v in crosscheck["review_layer_effect"].items()])
        a = crosscheck["audit_vs_sandbox_gold_catalog"]
        md += [
            f"Claude audit decisions against the sandbox gold catalog: {a['audit_decisions']} decisions, "
            f"{a['equal']} equal to the catalog value, {len(a['different'])} different, "
            f"{len(a['not_in_catalog'])} not in the catalog (catalog status of the audited labels: "
            f"{json.dumps(a['catalog_status_of_audited_labels'], sort_keys=True)}).",
            "",
        ]
        md += _table(
            ["Document", "Field", "Audit", "Catalog", "Near-gold overlay"],
            [
                [
                    x["doc"],
                    x["field"],
                    _token(x["audit"]),
                    _token(x["catalog"]),
                    _token(x["near_gold_overlay"]),
                ]
                for x in a["different"]
            ],
        )

    return "\n".join(md), cov, render_markdown(cov)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the label data-quality and coverage reports.")
    parser.add_argument("snapshots", type=Path, nargs="+")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--crosscheck", type=Path)
    parser.add_argument("--case", action="append", default=[], help="<document_id>:<field> to show in full")
    parser.add_argument("--date", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--scrub-crosscheck",
        action="store_true",
        help="rewrite the --crosscheck file in place with every label value replaced by its token",
    )
    args = parser.parse_args(argv)
    if args.scrub_crosscheck and not args.crosscheck:
        parser.error("--scrub-crosscheck needs --crosscheck")
    cases = []
    for c in args.case:
        if ":" not in c:
            parser.error("--case is <document_id>:<field>")
        cases.append(tuple(c.split(":", 1)))
    crosscheck = json.loads(args.crosscheck.read_text(encoding="utf-8")) if args.crosscheck else None
    if crosscheck is not None and args.scrub_crosscheck:
        crosscheck = scrub_crosscheck(crosscheck)
        args.crosscheck.write_text(json.dumps(crosscheck, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    built = build(args.snapshots, args.profile, crosscheck, cases, args.date)
    if built is None:
        print("the snapshot set fails verification: run mailroom-labels verify", file=sys.stderr)
        return 1
    quality_md, cov, cov_md = built
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"data-quality-{args.date}.md").write_text(quality_md, encoding="utf-8")
    (args.out_dir / f"coverage-{args.date}.json").write_text(
        json.dumps(cov, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.out_dir / f"coverage-{args.date}.md").write_text(cov_md, encoding="utf-8")
    print(
        f"wrote {args.out_dir}/data-quality-{args.date}.md, "
        f"coverage-{args.date}.md, coverage-{args.date}.json"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
