# Record and Store Implementation Plan (project 1 of 6)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `mailroom_record` package: one append-only, hash-chained run record with a content-addressed blob store, a rebuildable index, seal, verify, trace, pack and prune, plus an importer that turns today's sandbox run folders into records.

**Architecture:** The event log is the only source of truth. Blobs are written before the event that names them; the SQLite index and the Parquet column files are derived from the log and can be rebuilt. Each document has its own hash chain inside a run; a seal commits the chain heads and the document list, and seals chain to each other.

**Tech Stack:** Python 3.11+, pydantic v2, stdlib `sqlite3` (WAL), `pyarrow` (Parquet), `duckdb` (read-only analysis, optional extra `analysis`), `zstandard`, `typer` (CLI), `pytest`. No message broker, no ORM, no async in this package.

**Spec:** `docs/specs/2026-11-18-lean-rewrite-design.md` (snapshot of the Claude Doc "Lean Mailroom Rewrite Design", approved by JJB 2026-11-17 CT). Sections used: "The run record and store", "Systems review" (changes table, failure handling, package boundaries), "Calls" (builder answers). Executors read the spec and this plan.

**Proof data (read-only):** `/mnt/project-files/runs/autoresearch/ar-mailroom-v1-*` (5 folders, 25 documents each) and `ar-mailroom-v2-*` (10 folders, 75 documents each). Each holds `dataset.jsonl`, `items.jsonl`, `nodes.jsonl`, `events.jsonl`, `spec.lock.json`, `prompt.lock.json`, `checkpoint.json`. The slice-100 r2 folder is not available yet.

## Global Constraints

- The spec's "Requirements added 2026-11-18" (R1-R8) bind this plan. For project 1 that means: the record stays domain-free, with no document class, node or client hard-coded (R4); the importer and Task 10b carry floor replay (R7); and tests follow R8 and `docs/test-strategy-record.md` (fault injection, property-based invariants, never-silent failures).

- New workspace, working name `mailroom-core` (the final name and visibility are JJB's open question; do not publish or push until JJB names it). Layout: `packages/record/`, later `packages/labels/`, `packages/dojo/`, `packages/engine/`, `packages/launcher/`.
- `mailroom_record` imports nothing from the other workspace packages. Enforced by a test (Task 1).
- The log is the only truth. Nothing is written to the index or to column files that is not already in the log.
- Write order is fixed: blob, then event, then index.
- The chain holds hashes only. Document content lives in blobs. Deleting a blob leaves a tombstone and verification still passes.
- Every event carries `schema_version`. Stored files are never rewritten; old versions are read through upgrade functions.
- No document text in any exported analysis file (Parquet, reports, CLI output). The repo is public.
- A field the source did not capture is stored as absent (`None`), never guessed.
- No network and no model call in any test. CI is CircleCI only. No linter is required by this plan; if one is added it is `ruff` with correctness rules only.
- No session links or tool footers in commits or PR text. Commit messages follow the house template: `type(scope): summary`, then Why, What changed, Testing, Refs.
- All timestamps are UTC ISO-8601 strings with a `Z` suffix. All hashes are lowercase hex SHA-256.

## Execution notes: which model does what

JJB's standing rule (2026-11-17 CT). It overrides the older "never start an Opus subagent" line in the repos' orchestration rules, which predates it.

| Work | Model and effort |
|---|---|
| Heavy lifting: implementing a task, debugging, root-causing CI, reviews | Opus, medium effort at minimum |
| A tightly scoped, well-specified task (for example Task 2, or a single mechanical edit with its test already written) | Sonnet on high is acceptable |
| Basic actions: fetches, searches, running a command and returning its output | Haiku 5.5 |

- In this plan, Tasks 3 to 12 count as heavy lifting. Tasks 1 and 2 are tightly scoped.
- Every task's work is reviewed by a fresh, independent Opus reviewer on the full diff before the next task starts, and tests are re-run on the final head. Sonnet work is never handed off unreviewed.
- Keep usage lean: one implementer and one reviewer per task, no parallel fan-out.

## Review Focus

1. **A crash leaves a half-written last line in a segment.** Opening the log must ignore and report the torn tail, and the next append must continue the chain from the last complete event. (Task 5)
2. **Importing the same run folder twice.** The second import must be a no-op that reports "already imported", not a duplicate run or a second copy of any blob. (Task 10)
3. **A blob named by an event is missing or tombstoned.** Verify must distinguish the two: a tombstone passes, a missing blob fails and names the event. (Task 7)
4. **Two documents appended in parallel from two threads or processes.** Each document's chain must stay intact and no line may interleave with another. (Task 5)
5. **A stored event with an older `schema_version`, and non-UTF-8 or very large document text.** Old events load through the upgrade path; a blob is bytes and round-trips exactly for 50 MB of arbitrary bytes. (Tasks 3 and 4)

---

## File Structure

```
mailroom-core/
  pyproject.toml                      # workspace root: dev tools, pytest config
  .circleci/config.yml                # one job: install, pytest
  packages/record/
    pyproject.toml                    # package mailroom_record, extras: analysis
    src/mailroom_record/
      __init__.py                     # public names only (see Task 12)
      hashing.py                      # canonical JSON, sha256
      models.py                       # Run, Event, Seal, BlobRef, enums
      upgrade.py                      # schema_version upgrade functions
      blobs.py                        # BlobStore
      log.py                          # EventLog: append, read, torn-tail recovery
      seal.py                         # seal a run or a period, Parquet export
      verify.py                       # verify chains, seals, blobs
      index.py                        # SQLite index, reindex
      versions.py                     # per-document version chain
      trace.py                        # trace run / trace doc
      importer/sandbox_run.py         # sandbox run folder -> record
      importer/audit.py               # what routing inputs a stored run carries
      pack.py                         # pack, unpack, prune
      cli.py                          # `mailroom-record` commands
    tests/                            # one test file per module, plus tests/fixtures/
```

On disk, a store root looks like this:

```
<root>/blobs/<aa>/<bb>/<sha256>.zst        # or <sha256>.tombstone
<root>/runs/<run_id>/run.json               # the Run header (immutable)
<root>/runs/<run_id>/segments/<writer>.jsonl   # later segments: <writer>.<k>.jsonl
<root>/runs/<run_id>/seals/<n>.json
<root>/runs/<run_id>/columns/events-<n>.parquet
<root>/index.sqlite                         # derived
```

---

### Task 1: Workspace scaffold, CI and the boundary test

**Files:**
- Create: `pyproject.toml`, `.circleci/config.yml`, `packages/record/pyproject.toml`, `packages/record/src/mailroom_record/__init__.py`, `packages/record/tests/test_boundaries.py`, `.gitmessage`, `README.md`

**Interfaces:**
- Produces: an installable `mailroom_record` (`pip install -e "packages/record[dev,analysis]"`), and `pytest packages/record/tests -q` as the test command every later task uses.

- [ ] **Step 1: Write the failing test** in `test_boundaries.py`

```python
FORBIDDEN = ("mailroom_dojo", "mailroom_engine", "mailroom_launcher", "mailroom_labels")

def test_record_imports_no_sibling_package():
    offenders = imports_in_tree(Path(mailroom_record.__file__).parent)  # ast walk
    assert not [m for m in offenders if m.split(".")[0] in FORBIDDEN]

def test_package_version_is_set():
    assert mailroom_record.__version__ == "0.1.0"
```

- [ ] **Step 2:** Run `pytest packages/record/tests/test_boundaries.py -q`. Expected: fails with `ModuleNotFoundError: mailroom_record`.
- [ ] **Step 3:** Create the two `pyproject.toml` files (Python `>=3.11`; deps `pydantic>=2`, `zstandard`, `pyarrow`, `typer`; extra `analysis = ["duckdb"]`; extra `dev = ["pytest", "pytest-xdist"]`), the package with `__version__ = "0.1.0"`, and a CircleCI config with one `test` job that installs and runs pytest.
- [ ] **Step 4:** Run the test. Expected: 2 passed.
- [ ] **Step 5:** Commit: `chore(workspace): scaffold mailroom_record with CI and boundary test`.

### Task 2: Canonical hashing

**Files:** Create `hashing.py`, `tests/test_hashing.py`.

**Interfaces:**
- Produces: `canonical_json(obj: Any) -> bytes` (UTF-8, sorted keys, separators `(",", ":")`, `ensure_ascii=False`, floats via `repr`, rejects NaN and infinity with `ValueError`); `sha256_hex(data: bytes) -> str`; `hash_obj(obj: Any) -> str`.

- [ ] **Step 1: Failing tests**

```python
def test_key_order_does_not_change_hash():
    assert hash_obj({"a": 1, "b": 2}) == hash_obj({"b": 2, "a": 1})

def test_known_vector():
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert canonical_json({"b": [1, "é"], "a": None}) == '{"a":null,"b":[1,"é"]}'.encode()

def test_nan_is_rejected():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
```

- [ ] **Step 2:** Run; expect failure on import.
- [ ] **Step 3:** Implement the three functions.
- [ ] **Step 4:** Run; expect 3 passed.
- [ ] **Step 5:** Commit: `feat(record): canonical JSON hashing`.

### Task 3: Models and schema upgrades

**Files:** Create `models.py`, `upgrade.py`, `tests/test_models.py`.

**Interfaces:**
- Consumes: `hash_obj`.
- Produces (pydantic models, all frozen):
  - `RunKind = Literal["specialist_only", "full_pipeline", "service_period"]`
  - `EventKind = Literal["handoff", "model_call", "decision", "output", "review", "error", "serving"]`
  - `Run(schema_version: int = 1, run_id: str, kind: RunKind, backend: str, origin: Literal["native", "imported_sandbox"], policy_name: str | None, policy_hash: str | None, prompt_set: str | None, prompt_hashes: dict[str, str], agent_models: dict[str, str], code_version: str | None, label_snapshot_ids: list[str], document_ids: list[str], caps: dict[str, float], signed_by: str | None, created_at: str, source: dict[str, str])`
  - `BlobRef(sha256: str, role: str, size: int)` where role is one of `original`, `transcript`, `window`, `extraction`, `verdict`, `archived_record`, `human_correction`, `report`.
  - `Event(schema_version: int = 1, run_id: str, doc_id: str, seq: int, ts: str, node: str, agent: str | None, attempt: int, kind: EventKind, model: str | None, prompt_id: str | None, tokens_in: int | None, tokens_out: int | None, tokens_cached: int | None, price_basis: str | None, latency_ms: float | None, decision: dict | list | None, cause: str | None, inputs: list[BlobRef], outputs: list[BlobRef], payload: dict, prev_hash: str, hash: str)`
  - `Seal(schema_version: int = 1, run_id: str, n: int, sealed_at: str, doc_heads: dict[str, str], document_ids: list[str], event_count: int, prev_seal_hash: str, hash: str)`
  - `GENESIS = "0" * 64`
  - `event_hash(fields_without_hash: dict) -> str` = `hash_obj` of every field except `hash`.
  - `upgrade_event(raw: dict) -> dict` and `upgrade_run(raw: dict) -> dict` in `upgrade.py`: return a dict at the current version; raise `UnknownSchemaVersion` for a version newer than the code.
- `doc_id` is the document's content SHA-256. The run-level pseudo-document for run events is the literal `"_run"`.
- `decision` is a JSON object or a JSON list (Task 10 stores a node row's `decisions` list as given); both are normalised and canonical-checked like `payload`.
- `payload` may hold only JSON scalars, lists and dicts of our own tokens. It must never hold document text or a model's free text (those go in blobs).

- [ ] **Step 1: Failing tests**

```python
def test_event_hash_covers_every_field_but_hash():
    e = make_event(seq=0, prev_hash=GENESIS)
    assert e.hash == event_hash(e.model_dump(exclude={"hash"}))
    assert make_event(seq=0, prev_hash=GENESIS, cause="x").hash != e.hash

def test_newer_schema_version_is_refused():
    with pytest.raises(UnknownSchemaVersion):
        upgrade_event({"schema_version": 99})

def test_version_0_event_upgrades():      # Review Focus 5
    raw = v1_event_dict() | {"schema_version": 0}
    raw.pop("tokens_cached")               # the field v0 lacked
    assert upgrade_event(raw)["schema_version"] == 1
    assert upgrade_event(raw)["tokens_cached"] is None

def test_absent_fields_stay_none():
    assert make_event(seq=0, prev_hash=GENESIS, model=None).model is None
```

- [ ] **Step 2:** Run; expect import failure.
- [ ] **Step 3:** Implement models and `upgrade.py` (a dict of `{from_version: function}` applied in order; version 0 exists only to prove the path).
- [ ] **Step 4:** Run; expect 4 passed.
- [ ] **Step 5:** Commit: `feat(record): run, event and seal models with schema upgrades`.

### Task 4: Blob store

**Files:** Create `blobs.py`, `tests/test_blobs.py`.

**Interfaces:**
- Produces: `class BlobStore(root: Path)` with
  - `put(data: bytes, role: str) -> BlobRef` (idempotent; writes to a temp file, fsyncs, then renames)
  - `get(sha256: str) -> bytes` (raises `BlobMissing` or `BlobTombstoned`)
  - `exists(sha256: str) -> Literal["present", "tombstoned", "missing"]`
  - `tombstone(sha256: str, reason: str) -> None` (removes content, writes `<sha>.tombstone` holding reason and time)
  - `size_on_disk() -> int`

- [ ] **Step 1: Failing tests**

```python
def test_same_bytes_stored_once(tmp_path):
    s = BlobStore(tmp_path)
    a, b = s.put(b"x" * 1000, "original"), s.put(b"x" * 1000, "original")
    assert a.sha256 == b.sha256
    assert len(list(tmp_path.rglob("*.zst"))) == 1

def test_roundtrip_arbitrary_bytes(tmp_path):                 # Review Focus 5
    data = os.urandom(1024) + b"\xff\xfe" + bytes(50 * 1024 * 1024)
    s = BlobStore(tmp_path)
    assert s.get(s.put(data, "original").sha256) == data

def test_tombstone_keeps_identity_drops_content(tmp_path):
    s = BlobStore(tmp_path); ref = s.put(b"secret", "original")
    s.tombstone(ref.sha256, "client deletion request")
    assert s.exists(ref.sha256) == "tombstoned"
    with pytest.raises(BlobTombstoned):
        s.get(ref.sha256)

def test_missing_is_not_tombstoned(tmp_path):
    assert BlobStore(tmp_path).exists("0" * 64) == "missing"
```

- [ ] **Step 2:** Run; expect import failure.
- [ ] **Step 3:** Implement with `zstandard` level 9 and the path layout from File Structure.
- [ ] **Step 4:** Run; expect 4 passed.
- [ ] **Step 5:** Commit: `feat(record): content-addressed blob store with tombstones`.

### Task 5: Event log

**Files:** Create `log.py`, `tests/test_log.py`.

**Interfaces:**
- Consumes: `Run`, `Event`, `BlobRef`, `BlobStore`, `event_hash`, `GENESIS`, `upgrade_event`.
- Produces: `class EventLog(root: Path, blobs: BlobStore)` with
  - `create_run(run: Run) -> None` (writes `run.json` once; raises `RunExists`)
  - `open_writer(run_id: str, writer_id: str, flush_every: int = 16) -> LogWriter`
  - `LogWriter.append(*, doc_id, node, kind, agent=None, attempt=0, model=None, prompt_id=None, tokens_in=None, tokens_out=None, tokens_cached=None, price_basis=None, latency_ms=None, decision=None, cause=None, inputs=(), outputs=(), payload=None, ts=None) -> Event`. It sets `seq`, `prev_hash` and `hash` itself. It raises `BlobNotStored` if any ref in `inputs` or `outputs` is not `present` in the blob store (this enforces blob-before-event).
  - `LogWriter.flush() -> None` (write and fsync), `LogWriter.close() -> None`
  - `read_run(run_id: str) -> Run`
  - `iter_events(run_id: str, doc_id: str | None = None) -> Iterator[Event]` (merged across segments, ordered by `(doc_id, seq)`)
  - `doc_heads(run_id: str) -> dict[str, str]`
  - `torn_tails(run_id: str) -> list[TornTail]` where `TornTail(segment: str, byte_offset: int)`
- Decided while building (2026-11-18): a writer id is path-safe (`[A-Za-z0-9][A-Za-z0-9_-]{0,99}`) and holds an exclusive `flock` on its segment (`WriterInUse` if it is live elsewhere). If the writer's latest segment ends in a torn line, it starts `<writer>.<k>.jsonl` rather than appending after the torn bytes. A writer takes a document on its first append, reading that document's head from disk then, and holds it until `close()`; another live writer in the same process gets `DocumentInUse` (not enforced across processes). Readers see flushed events only. A whole line that is not a JSON object raises `CorruptLine`; `iter_stored(run_id)` yields the raw stored dicts (`StoredLine`) for Task 7 to hash before upgrading.
- One document is owned by one writer at a time. `seq` starts at 0 per document per run. A segment is one JSON object per line. Appends within a writer are serialized by a lock; each writer has its own segment file, so processes never share a file.

- [ ] **Step 1: Failing tests**

```python
def test_chain_links_per_document(store):
    w = store.log.open_writer("r1", "w0")
    a = w.append(doc_id="d1", node="classify", kind="decision")
    b = w.append(doc_id="d1", node="extract", kind="output")
    c = w.append(doc_id="d2", node="classify", kind="decision")
    assert (a.seq, b.seq, c.seq) == (0, 1, 0)
    assert a.prev_hash == GENESIS and b.prev_hash == a.hash and c.prev_hash == GENESIS

def test_event_before_blob_is_refused(store):
    ghost = BlobRef(sha256="1" * 64, role="extraction", size=3)
    with pytest.raises(BlobNotStored):
        store.log.open_writer("r1", "w0").append(doc_id="d1", node="extract", kind="output", outputs=[ghost])

def test_torn_tail_is_reported_and_chain_continues(store):   # Review Focus 1
    w = store.log.open_writer("r1", "w0"); a = w.append(doc_id="d1", node="n", kind="decision"); w.close()
    seg = next((store.root / "runs/r1/segments").iterdir())
    with seg.open("ab") as f: f.write(b'{"run_id": "r1", "doc_')        # crash mid-line
    assert len(store.log.torn_tails("r1")) == 1
    assert [e.hash for e in store.log.iter_events("r1")] == [a.hash]
    w2 = store.log.open_writer("r1", "w1")
    assert w2.append(doc_id="d1", node="n2", kind="decision").prev_hash == a.hash

def test_parallel_documents_keep_their_chains(store):        # Review Focus 4
    # 8 threads, each its own writer and its own 4 documents, 50 events per document
    run_parallel_appends(store, writers=8, docs_per_writer=4, events_per_doc=50)
    for doc_id in all_doc_ids:
        events = list(store.log.iter_events("r1", doc_id))
        assert [e.seq for e in events] == list(range(50))
        assert all(e.prev_hash == p.hash for p, e in zip(events, events[1:]))

def test_run_header_is_write_once(store):
    with pytest.raises(RunExists):
        store.log.create_run(store.run)
```

`store` is a fixture in `tests/conftest.py` that builds a `BlobStore`, an `EventLog`, and a created `Run` with id `r1` under `tmp_path`.

- [ ] **Step 2:** Run; expect import failure.
- [ ] **Step 3:** Implement. On `open_writer`, compute each document's current head by reading existing segments, skipping any final line that does not parse.
- [ ] **Step 4:** Run; expect 5 passed.
- [ ] **Step 5:** Commit: `feat(record): append-only event log with per-document hash chains`.

### Task 6: Seal and column export

**Files:** Create `seal.py`, `tests/test_seal.py`.

**Interfaces:**
- Consumes: `EventLog`, `Seal`, `hash_obj`.
- Produces:
  - `seal_run(log: EventLog, run_id: str, *, sealed_at: str | None = None) -> Seal`. Writes `seals/<n>.json` and `columns/events-<n>.parquet` holding the events since the previous seal. `n` starts at 0. `prev_seal_hash` is `GENESIS` for the first seal. `doc_heads` and `document_ids` cover every document with at least one event up to this seal.
  - `list_seals(log, run_id) -> list[Seal]`
  - `EVENT_COLUMNS: tuple[str, ...]`: every `Event` field except `payload`; `decision` is stored as a JSON string; `inputs` and `outputs` as lists of `sha256`.
- A service-period run is sealed repeatedly (the engine will call this on a timer). An experiment run is sealed once when it ends. Sealing with no new events returns the previous seal unchanged.

- [ ] **Step 1: Failing tests**

```python
def test_seal_commits_heads_and_document_list(store_with_events):
    seal = seal_run(store.log, "r1")
    assert seal.n == 0 and seal.prev_seal_hash == GENESIS
    assert seal.doc_heads == store.log.doc_heads("r1")
    assert sorted(seal.document_ids) == ["d1", "d2"]

def test_second_seal_chains_and_exports_only_new_events(store_with_events):
    s0 = seal_run(store.log, "r1"); append_more(store, doc_id="d3", n=3); s1 = seal_run(store.log, "r1")
    assert s1.prev_seal_hash == s0.hash and s1.n == 1
    assert pq.read_table(store.root / "runs/r1/columns/events-1.parquet").num_rows == 3

def test_parquet_has_no_payload_column(store_with_events):
    seal_run(store.log, "r1")
    assert "payload" not in pq.read_schema(store.root / "runs/r1/columns/events-0.parquet").names

def test_seal_without_new_events_is_a_no_op(store_with_events):
    assert seal_run(store.log, "r1").hash == seal_run(store.log, "r1").hash
```

- [ ] **Step 2:** Run; expect import failure.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; expect 4 passed.
- [ ] **Step 5:** Commit: `feat(record): chained seals and Parquet export`.

### Task 7: Verify

**Files:** Create `verify.py`, `tests/test_verify.py`.

**Interfaces:**
- Consumes: `EventLog`, `BlobStore`, `list_seals`, `event_hash`.
- Produces: `verify_run(log: EventLog, blobs: BlobStore, run_id: str) -> VerifyReport` with `VerifyReport(ok: bool, problems: list[Problem], events_checked: int, documents_checked: int, tombstoned_blobs: int)` and `Problem(kind: Literal["bad_hash", "broken_link", "seq_gap", "missing_blob", "seal_mismatch", "document_missing_from_log", "torn_tail", "truncated_after_seal"], doc_id: str | None, seq: int | None, detail: str)`. `torn_tail` is reported but does not set `ok` to false. Every other kind does.
- Decided while building (2026-11-18): verify never raises on damaged data (only an unknown or malformed run id raises). `Problem` also carries `segment`, `byte_offset`, `seal` and `fails`; `VerifyReport` also carries `seals_checked` and `closed`. Kinds added: `corrupt_line`, `invalid_event`, `forked_chain`, `corrupt_blob`, `tombstone_with_content`, `seal_corrupt`, `seal_missing`, `columns_missing`, `columns_mismatch`, `corrupt_run_header`. `torn_tail` fails only on a closed run (an experiment-kind run with a seal); on a live or `service_period` run it is benign. Segments are read through `EventLog.iter_raw` (streamed, unparsed), added to `log.py` for this task.

- [x] **Step 1: Failing tests** (each tampers with a sealed fixture run, then asserts the exact problem kind)

```python
def test_clean_run_verifies(sealed_store):
    r = verify_run(*sealed_store.parts, "r1"); assert r.ok and r.problems == []

def test_edited_event_is_bad_hash(sealed_store):
    rewrite_line(sealed_store, doc_id="d1", seq=1, cause="tampered")
    assert kinds(verify(sealed_store)) == {"bad_hash", "broken_link"}

def test_deleted_middle_event_is_seq_gap(sealed_store):
    delete_line(sealed_store, doc_id="d1", seq=1)
    assert "seq_gap" in kinds(verify(sealed_store))

def test_deleted_whole_document_is_caught_by_seal(sealed_store):
    delete_document(sealed_store, "d2")
    assert "document_missing_from_log" in kinds(verify(sealed_store))

def test_cut_tail_is_caught_by_seal(sealed_store):
    delete_line(sealed_store, doc_id="d1", seq=last_seq(sealed_store, "d1"))
    assert "truncated_after_seal" in kinds(verify(sealed_store))

def test_tombstoned_blob_passes_missing_blob_fails(sealed_store):   # Review Focus 3
    a, b = two_output_blobs(sealed_store)
    sealed_store.blobs.tombstone(a, "deletion request"); remove_blob_file(sealed_store, b)
    r = verify(sealed_store)
    assert r.tombstoned_blobs == 1
    assert [p.kind for p in r.problems] == ["missing_blob"]
```

- [x] **Step 2:** Run; expect import failure.
- [x] **Step 3:** Implement.
- [x] **Step 4:** Run; expect 6 passed.
- [x] **Step 5:** Commit: `feat(record): verify chains, seals and blobs`.

### Task 8: Derived index

**Files:** Create `index.py`, `tests/test_index.py`.

**Interfaces:**
- Consumes: `EventLog`, `list_seals`.
- Produces: `class Index(path: Path)` with
  - `reindex(log: EventLog) -> IndexStats` (drops and rebuilds everything from the log)
  - `apply(event: Event) -> None` (incremental; the caller invokes it only after `LogWriter.flush()`)
  - `runs() -> list[RunRow]`, `documents(run_id: str) -> list[DocRow]`, `runs_for_document(doc_id: str) -> list[RunRow]`
  - `DocRow(run_id, doc_id, events, last_node, last_kind, head_hash, in_review: bool)`
- Tables: `runs`, `documents`, `doc_runs`, `chain_heads`, `review_queue`. SQLite in WAL mode, `synchronous=NORMAL`. No table stores document text or payloads.
- Decided while building (2026-11-18): `reindex` is the authority and rebuilds in one write transaction (WAL readers see the old index until commit; a crash leaves the old index, or an empty `missing` one); the file is never renamed into place. A `meta` table holds `schema_version` and `state`; a missing, empty, damaged (`quick_check`), old-version or unfinished file is never trusted: queries and `apply` raise `IndexUnavailable(status)`, and `reindex`/`ensure(log)` rebuild it (an unreadable file is deleted and recreated). `apply` takes exactly the next link of each chain: repeats and covered events are no-ops; a gap, a fork at the head and a broken link raise `ApplyRefused` and change nothing. A run must be registered first: `add_run(run)` after `create_run` (or a rebuild); `apply_seal(seal)` follows the seal chain the same way. In review means the document's head event has `kind == "review"` (no node names, decisions or payloads are read). A rebuild never aborts on a damaged log: corrupt and invalid lines, gaps, forks, broken links, unreadable seals and run headers become `problems` rows (`IndexStats.problems`), and a document stops at its last good event; a scan with a gap is re-read until two reads agree. Also added: `RunRow(run_id, kind, origin, backend, created_at, documents, events, in_review, seals)`, `review_queue()`, `problems()`, `dump()`, `drift(log)`, `status()`, `iter_events(log, run_id, doc_id)` (reads the byte ranges the rebuild recorded in `segments`/`doc_spans` plus every unindexed tail, falling back to a full scan when they no longer match). The event log's readers now stream (`EventLog.scan`, `iter_located`), keeping the `(doc_id, seq)` order with two passes.

- [x] **Step 1: Failing tests**

```python
def test_reindex_equals_incremental(store_with_events, tmp_path):
    inc = Index(tmp_path / "a.sqlite"); inc.add_run(run)   # decided while building: a run is registered first
    [inc.apply(e) for e in store.log.iter_events("r1")]
    full = Index(tmp_path / "b.sqlite"); full.reindex(store.log)
    assert dump(inc) == dump(full)

def test_index_is_disposable(store_with_events, tmp_path):
    p = tmp_path / "i.sqlite"; Index(p).reindex(store.log); before = dump(Index(p)); p.unlink()
    Index(p).reindex(store.log); assert dump(Index(p)) == before

def test_document_seen_in_two_runs(two_run_store, tmp_path):
    idx = Index(tmp_path / "i.sqlite"); idx.reindex(two_run_store.log)
    assert {r.run_id for r in idx.runs_for_document("d1")} == {"r1", "r2"}

def test_review_queue_tracks_last_event_kind(store_with_events, tmp_path):
    append(store, doc_id="d1", node="human_review", kind="review", decision={"state": "paused"})
    idx = Index(tmp_path / "i.sqlite"); idx.reindex(store.log)
    assert [d.doc_id for d in idx.documents("r1") if d.in_review] == ["d1"]
```

- [x] **Step 2:** Run; expect import failure.
- [x] **Step 3:** Implement with stdlib `sqlite3`.
- [x] **Step 4:** Run; expect 4 passed.
- [x] **Step 5:** Commit: `feat(record): rebuildable SQLite index`.

### Task 9: Version chain, trace and the CLI

**Files:** Create `versions.py`, `trace.py`, `cli.py`, `tests/test_trace.py`.

**Interfaces:**
- Consumes: `EventLog`, `Index`, `BlobStore`, `verify_run`, `seal_run`.
- Produces:
  - `version_chain(log, run_id, doc_id) -> list[Version]` with `Version(order: int, role: str, sha256: str, size: int, node: str, attempt: int, ts: str, state: Literal["present", "tombstoned", "missing"])`. Order follows event order; within an event, inputs before outputs; a blob appears once, at its first use.
  - `trace_run(log, index, run_id) -> RunTrace` (per document: nodes visited in order, attempts, last decision, cause, in review or not)
  - `trace_doc(log, index, doc_id) -> DocTrace` (every run that touched the document, with its version chain per run)
  - CLI `mailroom-record` with commands `verify <run>`, `seal <run>`, `reindex`, `trace run <run>`, `trace doc <doc>`, `versions <run> <doc>`. All take `--root`. Exit code 1 when verify is not ok. Output prints hashes, roles, nodes and counts, and never blob content.

- [ ] **Step 1: Failing tests**

```python
def test_version_chain_orders_original_to_correction(review_store):
    roles = [v.role for v in version_chain(review_store.log, "r1", "d1")]
    assert roles == ["original", "transcript", "extraction", "extraction", "archived_record", "human_correction"]

def test_trace_doc_lists_every_run(two_run_store, tmp_path):
    t = trace_doc(two_run_store.log, built_index(two_run_store, tmp_path), "d1")
    assert [r.run_id for r in t.runs] == ["r1", "r2"]

def test_cli_verify_exit_codes(sealed_store):
    assert run_cli("verify", "r1", root=sealed_store.root).exit_code == 0
    rewrite_line(sealed_store, doc_id="d1", seq=0, cause="x")
    assert run_cli("verify", "r1", root=sealed_store.root).exit_code == 1

def test_cli_never_prints_blob_content(review_store):
    out = run_cli("trace", "doc", "d1", root=review_store.root).stdout
    assert "SECRET-DOCUMENT-TEXT" not in out          # the fixture's original blob
```

- [ ] **Step 2:** Run; expect import failure.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; expect 4 passed.
- [ ] **Step 5:** Commit: `feat(record): version chain, trace and CLI`.

### Task 10: Sandbox run importer and routing-input audit

**Files:** Create `importer/sandbox_run.py`, `importer/audit.py`, `tests/test_importer.py`, `tests/fixtures/sandbox_run_min/` (a hand-made 3-document folder with synthetic text, in the real format below).

**The format being read** (observed in `ar-mailroom-v1-r00-baseline`):
- `dataset.jsonl` rows: `id` (`DOC-` plus 16 hex), `filename`, `doc_text`, `expected_doc_class`, `expected_subclass`, `expected_stage`, `expected_fields` (dict), `content_sha256`, `split`, `source_revision`.
- `items.jsonl` rows: `item_id`, `ok`, `error`, `pred` (`doc_type`, `stage`, `extracted_data`, `classification_confidence`, `extraction_confidence`, `review_causes`, `review_verdict`, `escalation_reason`, `reviewer_doc_type`, `reviewer_confidence`, `error`, `error_message`, `offline_fallback`, `mailroom_src`), `score` (dict), `latency_ms`, `prompt_tokens`, `completion_tokens`, `usage_by_agent` (per agent: `calls`, `prompt_tokens`, `completion_tokens`, `models`), `ts`.
- `nodes.jsonl` rows: `doc`, `node`, `status` (`running`, `done`, `paused`), `agent`, `model`, `ts` (epoch float), and on `done` or `paused` rows `ms`, `stage`, `decisions` (list of `{question, type, answer, p, expected, options, source}`).
- `events.jsonl` rows: `ts`, `event` (`preflight_ok`, `spend_authorized`, `start`, `done`, ...), `level`, `detail`.
- `spec.lock.json` keys include `run_id`, `spec_hash`, `agent_models`, `agent_routes`, `agent_knobs`, `confidence`, `effective_confidence`, `dependency_sources`, `engine`, `git`, `job`, `prompt`, `prompt_text_sha`, `versions`. `prompt.lock.json` has `agents.<name>.sha256`.

**Interfaces:**
- Consumes: `EventLog`, `BlobStore`, `Run`, `seal_run`.
- Produces:
  - `import_sandbox_run(folder: Path, log: EventLog, blobs: BlobStore) -> ImportResult` with `ImportResult(run_id: str, status: Literal["imported", "already_imported"], documents: int, events: int, absent_fields: list[str])`.
  - `audit_sandbox_run(folder: Path) -> RoutingAudit` with `RoutingAudit(run_id, documents, nodes_seen: dict[str, int], decisions_seen: dict[str, int], present: list[str], absent: list[str])`.
  - CLI: `mailroom-record import sandbox-run <folder>` and `mailroom-record audit sandbox-run <folder>`.
- Mapping rules:
  - `Run`: `origin="imported_sandbox"`, `kind="full_pipeline"` when `nodes.jsonl` has a `classify` node, else `"specialist_only"`; `backend` from `spec.lock.json` `engine`; `agent_models`, `prompt_hashes` (from `prompt.lock.json`), `code_version` (from `git`), `signed_by` (from `spend-authorization.json` if present, else `None`); `source = {"folder": <name>, "spec_hash": ...}`. `policy_name="legacy"`, `policy_hash=None`.
  - `doc_id` is `content_sha256`. `doc_text` becomes an `original`-role blob. `expected_*` fields are NOT imported into the record (labels belong to the labels package); only `expected_doc_class` is kept in the first event's `payload` as `draw_class`.
  - Each `done`, `paused` or `failed` node row becomes one event, its kind decided in this order: `kind="review"` for `human_review`, `kind="error"` for `failed`, `kind="decision"` when `decisions` is non-empty, else `kind="handoff"` (so a paused `human_review` row with decisions is still a review, and Task 8's review queue sees it). `decision` holds the row's `decisions` list as given, minus any `options` text. `latency_ms` from `ms`. `ts` converted to ISO (the finish time).
  - Floor replay (added 2026-11-18, see the spec's "Requirements added 2026-11-18", R7): the matching `running` row is not a separate event, but its time is kept on the finishing event as `payload.started_ts` (ISO). The finishing event's `payload` also keeps `status`, `step` (when present) and `stage` (when present). A `running` row with no finishing row (in flight when the run stopped) becomes one event with `kind="handoff"` and `payload.status="running"`. Pairing is by `(doc, node, step)` in file order, the same rule the sandbox's `tui/floor_replay.py:build_timeline` uses.
  - `checkpoint.json` (`state`, `cursor`, `total`) becomes a final `_run` event with `node="checkpoint"`, `kind="handoff"` and those three values in `payload`; absent when the file is missing.
  - The item's `pred.extracted_data` (without `_report`) becomes an `extraction` blob and `_report` a `report` blob, attached to one final `kind="output"` event per document whose `decision` holds `doc_type`, `stage`, both confidences, `review_causes`, `review_verdict`, `escalation_reason`. Its `payload` holds `usage_by_agent` and the sandbox `score` dict under the key `legacy_score`.
  - `events.jsonl` rows become `_run` events with `kind="handoff"`, `node=<event name>`.
  - Per-call tokens, per-attempt extractions and prompt ids per call do not exist in the source. They are listed in `absent_fields` and left `None`.
  - The run is sealed at the end of the import.
- `audit_sandbox_run` lists, as `present` or `absent`, each routing input the engine's `legacy` replay will need: `classification_confidence`, `extraction_confidence`, `judge verdict per pass`, `arbiter route per pass`, `sorter reviewer verdict`, `retry counts`, `effective thresholds`, `decision slot outputs`, `boss approval`.

- [ ] **Step 1: Failing tests**

```python
def test_import_fixture_folder(tmp_store, fixture_folder):
    r = import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    assert (r.status, r.documents) == ("imported", 3)
    assert verify_run(tmp_store.log, tmp_store.blobs, r.run_id).ok
    assert "tokens_in per call" in r.absent_fields

def test_second_import_is_a_no_op(tmp_store, fixture_folder):          # Review Focus 2
    import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    blobs_before = tmp_store.blobs.size_on_disk()
    assert import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs).status == "already_imported"
    assert tmp_store.blobs.size_on_disk() == blobs_before

def test_same_document_in_two_runs_is_one_blob(tmp_store, fixture_folder, fixture_folder_replicate):
    import_sandbox_run(fixture_folder, ...); import_sandbox_run(fixture_folder_replicate, ...)
    assert count_blobs(tmp_store, role="original") == 3

def test_node_decisions_survive(tmp_store, fixture_folder):
    import_sandbox_run(fixture_folder, ...)
    ev = [e for e in tmp_store.log.iter_events(RUN, DOC1) if e.node == "judge_verify"]
    assert ev[0].decision[0]["question"] == "extraction_verdict" and ev[0].decision[0]["answer"] == "partial"

def test_labels_are_not_imported(tmp_store, fixture_folder):
    import_sandbox_run(fixture_folder, ...)
    assert "expected_fields" not in json.dumps([e.model_dump() for e in tmp_store.log.iter_events(RUN)])

def test_missing_optional_files_are_absent_not_errors(tmp_store, fixture_folder_no_prompt_lock):
    r = import_sandbox_run(fixture_folder_no_prompt_lock, ...)
    assert r.status == "imported" and "prompt_hashes" in r.absent_fields

def test_audit_reports_routing_inputs(fixture_folder):
    a = audit_sandbox_run(fixture_folder)
    assert "extraction_confidence" in a.present and "decision slot outputs" in a.absent
```

- [ ] **Step 2:** Run; expect import failure.
- [ ] **Step 3:** Build the fixture folders (synthetic text only, no real document), then implement.
- [ ] **Step 4:** Run; expect 7 passed.
- [ ] **Step 5:** Commit: `feat(record): import sandbox run folders and audit routing inputs`.

### Task 10b: Floor replay timeline from the record

Added 2026-11-18 (JJB: all floor replayability moves into this repo). The TUI and floor viewer themselves move in the launcher project; this task makes every stored or imported run replayable from the record alone.

**Files:** Create `replay.py`, `tests/test_replay.py`.

**Interfaces:**
- Consumes: `EventLog`.
- Produces: `replay_timeline(log, run_id) -> tuple[list[tuple[float, dict]], dict]`, the same contract as the sandbox's `tui/floor_replay.py:build_timeline`: a sorted list of `(at_seconds, event)` where `event` is `{"node": {...}}`, `{"item": {...}}` or `{"checkpoint": {...}}`, plus a `meta` dict (`source_run_id`, `engine_kind`, `engine_model`, `node_records`, `item_records`, `duration_s`, `synthetic_timing`, `incomplete`, `source_state`, `warnings`). Node rows carry `doc`, `node`, `status`, `agent`, `model`, `step`, `stage`, `ms`, `decisions` (without `options` text); item rows carry the outcome fields from the final `output` event (no extracted values, no document text). A `running` start is emitted at `started_ts` and its finish at the event `ts`.
- No document text and no extracted field values in the timeline.

- [ ] **Step 1: Failing tests**

```python
def test_timeline_matches_fixture_folder(tmp_store, fixture_folder):
    r = import_sandbox_run(fixture_folder, tmp_store.log, tmp_store.blobs)
    timeline, meta = replay_timeline(tmp_store.log, r.run_id)
    assert expected_timeline(fixture_folder) == normalise(timeline)   # hand-computed from the fixture's nodes/items/checkpoint
    assert meta["node_records"] == count_node_rows(fixture_folder) and not meta["synthetic_timing"]

def test_in_flight_node_replays_as_running(tmp_store, fixture_folder_crashed): ...
def test_timeline_has_no_document_text(tmp_store, fixture_folder): ...     # sentinel scan
def test_native_run_replays(store_with_events): ...                        # a run written by LogWriter, not imported
```

- [ ] **Step 2:** Run; expect import failure. **Step 3:** Implement. **Step 4:** Run; expect pass.
- [ ] **Step 5:** Commit: `feat(record): floor replay timeline from the record`.

### Task 11: Pack, unpack and prune

**Files:** Create `pack.py`, `tests/test_pack.py`.

**Interfaces:**
- Produces:
  - `pack_run(root: Path, run_id: str, out: Path, *, include_blobs: bool = True) -> PackManifest` (a `.tar.zst` holding the run folder, the blobs it names, and `manifest.json` with every member's SHA-256 and the last seal hash). Refuses with `RunNotSealed` unless the latest seal covers the latest event: a run with no seal, or with flushed events after its latest seal, is refused (seal it first).
  - `unpack_run(archive: Path, root: Path) -> str` (verifies the manifest, then `verify_run`; raises `PackCorrupt`)
  - `prune_run(root: Path, run_id: str, *, keep_columns: bool = True) -> PruneReport` (removes segments and seals of a packed run; deletes a blob only when no other run names it, found by reading the other runs' logs, the authoritative record, never the index, which can lag a crash between flush and apply). It holds the store's exclusive maintenance lock, which writers take shared, so no append can name a blob between that check and the delete. Refuses unless a pack path is recorded for the run, and, like `pack_run`, unless the latest seal covers the latest event.
  - CLI: `pack`, `unpack`, `prune`.

- [ ] **Step 1: Failing tests**

```python
def test_pack_unpack_roundtrip_verifies(sealed_store, tmp_path):
    pack_run(sealed_store.root, "r1", tmp_path / "r1.tar.zst")
    rid = unpack_run(tmp_path / "r1.tar.zst", tmp_path / "other")
    assert verify_run(*open_store(tmp_path / "other"), rid).ok

def test_unsealed_run_cannot_be_packed(store_with_events, tmp_path):
    with pytest.raises(RunNotSealed):
        pack_run(store.root, "r1", tmp_path / "x.tar.zst")

def test_prune_keeps_blobs_shared_with_another_run(two_run_sealed_store, tmp_path):
    pack_run(root, "r1", tmp_path / "r1.tar.zst"); prune_run(root, "r1")
    assert verify_run(*parts, "r2").ok

def test_corrupt_archive_is_refused(sealed_store, tmp_path):
    p = tmp_path / "r1.tar.zst"; pack_run(sealed_store.root, "r1", p); flip_one_byte(p)
    with pytest.raises(PackCorrupt):
        unpack_run(p, tmp_path / "other")
```

- [ ] **Step 2:** Run; expect import failure.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; expect 4 passed.
- [ ] **Step 5:** Commit: `feat(record): pack, unpack and prune`.

### Task 12: Stage gate on the real runs, and the public surface

**Files:** Create `packages/record/tests/test_public_surface.py`, `packages/record/scripts/stage1_gate.py`, `docs/stage1-gate-report.md` (written by the script).

**Interfaces:**
- Consumes: everything above.
- Produces: `mailroom_record.__all__` with exactly these names: `Run`, `Event`, `Seal`, `BlobRef`, `BlobStore`, `EventLog`, `Index`, `seal_run`, `verify_run`, `version_chain`, `trace_run`, `trace_doc`, `import_sandbox_run`, `audit_sandbox_run`, `replay_timeline`, `pack_run`, `unpack_run`, `prune_run`, `GENESIS` (19 names; `replay_timeline` added 2026-11-18). And `stage1_gate.py --runs <dir> --root <dir>`, which imports every run folder under `--runs`, verifies each, reindexes, and writes the report.

- [ ] **Step 1: Failing test**

```python
def test_public_surface_is_exactly_this():
    assert sorted(mailroom_record.__all__) == sorted(EXPECTED_19_NAMES)
```

- [ ] **Step 2:** Run; expect failure. Then set `__all__` and re-run; expect pass.
- [ ] **Step 3:** Write `stage1_gate.py`. The report it writes has one row per run (documents, events, verify ok, absent fields) and these totals: source size on disk against store size on disk; count of `original` blobs against count of document-runs; and the union of `RoutingAudit.absent` across runs. The report holds counts, hashes and run ids only.
- [ ] **Step 4:** Run it on the real data:

```bash
ROOT="$(mktemp -d)"   # one store root, kept for the second run below
python packages/record/scripts/stage1_gate.py --runs /mnt/project-files/runs/autoresearch --root "$ROOT"
```

Expected, and each is a gate condition:
- 15 run folders imported, all with `verify ok`. (The v2 `r07` run errored for lack of credits; it must still import, with its failed documents carried as `kind="error"` events.)
- `original` blobs: 25 for the five v1 runs together and 75 for the ten v2 runs together, assuming v1 and v2 share no document. Report the real number; if it differs, explain it from the draws, do not force it.
- Store size on disk is smaller than the 93 MB source.
- `mailroom-record trace doc <a v1 doc id>` lists all five v1 runs.
- A second run of the script with the same `--root "$ROOT"` reports every run as `already_imported`.
- A 40-character string taken from one imported document's text is in none of the report's bytes and none of the decoded values of any Parquet file (every string and list-of-string cell of `pyarrow.parquet.read_table(...)`; a byte `grep` misses compressed pages). Positive control first: the same scan finds the string in a scratch Parquet file that holds it.
- Floor replay parity: for every run folder with `nodes.jsonl` or `items.jsonl`, `replay_timeline` from the record gives the same node sequence (doc, node, status) and the same times (within 1 ms) as the sandbox's `build_timeline` on the source folder. The script loads `tui/floor_replay.py` by path from a sandbox checkout when one is given (`--sandbox <dir>`; the module imports only the standard library) and reports the check as skipped otherwise.

- [ ] **Step 5:** Run the whole suite: `pytest packages/record/tests -q`. Expected: all pass, no skips. Commit: `feat(record): stage 1 gate on the v1 and v2 runs` with the report's totals in the commit body.

---

## What this plan leaves to later projects

- Reading labels (`expected_*` fields, Hub, interim, near_gold): project 2, the labels package.
- Scoring anything: project 3, the Dojo. The importer keeps the sandbox's own scores under `legacy_score` only so the Dojo can check its numbers against them.
- Postgres for the live index, encryption of blobs at rest, and the periodic seal timer: the engine and deploy projects. This plan keeps `Index` behind one class so a Postgres twin can be added, and `seal_run` callable repeatedly.
- The slice-100 r2 run: import it with the same command when its folder is found.

## Notes from checking the real run folders

- Stored runs carry per-node decisions (class and probability, extraction confidence, judge verdict, arbiter route), so router replay in the engine project has real inputs for those. They do not carry per-call tokens, per-attempt extractions, or decision-slot outputs. Task 10's audit puts numbers on this for all 15 runs before the engine plan is written.
- `dataset.jsonl` holds full document text (about 2.8 MB for 25 documents). That text goes to blobs and nowhere else.
