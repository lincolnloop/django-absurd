---
name: archive-specs
description:
  Use when cleaning up docs/specs and docs/plans — retiring specs/plans that are
  obsolete or already captured in docs/WHY.md. Records each in docs/HISTORY.md with a
  recoverable git SHA link before deleting it. Run after capture-why.
---

# archive-specs

## Overview

Retire consumed or obsolete specs/plans so they stop polluting context — without losing
anything. Every retired doc is first recorded in `docs/HISTORY.md` with a permanent
`origin/main` blob link, _then_ deleted. Git keeps the full original forever; the ledger
is how anyone finds it again.

This skill never distills or writes `docs/WHY.md` — that is `capture-why`'s job. Run
`capture-why` first so a doc's durable "why" is captured before its file is removed.

## What is retireable

A spec/plan may be retired when it is either:

- **Obsolete** — superseded by a later doc, an abandoned approach, or contradicted by
  the current code; or
- **Fully captured** — its load-bearing "why" already lives in `docs/WHY.md`.

A doc that exists only on the current branch (not yet on `origin/main`) is **not**
retireable: it has no `origin/main` blob to link, so it can't be recovered via the
ledger. Leave it; commit and push first if it should be archived later.

## HISTORY.md entry format

```
<authored-date> — <brief summary> → [view @<sha>](<origin/main blob URL>)
```

- `authored-date` = the `YYYY-MM-DD` prefix on the filename.
- `brief summary` = one line: what the doc covered / which approach it described.
- blob URL =
  `https://github.com/lincolnloop/django-absurd/blob/<sha>/<path-on-origin-main>`, where
  `<sha>` is the current `origin/main` commit and `<path-on-origin-main>` is where the
  doc lives in that commit (it may differ from the working-tree path if recently moved).
  The file MUST exist at that SHA/path.

## Procedure

1. `git fetch origin` so `origin/main` is current.
2. Scan `docs/specs/` and `docs/plans/`. For each doc, decide retireable or keep, with a
   one-line reason. Ground "obsolete" claims against the code, not against other specs.
3. For each retireable doc, resolve its `origin/main` SHA + path and build the blob URL.
   Verify it resolves (e.g.
   `gh api repos/lincolnloop/django-absurd/contents/<path>?ref=<sha>`).
4. **Confirm gate — STOP.** Present the full list (file, authored-date, summary, reason,
   blob URL) and require explicit approval. Delete nothing before the user approves.
5. On approval, for each doc, in this order: append its entry to `docs/HISTORY.md`
   (create the file if missing), **then** `git rm` the doc. Record-link-then-delete.
6. Never delete a doc that isn't first recorded in `docs/HISTORY.md`.

## Red flags — STOP

- About to `git rm` before writing the ledger entry → reverse the order.
- About to archive a doc with no resolvable `origin/main` blob → it's branch-only; skip.
- Deleting without explicit approval at the confirm gate → never.
- Judging "obsolete" from another spec rather than the code → re-check against code.

## Common mistakes

- Deleting then linking → if anything fails between, the link is dead. Always record
  first.
- Linking to the working-tree path when the doc sits at a different path on
  `origin/main`.
- Distilling "why" here → that belongs in `capture-why`; this skill only retires.
