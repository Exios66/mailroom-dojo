# mailroom_common

The shared foundation every other package may import. It is domain-free (no document classes, node names or clients) and imports nothing else from this workspace. See `docs/adr/0007-shared-leaf-packages.md`.

| Module | What it gives you |
|:---|:---|
| `errors` | `MailroomError` and its family (`ConfigError`, `IntegrityError`, `RegistryError`); every deliberate error carries a `code` and a `context` mapping |
| `yamlio` | the one way to read YAML: duplicate keys and non-string keys are refused, `no`/`on`/`off` stay strings |
| `config` | `ConfigModel`, `load_config`, `load_layered` (overlays), `deep_merge`, `config_hash` (stored on a run), optional `${VAR}` interpolation |
| `hashing` | canonical JSON and SHA-256 (`hash_obj`, `sha256_file`), byte-identical to `mailroom_record.hashing` (a parity test enforces it) |
| `jsonl` | atomic JSONL append and rewrite, streaming strict or tolerant reader with line numbers |
| `ids`, `clock` | sortable run ids, safe slugs for path segments, an injectable `Clock` so tests never sleep |
| `logging` | structured JSON logging with context binding and secret redaction |
| `models`, `retry` | provider-agnostic `ModelClient` interface, `ModelMap`, provider error family, `RetryPolicy` and `call_with_retry` |
| `registry` | `Registry[T]` for plug-ins (document classes, nodes, metrics, backends), freezable, entry-point discovery |
| `testing`, `pytest_plugin` | `FakeModelClient`, frozen clock, file writers and log capture as fixtures: `pytest_plugins = ["mailroom_common.pytest_plugin"]` in a package's `tests/conftest.py` |

Install: `pip install -e "packages/common[dev]"`. Test: `pytest packages/common/tests -q`.
