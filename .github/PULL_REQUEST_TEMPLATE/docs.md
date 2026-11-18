<!--
Docs PR: a spec, plan, ADR, test strategy, README or agent guidance. An executive summary,
never a transcript: no direct quotes, names, handles, credentials or local paths, and no
claude.ai link, session trailer or tool footer anywhere (.claude/rules/pr-flow.md).
-->

## What this document decides or records

<!-- The decision, plan or rule, in two or three sentences. Which requirement (R1-R10) or stage it serves. -->

## Documents changed

<!-- path - one line on what changed -->

-

## Truth check

<!-- Every path, count, command and version named in the changed text, checked. -->

Commit: `<short sha>`

| Named in the text | Exists / ran | How checked |
|:---|:---|:---|
| `<path or command>` | yes / marked "(after PR #N merges)" / marked unverified | `<command and result>` |

- **Session links:** `bash .circleci/check_no_session_links.sh` -> `<exit code>`; title and body read back after the last edit
- **Layout map:** every new folder has a line in the README map (or "no new folder")

## Decisions left to the owner

<!-- Open questions this document raises, or "none". -->

## Not verified

<!-- Facts you could not confirm and why. "Nothing" is a claim too. -->

## Review log

- Review (`<lens>`, `<model, effort>`): `<findings and fixes>`
- Pre-PR review, fresh context (`<model, effort>`): `<READY / fixes applied>`
- Opus review of Sonnet or Haiku output: `<done on <sha> / not applicable>`

## Risks, rollback and follow-ups

<!-- What could go wrong, how to undo it (usually: revert the squash commit), and any follow-up PR or issue. Short. -->

## Cross-repo and downstream impact

<!-- Does this change what the older repos or their users must do? "None" and why. -->

## Squash-merge message

```text
```

## Checklist

- [ ] Every path, count and command named exists, was run, or is marked unverified or "(after PR #N merges)"
- [ ] Facts repeated on other pages were updated there too
- [ ] Links and anchors resolve
- [ ] No session links or Claude Code footers in commits or this description
- [ ] Squash-merge message drafted above
