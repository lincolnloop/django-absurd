---
name: sync-docs
description:
  Use when a change touches user-facing behavior — management commands or their flags,
  TASKS / OPTIONS settings, the enqueue or params API, defaults, the setup flow, or
  system checks, or project conventions. Keeps README.md, the AGENTS.md integration
  guide, the docs/web/ documentation site, the runnable example (examples/), CLAUDE.md
  (maintenance), and design decisions in docs/WHY.md (via capture-why) in sync with the
  code so updates don't get lost in context.
---

# sync-docs

## Overview

The project has several docs with **distinct audiences and a single canonical home
each**. When code changes user-facing behavior, the right doc(s) must be updated — and
updated in the _right_ place, at the _right_ altitude. This skill is the checklist so
that never gets dropped.

Two user-facing homes now: **`docs/web/`** is the public documentation **site**
(Zensical, PR #30 / GitHub Pages) — the primary docs for humans;
**`django_absurd/AGENTS.md`** is the full reference that ships **inside the installed
package** (discoverable from a project's venv and by coding agents). Keep both in step —
the site may expand on AGENTS.md but must not contradict it.

## Audience map — where each fact lives

| File                               | Audience                                       | Role / altitude                                                                                                                                                                                                                                                                                                                                                      |
| ---------------------------------- | ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `README.md`                        | repo landing                                   | **Trim.** The tl;dr happy path only: one-liner + alpha note, `pip install`, a ~10-line quickstart (TASKS snippet → `migrate` → `absurd_worker`, "queues auto-create"), then a short **Documentation** section linking out. **Never grow it** — new detail goes to AGENTS.md, not here.                                                                               |
| `django_absurd/AGENTS.md`          | **end users / coding agents** (in the package) | The **full reference**: requirements, configuration + every `OPTIONS` key, run, validate (`check`), workers, enqueue + params, scheduling, retrieving results, deployment, adopting an existing DB. Ships inside the installed package — discoverable from a project's venv. The **in-package / agent** canonical; mirror its facts into the site below.             |
| `docs/web/` (Zensical site)        | **end users** (public docs site)               | The public **documentation site**: `docs/web/*.md` → built to `site/` (PR #30 / GitHub Pages). Navigable pages — **Home / Tasks / Scheduling / Configuration / How it works** — presenting AGENTS.md's material for humans (may add examples/links; must not contradict it). On a user-facing change, update the relevant page **and** the `nav` in `zensical.toml`. |
| `examples/README.md` + `examples/` | runnable demo                                  | A working dockerized nanodjango project (`app.py`). Keep the **flow accurate**: `Dockerfile` CMD, `compose.yaml`, `app.py` (config / task / views / admin), and the "Run it" steps must match real behavior.                                                                                                                                                         |
| `CLAUDE.md`                        | **contributors / coding agents**               | Project **maintenance** only: naming, imports, testing conventions, runtime floor (Django / Python), tooling. NOT how-to — it _references_ `AGENTS.md` for usage/integration and must not duplicate it. Changes on convention / tooling / test-setup / runtime shifts (a different trigger from the user docs above; only the runtime floor is shared).              |
| `docs/specs/`, `docs/plans/`       | design history                                 | NOT user docs. Design intent / decisions. Leave to `capture-why` / `archive-specs`; don't treat as the place to document features.                                                                                                                                                                                                                                   |

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
- project **conventions / tooling / testing setup / runtime floor** — these live in
  `CLAUDE.md` (maintenance), a separate trigger from user-facing behavior

## Checklist

1. **README.md** — does the quickstart still reflect the happy path? If a step changed
   (e.g. a command became optional), fix it. If you're tempted to _add_ explanation, put
   it in AGENTS.md and link instead.
2. **AGENTS.md** — update the relevant section (Configure / Run / Validate / Enqueue /
   Workers / Scheduling / Results / Deployment / Adopting). This is where completeness
   lives.
3. **docs/web/ (site)** — update the matching page (`tasks.md` / `scheduling.md` /
   `configuration.md` / `how-it-works.md`, or `index.md` for the quickstart) so the site
   tracks AGENTS.md. A new top-level topic also needs a `nav` entry in `zensical.toml`.
   Build to confirm: `uvx zensical build` (expect "No issues found"); the output `site/`
   is gitignored.
4. **examples/** — always check the example when the run flow or a demonstrated
   capability changes. Update `examples/README.md` AND the runnable bits it documents
   (`Dockerfile` CMD, `compose.yaml`, `app.py`), kept to the simplest happy path. If the
   flow changed, re-run it (`docker compose up --build --abort-on-container-exit`) to
   confirm it still exits `0`.
5. **CLAUDE.md** — only if a convention, the runtime floor, testing setup, or tooling
   changed. Keep it maintenance-only; route any how-to/usage into AGENTS.md and
   reference it, don't duplicate.
6. **WHY.md (design decisions)** — if the change made an architecture/design decision
   worth keeping, run **`capture-why`** to fold it into `docs/WHY.md`. Do NOT run
   `archive-specs` (the prune) — specs/plans stay put until you deliberately digest.
7. **Cross-check copy** — exact command names, flag names, message text, and defaults
   must match the code verbatim across the user-facing docs (README, AGENTS.md, and the
   `docs/web/` pages); the runtime floor (Django / Python) must agree between
   `CLAUDE.md`, README, and AGENTS.

## Conventions

- Keep README trim; AGENTS complete; the `docs/web/` site mirrors AGENTS (don't let them
  drift — and build the site before claiming it's updated); examples runnable; CLAUDE.md
  maintenance-only (it references AGENTS for how-to). Don't duplicate full reference
  prose into README, and don't put usage/how-to into CLAUDE.md.
- **WHY.md is in scope (via `capture-why`); pruning is not.** Refresh `docs/WHY.md` with
  the run-`capture-why` step above so design decisions land while fresh — but never run
  `archive-specs` here. Retiring specs/plans to `docs/HISTORY.md` is a deliberate digest
  step (the `/dream` flow), so specs + plans persist until you choose to prune.
- Don't narrate history in docs ("previously…", "this used to…") — document current
  behavior.
- After editing, skim the changed docs once with fresh eyes for stale
  command/flag/default references the change made obsolete.
