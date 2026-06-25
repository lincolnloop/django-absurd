---
name: sync-docs
description:
  Use when a change touches user-facing behavior — management commands or their flags,
  TASKS / OPTIONS settings, the enqueue or params API, defaults, the setup flow, or
  system checks. Keeps the project's docs in sync with the code so updates don't get
  lost in context.
---

# sync-docs

## Overview

The project has several docs with **distinct audiences and a single canonical home
each**. When code changes user-facing behavior, the right doc(s) must be updated — and
updated in the _right_ place, at the _right_ altitude. This skill is the checklist so
that never gets dropped.

There is no doc site yet (planned). Until then, **AGENTS.md is the canonical full
reference**.

## Audience map — where each fact lives

| File                               | Audience                                     | Role / altitude                                                                                                                                                                                                                                                                                                     |
| ---------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `README.md`                        | repo landing                                 | **Trim.** The tl;dr happy path only: one-liner + alpha note, `pip install`, a ~10-line quickstart (TASKS snippet → `migrate` → `absurd_worker`, "queues auto-create"), then a short **Documentation** section linking out. **Never grow it** — new detail goes to AGENTS.md, not here.                              |
| `django_absurd/AGENTS.md`          | **end users** (devs integrating the package) | The **full reference**: requirements, configuration + every `OPTIONS` key, run, validate (`check`), workers, enqueue + params, retrieving results, deployment notes, adopting an existing DB. Ships inside the installed package, so it is discoverable from a project's venv. Canonical until the doc site exists. |
| `examples/README.md` + `examples/` | runnable demo                                | A working dockerized project. Keep the **flow accurate**: `Dockerfile` CMD, `compose.yaml`, `demo/tasks.py`, `enqueue_demo`, and the "Run it" steps must match real behavior.                                                                                                                                       |
| `docs/specs/`, `docs/plans/`       | design history                               | NOT user docs. Design intent / decisions. Leave to `capture-why` / `archive-specs`; don't treat as the place to document features.                                                                                                                                                                                  |

## When to act

A change triggers a doc pass if it touches any of:

- a **management command** or one of its flags (`absurd_worker`, `absurd_sync_queues`,
  …)
- **settings**: `TASKS`, backend `OPTIONS`, defaults (e.g. `DEFAULT_MAX_ATTEMPTS`)
- the **enqueue / params API** (`AbsurdSpawnParams`, `@absurd_default_params`, reserved
  kwargs)
- the **setup / run flow** (what a user must run, and in what order)
- **system checks** (which fire, their messages/hints)
- backend **capabilities** (`supports_*`)

## Checklist

1. **README.md** — does the quickstart still reflect the happy path? If a step changed
   (e.g. a command became optional), fix it. If you're tempted to _add_ explanation, put
   it in AGENTS.md and link instead.
2. **AGENTS.md** — update the relevant section (Configure / Run / Validate / Enqueue /
   Workers / Results / Deployment / Adopting). This is where completeness lives.
3. **examples/** — if the change alters the run flow or a demonstrated capability,
   update `examples/README.md` and the runnable bits (`Dockerfile` CMD, `demo/tasks.py`,
   `enqueue_demo`). Prefer demonstrating the simplest happy path.
4. **Cross-check copy** — exact command names, flag names, message text, and defaults
   must match the code verbatim across all three files.

## Conventions

- Keep README trim; AGENTS complete; examples runnable. Don't duplicate full reference
  prose into README.
- Don't narrate history in docs ("previously…", "this used to…") — document current
  behavior.
- After editing, skim the changed docs once with fresh eyes for stale
  command/flag/default references the change made obsolete.
