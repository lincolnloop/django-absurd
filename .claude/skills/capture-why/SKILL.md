---
name: capture-why
description:
  Use when refreshing docs/WHY.md or consolidating the project's durable reasoning from
  docs/specs and docs/plans — e.g. specs/plans have accumulated, or before retiring
  them. Captures intent and load-bearing "why", never current structure.
---

# capture-why

## Overview

Distill the durable **why** out of `docs/specs/` + `docs/plans/` into `docs/WHY.md`:
intent, direction, and the load-bearing reasons behind the project's shape — for humans
first, useful to LLMs. **Why, not how.** How-it-works-now lives in the code,
`AGENTS.md`, and `README.md`; `WHY.md` never duplicates it, so it can't go stale.

This skill never deletes or moves files. Retiring consumed docs is `archive-specs`'s
job.

## The filter rule

Keep a fact only if **not knowing it would let someone make a wrong or redundant
decision.** Everything else — reversible plumbing, trial-and-error, "we flipped this
setting twice" — is noise. Drop it.

- In: "reuse Django's connection ⇒ psycopg3 is mandatory."
- In (maintainer): "when bumping the pinned Absurd version, regenerate the SQL delta
  with `absurdctl`."
- Out: "the release trigger changed from tag-push to release-published" (reversible
  plumbing).

## Altitude: why, not how

| ❌ Describes the machine (rots)                                                          | ✅ Explains the choice (durable)                                                                                                            |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| "The worker (`worker.py`) uses `LazyTaskRegistry` and an autocommit psycopg connection." | "The worker uses its own connection, not Django's shared one, because at concurrency >1 a shared connection corrupts Absurd's bookkeeping." |

Every line must survive a rename. No file/class/module names, no structure map, no
changelog phrasing.

## Procedure

1. Read everything in `docs/specs/` and `docs/plans/`.
2. **Ground each candidate "why" against the current code.** If the thing it explains no
   longer exists in code, drop it — don't import a spec's stale claim.
3. Apply the filter rule.
4. Write/refresh `docs/WHY.md` as readable prose under stable thematic sections (e.g.
   Intent; Database & connection; Schema & migrations; Tasks, enqueue & worker;
   Deliberately-not-doing). Start with a banner: _"Why, not how — see code / AGENTS.md /
   README for current behavior."_
5. **Re-run = merge/refresh:** fold in new reasoning; **replace** any "why" that's now
   wrong. The only "stale" allowed is the explanatory form — "we tried X, chose Y
   because Z."
6. Do not delete or move any file.

## Common mistakes

- Leaking file/class/module names or a structure description → it rots; cut it.
- Writing how-it-works-now → that's the code's job and goes stale; keep only why.
- Letting it become a changelog of every change → only load-bearing reasons survive.
- Trusting a spec's claim without checking the code still does that → ground first.
