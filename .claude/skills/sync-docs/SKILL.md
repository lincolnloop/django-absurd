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

| File                               | Audience                                       | Role / altitude                                                                                                                                                                                                                                                                                                                                                                                             |
| ---------------------------------- | ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `README.md`                        | repo landing                                   | **Trim.** The tl;dr happy path only: one-liner + alpha note, `pip install`, a ~10-line quickstart (TASKS snippet → `migrate` → `absurd_worker`; the `"default"` queue is declared for you), then a short **Documentation** section linking out. **Never grow it** — new detail goes to AGENTS.md, not here.                                                                                                 |
| `django_absurd/AGENTS.md`          | **end users / coding agents** (in the package) | The **full, standalone reference**, opening with a **What's here** map: quickstart, tasks, workflows, cron jobs, workers, cleanup, monitoring, testing, configuration (every `OPTIONS` key, `check` IDs, exceptions), database setup. Ships inside the installed package — discoverable from a project's venv. The **in-package / agent** canonical; mirror its facts into the site below.                  |
| `docs/web/` (Zensical site)        | **end users** (public docs site)               | The public **documentation site**: `docs/web/*.md` → built to `site/` (PR #30 / GitHub Pages). Navigable pages — **Home / Tasks / Workflows / Cron Jobs / Workers / Cleanup / Monitoring / Testing / Configuration** — presenting AGENTS.md's material for humans (may add examples/links; must not contradict it). On a user-facing change, update the relevant page **and** the `nav` in `zensical.toml`. |
| `examples/README.md` + `examples/` | runnable demo                                  | A working dockerized nanodjango project (`app.py`). Keep the **flow accurate**: `Dockerfile` CMD, `compose.yaml`, `app.py` (config / task / views / admin), and the "Run it" steps must match real behavior.                                                                                                                                                                                                |
| `CLAUDE.md`                        | **contributors / coding agents**               | Project **maintenance** only: naming, imports, testing conventions, runtime floor (Django / Python), tooling. NOT how-to — it _references_ `AGENTS.md` for usage/integration and must not duplicate it. Changes on convention / tooling / test-setup / runtime shifts (a different trigger from the user docs above; only the runtime floor is shared).                                                     |
| `docs/specs/`, `docs/plans/`       | design history                                 | NOT user docs. Design intent / decisions. Leave to `capture-why` / `archive-specs`; don't treat as the place to document features.                                                                                                                                                                                                                                                                          |

## House style — both docs homes

Applies to `AGENTS.md` and every `docs/web/` page; a new section must follow it.

1. **Example first.** Every concept heading opens with a code block ≤10 lines — nothing
   between the heading and the code.
2. **Then ≤3 sentences.** Prose explains only what the example cannot show. Never
   restate code in English.
3. **One example per concept.** On the site, sync/async twins collapse into `=== "Sync"`
   / `=== "Async"` tabs. `AGENTS.md` has no tabs, so it gets one example plus a one-line
   note on the difference.
4. **Opener ≤2 sentences**, then straight into the first concept.
5. **Cut internals** — anything the reader never types or reads back: job-name
   namespacing schemes, wrapper mechanics beyond what shows in the admin, "mirrors the
   SDK's signatures" tours, which module a logger child comes from.
6. **Admonitions only for damage** — data loss or destructive commands. The site has
   `!!!` / `???`; `AGENTS.md` has neither, so a warning there is bold prose.
7. **Django surface before Absurd-specifics.** Where a page covers both, the plain
   Django API comes first and finishes before any django-absurd-only surface starts — a
   reader who only needs Django's API can stop partway down having missed nothing.

**Caveat tiers.** Length decides location, not topic:

| Tier | Test                                  | Where                                                       |
| ---- | ------------------------------------- | ----------------------------------------------------------- |
| 1    | one line, belongs to one concept      | bullet directly under that concept                          |
| 2    | page-wide, owned by no single concept | short bullet list at the bottom                             |
| 3    | needs paragraphs                      | not a caveat — promote to a concept with an example, or cut |

Hard cap: more than two lines and it cannot be a bullet → promote or delete. Most
convoluted blocks are concepts wearing a warning costume, and a page ending in a
warnings dump reads unfinished.

**Sizing.** Measure in tokens, not lines — a pass that adds facts while cutting prose
barely moves the line count. Two things that look like savings and are not: converting a
table into an example is token-neutral (do it for the form — an agent copies an example
and translates a table — never for the size), and replacing embedded code with prose
plus links can come out **larger** than the code it replaced, while also costing
AGENTS.md its standalone property. Real reduction comes from deleting content, not
reshaping it; a light prose-compression pass over already-tight prose buys under 1%, so
don't spend one.

## When to act

A change triggers a doc pass if it touches any of:

- a **management command** or one of its flags (`absurd_worker`, `absurd_sync_queues`,
  …)
- **settings**: `TASKS`, backend `OPTIONS`, defaults (e.g. `DEFAULT_MAX_ATTEMPTS`)
- the **enqueue / params API** (`absurd_params(...)` as a decorator below `@task` or via
  `absurd_params(...).bind(task).enqueue(...)`)
- the **setup / run flow** (what a user must run, and in what order)
- **system checks** (which fire, their messages/hints)
- backend **capabilities** (`supports_*`)
- project **conventions / tooling / testing setup / runtime floor** — these live in
  `CLAUDE.md` (maintenance), a separate trigger from user-facing behavior

## Checklist

1. **README.md** — does the quickstart still reflect the happy path? If a step changed
   (e.g. a command became optional), fix it. If you're tempted to _add_ explanation, put
   it in AGENTS.md and link instead.
2. **AGENTS.md** — update the relevant section (Tasks / Workflows / Cron jobs / Workers
   / Cleanup / Monitoring / Testing / Configuration / Database setup). This is where
   completeness lives, and it must **stand alone**: it ships inside the installed
   package, so every fact a user needs is in the file. Mention that the docs site
   exists; never let it be the only place a fact lives. Three rules a change must
   respect:
   - **The [house style](#house-style--both-docs-homes) above**, the same one the site
     follows: concept heading → code block → ≤3 sentences → tier-1 bullets.
   - **No internals.** No maintainer rationale, no repo-internal paths (`tests/…`), no
     column layouts or wrapper mechanics — that reasoning belongs in `docs/WHY.md` (step
     6). Document what a user types or reads back, nothing else.
   - **Keep the map and the anchors true.** A new `##` section needs a row in the
     **What's here** table at the top of the file; a renamed heading needs every in-file
     `#anchor` link repointed (`grep -oE "\(#[a-z0-9-]+\)"` over the file).
3. **docs/web/ (site)** — update the matching page (`tasks.md` / `workflows.md` /
   `cron-jobs.md` / `workers.md` / `cleanup.md` / `monitoring.md` / `testing.md` /
   `configuration.md`, or `index.md` for the quickstart) so the site tracks AGENTS.md.
   Every page is **example-first** — a new section follows the
   [house style](#house-style--both-docs-homes) above. A new top-level topic also needs
   a `nav` entry in `zensical.toml`. Build to confirm: `uvx zensical build` (expect "No
   issues found"); the output `site/` is gitignored.
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
