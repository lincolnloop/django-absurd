# Spec: /dream knowledge distillation (capture-why + archive-specs)

Date: 2026-06-24

## Problem

`docs/specs` + `docs/plans` accumulate. Mix of live, obsolete, and trial-and-error docs.
Pollutes context for humans + LLMs. Worse: spec claims drift from code (audit found a
check documented `W003` while code emits `E005`; two specs still document deleted
`ABSURD_QUEUES` / `ABSURD_DATABASE` settings). Need to preserve the load-bearing "why",
discard noise, keep a recovery path, and never introduce stale info.

## Guiding principle

Avoid staleness at all costs. Current behavior lives in code / AGENTS.md / README and is
never duplicated. Only sanctioned "stale" = explanatory rationale ("tried X, explored Y,
chose Z because…") — timeless by nature.

## Audience

`WHY.md` and `HISTORY.md` are written for humans first — readable prose a maintainer
skims to understand the project's reasoning and to find retired docs. Useful to LLMs
too, but human readability wins where the two trade off. (Contrast `AGENTS.md`, which is
agent-first integration guidance.)

## Artifacts (both under `docs/`)

### WHY.md — distilled durable rationale

Captures intent, direction, and load-bearing "why" (consumer and maintainer). Organized
in stable thematic sections (e.g. Intent; Database & connection; Schema & migrations;
Tasks, enqueue & worker; Deliberately-not-doing). Rules:

- Why, not how. No file/class/module names, no structure map — that rots.
- Not a changelog.
- Top banner points readers to code / AGENTS.md / README for "how it works now".
- Every retained "why" is grounded against code: keep it only if the thing it explains
  still exists.

Altitude example (the level of every line):

> The worker uses its own dedicated connection, not Django's shared one, because at
> concurrency >1 a shared connection corrupts Absurd's bookkeeping. Delivery is
> at-least-once by design, so handlers must be idempotent.

(No part names; pure reason; survives any refactor.)

### HISTORY.md — recovery ledger

Append-only. One entry per retired spec/plan:

```
<authored-date> — <brief summary> → [view @<sha>](<origin/main blob URL>)
```

- `authored-date` = the `YYYY-MM-DD` filename prefix.
- `brief summary` = one line: what the doc covered / which approach it described.
- SHA link = `origin/main` blob URL (immutable git object — never dies).

Pure pointers into git → cannot go stale.

## Filter rule (used by capture-why)

Keep a fact only if NOT knowing it would let someone make a wrong or redundant decision.
Drop reversible plumbing and trial-and-error. Examples: release-trigger flip-flop → out;
"reuse Django's connection ⇒ psycopg3 mandatory" → in; "bump pinned Absurd ⇒ regenerate
SQL delta with `absurdctl`" → in (maintainer why).

## Skills (single-purpose)

### capture-why

- Inputs: `docs/specs`, `docs/plans`, plus code/git to ground against reality.
- Distills durable why into `WHY.md` under the stable sections, applying the filter
  rule.
- Grounds each "why" against code; drops any whose subject no longer exists.
- Re-run = merge/refresh: fold in new material; REPLACE any "why" now wrong (no stale
  survives, except the sanctioned tried-X-chose-Y form).
- Never deletes specs/plans or any file.

### archive-specs

- Inputs: `docs/specs`, `docs/plans`; identifies docs that are obsolete or fully
  captured in `WHY.md`.
- Obsolete = superseded by a later doc / abandoned approach / contradicted by code. Each
  candidate must be justified; user confirms.
- For each doc to retire: resolve its `origin/main` blob SHA URL (file must exist at
  that SHA), append `date — summary → SHA` to `HISTORY.md`, then delete the file.
- Safety: hard confirm gate — present the full list (file, summary, SHA) and require
  explicit approval before any deletion. Never delete a file not first recorded in
  `HISTORY.md`. Record-link-then-delete ordering, so the SHA always resolves.

## Command

`/dream` runs `capture-why`, then `archive-specs`. The ordering enforces
capture-before-delete; each skill stays independently runnable.

## Prep (mechanical, part of implementation)

- Move `docs/superpowers/specs` → `docs/specs`, `docs/superpowers/plans` → `docs/plans`;
  drop the `superpowers/` directory.
- CLAUDE.md: update the moved paths; add a section telling users about the available
  superpowers skills and caveman mode.

## Out of scope (YAGNI)

- Scheduled/automatic runs — `/dream` is manual.
- AGENTS.md / README freshness sync, dead-link checks — future `dream` sub-skills.
- ADR-per-file format — single `WHY.md` is the deliberate choice (one thing to load).

## Verification

- capture-why: `WHY.md` has no file/class/module names and no current-state "how" prose;
  each section traces to a code-grounded reason.
- archive-specs: every `HISTORY.md` SHA link resolves to a file present at that SHA; no
  spec/plan deleted without a preceding `HISTORY.md` entry; confirm gate blocks
  unattended deletion.
