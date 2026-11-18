# mailroom_common

The shared foundation every other package may import: errors, strict YAML, config, hashing,
JSONL IO, ids, logging and test helpers. It is domain-free (no document classes, node names
or clients) and imports nothing else from this workspace, so any package can depend on it.
Why it is a separate leaf package: `docs/adr/0007-shared-leaf-packages.md`.

## Install and test

From the workspace root, in a venv (`python3 -m venv .venv`, or `make install` for every
package):

```bash
.venv/bin/pip install -e "packages/common[dev]"
.venv/bin/python -m pytest packages/common/tests -q
```

## Quick example

```python
from pathlib import Path

from mailroom_common.config import ConfigModel, config_hash, load_config
from mailroom_common.hashing import hash_obj
from mailroom_common.jsonl import append_jsonl, iter_jsonl
from mailroom_common.yamlio import loads_yaml


class Settings(ConfigModel):
    name: str
    retries: int


settings = load_config(Path("settings.yaml"), Settings)   # validated, frozen
config_hash(settings)                                      # store it on the run
hash_obj({"b": 1, "a": 2}) == hash_obj({"a": 2, "b": 1})   # True: canonical JSON
loads_yaml("answer: no")                                   # {'answer': 'no'}, not False
append_jsonl(Path("rows.jsonl"), {"n": 1})
list(iter_jsonl(Path("rows.jsonl")))                       # [{'n': 1}]
```

`settings.yaml` here holds `name: demo` and `retries: 3`.

## Modules

| Module | What it gives you |
|:---|:---|
| `errors` | `MailroomError` and its family (`ConfigError`, `IntegrityError`, `RegistryError`); every deliberate error carries a `code` and a `context` mapping |
| `yamlio` | The one way to read YAML: duplicate keys and non-string keys are refused, `no`/`on`/`off` stay strings |
| `config` | `ConfigModel`, `load_config`, `load_layered` (overlays), `deep_merge`, `config_hash` (stored on a run), optional `${VAR}` interpolation |
| `hashing` | Canonical JSON and SHA-256 (`hash_obj`, `sha256_file`), byte-identical to `mailroom_record.hashing` (enforced by `tests/test_hashing_parity.py` at the workspace root) |
| `jsonl` | Atomic JSONL append and rewrite; a streaming strict or tolerant reader with line numbers |
| `ids`, `clock` | Sortable run ids, safe slugs for path segments, an injectable `Clock` so tests never sleep |
| `logging` | Structured JSON logging with context binding and secret redaction (provisional) |
| `models`, `retry` | Provider-agnostic `ModelClient` interface, `ModelMap`, provider error family, `RetryPolicy` and `call_with_retry` (provisional) |
| `registry` | `Registry[T]` for plug-ins (document classes, nodes, metrics, backends), freezable, all-or-nothing entry-point discovery (provisional) |
| `testing`, `pytest_plugin` | `FakeModelClient`, frozen clock, file writers and log capture as fixtures, loaded automatically through the `pytest11` entry point (no `conftest.py` line needed) |

**Provisional:** `models`, `retry`, `registry` and `logging` are provisional: the engine may
move or rename these when it is built. Nothing outside common imports them yet.
