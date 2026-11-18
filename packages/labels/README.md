# mailroom-labels

`mailroom_labels`: every label the benchmark scores against, as versioned, hash-checked
snapshots that load with no network; the precedence that turns label layers into one
effective label per document field; the data-quality gate; and the coverage report.
Plan: `docs/plans/2026-11-18-labels-plan.md`. Requirements served: R4, R6, R8, R9.

Install `packages/record` from this workspace first (editable). The labels package
depends on the bare name `mailroom-record`, which pip would otherwise try to resolve on
PyPI, where no such package of ours is published.

```bash
.venv/bin/pip install -e packages/record -e "packages/labels[dev]"
.venv/bin/python -m pytest packages/labels/tests -q
mailroom-labels verify --profile config/clients/<client>/profile.yaml labels/<snapshot>...
mailroom-labels coverage --profile ... --json-out c.json --md-out c.md labels/<snapshot>...
```

`verify` exits 0 with no error and 1 with any error (warnings never fail it); `coverage`
exits 1 and writes nothing when verification finds an error; usage and profile errors
exit 2.

## Modules

| Module | What it does |
| --- | --- |
| `models.py` | Frozen models: `LayerRecord`, `LayerFact`, `CorpusEntry`, `FieldLabel`, `DocumentLabels`, `Finding`, `SnapshotManifest`; the ranks and status words |
| `errors.py` | Typed errors: `ManifestMissing`, `ManifestMismatch` (with `kind`), `UnknownSnapshotSchema`, `CorruptLine` (file and line), `DuplicateSnapshot`, `ProfileError` |
| `profile.py` | `ClientProfile` and `load_profile`: classes, fields, Dojo field types, vocabularies, precedence |
| `snapshot.py` | `write_snapshot`, `build_manifest`, `load_snapshot` (verifies before reading), `load_snapshot_set`, `snapshot_refs`, `label_snapshot_ids`; `LabelSnapshot.rows()` streams the raw native rows |
| `labelset.py` | `load_label_set`, `LabelSet`, `QualityGateFailed`: the set-level surface a scorer uses |
| `layers.py` | Readers for the four native snapshot kinds (`hub`, `interim`, `near_gold`, `review`) and read-time upgrades |
| `resolve.py` | `resolve`: the precedence, staleness and scorability rules |
| `quality.py` | `check`, `check_dirs`, `verify`: typed findings for every data-quality check |
| `findings.py` | Finding kinds and their severities |
| `coverage.py` | Scorable documents and fields per class, by status and rank; JSON and Markdown |
| `cli.py` | `mailroom-labels verify` and `coverage` |

## Layers, ranks and statuses

| Layer | Native shape (`layer_schema`) | Ranks it emits | Status word |
| --- | --- | --- | --- |
| `hub` | `mailroom-labels/hub/v1`: one row per dataset document, with `fields.<f>.{value, presence}` | `hub` (populated), `hub:absent` (documented absence or `not_applicable`) | `hub` |
| `interim` | `mailroom-interim-labels/v1` (sandbox interim overlay) | `interim:correction`, `interim:gap_fill`, `interim:derived`, `interim:disputed` (never truth) | `interim` |
| `near_gold` | `mailroom-interim-labels/v1` (sandbox near-gold overlay, `basis: near_gold:<how>`) | `near_gold` | `near_gold` |
| `review` | `mailroom-labels/review/v1`: one decision per line | `review:gold` (reviewer model `human`), `review:near_gold` (any other reviewer) | `gold`, `near_gold` |

The Hub has two ranks because the sandbox treats them differently: interim `gap_fill` and
`derived` replace a Hub documented absence or `not_applicable` (`apply_overlay` skips only
a populated Hub field), while a near_gold label never replaces any Hub presence code
(`apply_near_gold`). The mailroom precedence is `review:gold`, `interim:correction`, `hub`,
`review:near_gold`, `interim:gap_fill`, `interim:derived`, `hub:absent`, `near_gold`.

The exact field-by-field shapes are in the `layers.py` docstring. The client profile's
`precedence` orders the ranks (highest first); a rank it leaves out is never truth, and
`interim:disputed` can never be listed. A label whose content hash or class disagrees with
the Hub corpus index is stale: it is never truth, and its document is unverified, so the
document has no scorable fields but does not block any other.

## Snapshots

A snapshot is a directory of `.jsonl` files plus `manifest.json`: every file with its
sha256, row count and byte size, the source, the layer schema, the builder, accepted
warnings, and `manifest_hash` (the canonical hash from `mailroom_record.hashing` of the
manifest without that key). Loading refuses a missing, extra, changed, truncated, grown or
reordered file, a wrong row count and a wrong manifest hash, each with its own
`ManifestMismatch.kind`, before any row is read.

## Boundaries

`mailroom_labels` imports `mailroom_record` and no other workspace package
(`tests/test_boundaries.py`). Library code names no class or field: they are profile data.

## Using a label set (what the Dojo codes against)

```python
from mailroom_labels import load_label_set, load_profile

labels = load_label_set(["labels/hub-ccd040f6", "labels/interim-2026-11-01",
                         "labels/near-gold-2026-11-17", "labels/gold-review-2026-11-18"],
                        load_profile("config/clients/mailroom-dataset/profile.yaml"))
labels.snapshot_id          # member ids, sorted, joined with "+"
labels.manifest_hash        # hash_obj of the sorted [snapshot_id, manifest_hash] pairs
for doc in labels.documents():   # resolved DocumentLabels, sorted by document id
    doc.scorable_fields, doc.fields
labels.snapshot_refs()      # [{snapshot_id, kind, manifest_hash}, ...]
labels.label_snapshot_ids() # Run.label_snapshot_ids: ["<snapshot_id>@<manifest_hash>", ...]
```

`load_label_set` verifies every manifest, runs the quality gate and raises
`QualityGateFailed` (with the full report) when it finds an error, so a run can never be
scored against a set that does not pass `mailroom-labels verify`.
