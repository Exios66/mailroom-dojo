# mailroom-dictionaries

The `mailroom_dictionaries` package: the workspace's controlled vocabularies (document
classes, field types, closed label values, label statuses, review causes, outcomes, decision
slots) as versioned YAML data files with a validating loader, plus the glossary. It imports
only `mailroom_common`, so every other package can read its vocabularies from here.

```bash
.venv/bin/pip install -e "packages/common[dev]" -e "packages/dictionaries[dev]"
.venv/bin/python -m pytest packages/dictionaries/tests -q
```

```python
from mailroom_dictionaries import load_dictionary, parse_field_type, shipped_dictionaries

urgency = load_dictionary("urgency")
urgency.require("Time Sensitive")        # "time-sensitive"; raises VocabularyError if unknown
urgency.normalize("soon")                # None
load_dictionary("label_statuses").rank("gold")   # 1: gold wins over hub, interim, near_gold
parse_field_type("entity_list:name")     # FieldType(base="entity_list", element="name")
shipped_dictionaries().manifest()        # {name: {"version", "hash"}}: pin it on a run
```

## Adding a dictionary

1. Write `src/mailroom_dictionaries/data/<name>.yaml` with `name` (equal to the file name),
   `version: 1`, `description` (say where the values come from), `ordered`, and `entries`
   (`key`, `label`, `description`, optional `aliases`, `deprecated`, `replaced_by`, `rank`,
   `attributes`). Keys match `[a-z][a-z0-9_-]*`. No two keys or aliases may be equal once
   case, spaces, hyphens and underscores are ignored. An ordered dictionary gives every entry
   a `rank`, listed in rank order.
2. Run `python packages/dictionaries/scripts/update_lock.py`, then the tests.

## A client profile's own vocabulary

A client ships its own files (for example a `doc_classes.yaml` with an extra class and the
dictionary for its closed label field) and loads them over the shipped ones, with no code
change:

```python
from mailroom_dictionaries import load_dictionary_file, shipped_dictionaries

client = [load_dictionary_file(p) for p in sorted(profile_dir.glob("*.yaml"))]
vocabularies = shipped_dictionaries().with_overrides(client)   # cross-references checked here
```

## Changing a dictionary

Any content change needs a version bump: raise `version` by one, then run
`python packages/dictionaries/scripts/update_lock.py`. The test suite fails when a
dictionary's content no longer matches `data/dictionaries.lock.json` at the same version, and
the script refuses to rewrite the lock in that case.

## The glossary

Edit `src/mailroom_dictionaries/data/glossary.yaml` (every term names its source), then run
`python packages/dictionaries/scripts/render_glossary.py` to regenerate `docs/glossary.md`.
`--check` fails when the page is stale; a test runs it.
