# mailroom-dojo

Workspace for the lean Mailroom rewrite. Each package lives under `packages/`;
the first is `packages/record/` (`mailroom_record`: the append-only, hash-chained
run record and its blob store). Design and build plan: `docs/specs/` and `docs/plans/`.

```bash
python3 -m venv .venv && .venv/bin/pip install -e "packages/record[dev,analysis]"
.venv/bin/python -m pytest packages/record/tests -q
```

Commit messages follow `.gitmessage` (`git config commit.template .gitmessage`).
