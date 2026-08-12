# AGENTS.md prune — example-first, standalone

Scope: `django_absurd/AGENTS.md` only. Completes deferred half of
`2026-08-08-docs-howto-rewrite-design.md` (that spec rewrote `docs/web/`, left AGENTS.md
and README out).

Goal: cut maintainer internals, fold duplicated sections, backfill facts the site gained
in the rewrite. Carry MORE facts than before while getting smaller.

Measured outcome (tiktoken `o200k_base`, a proxy — Anthropic publishes no tokenizer):
16,492 → 14,562 tokens, −11.7%, with 16 facts added. Line count barely moved: the file
got denser, not shorter. A line target was the wrong instrument, and ~900 lines was
never reachable alongside standalone — see Size below.

## Constraints

- **Standalone.** Ships in the installed package (`site-packages/django_absurd/`). Every
  fact a user needs lives in the file. May state the docs site exists; site never sole
  home of a fact.
- **Audience: users + coding agents building on the library in their own Django
  project.** Not maintainers. Nothing about this repo's own tests, tooling, or
  internals.
- **Normal terse prose.** No telegraphic/caveman grammar: file is published, humans read
  it in a venv and on GitHub, and dropped function words flip normative meaning
  (`exactly one`, `never both`). Density comes from tables + cuts, not grammar.
- House style inherited from the site spec: concept heading → code block ≤10 lines → ≤3
  sentences → tier-1 bullets. Caveat tiers apply; >2 lines can't be a bullet → promote
  to concept or delete.
- Zensical-only syntax unavailable here: no `=== "Sync"` tabs, no `!!!`/`???`
  admonitions. Sync/async twins collapse to one example plus a one-line note; a
  destructive warning is bold prose.

## Structure

`## What's here` table directly after the opener — one row per `##`, stating what a
reader comes for, so the right section is pickable without opening it. Section order
mirrors site nav (Django surface before Absurd-specifics; Configuration last as
reference, since Quickstart already covers minimal setup).

| §              | Content                                                                                                                               | Today's lines           | Target |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- | ------ |
| Opener         | what it is, ≤2 sentences, pointers to docs site + `examples/`                                                                         | 1-15                    | 8      |
| What's here    | TOC table                                                                                                                             | —                       | 15     |
| Requirements   | Python 3.12+, Django 6.0+, psycopg3                                                                                                   | 17-22                   | 6      |
| Quickstart     | `TASKS` → `migrate` → `@task` → `enqueue` → worker                                                                                    | implicit in 24-79       | 30     |
| Tasks          | enqueue · read result · run later · retries & spawn options · idempotency keys                                                        | 167-251, 708-724        | 100    |
| Workflows      | steps · `run_step` · long steps/heartbeat · sleep · events · emit from a view · timeout · API table · gotchas                         | 1058-1320               | 150    |
| Cron jobs      | declare `SCHEDULE` · beat · pg_cron · reconcile · admin authoring · test databases · uninstall · operator setup · Docker              | 283-629                 | 170    |
| Workers        | command · full flag table · runs & retries                                                                                            | 253-281                 | 45     |
| Cleanup        | on demand · scheduled · retention knobs · `absurd_flush`                                                                              | 631-706                 | 60     |
| Monitoring     | logging · query queue state (keep `queue=` pruning rule) · admin                                                                      | 81-129, 985-1019        | 70     |
| Testing        | `dj_absurd` · durable time · fixture API + field/state tables · auto-cleanup · `manage.py test` · SCHEDULE in a test                  | 757-981                 | 120    |
| Configuration  | one settings block · both queue-declaration forms · full `OPTIONS` table · router · `check` + ID table · exceptions                   | 24-65, 131-165, 726-755 | 100    |
| Database setup | the `GRANT CREATE ON DATABASE` rule · pointers to pg_cron operator setup and what `migrate` installs · adopting an existing Absurd DB | 1021-1056               | 25     |
| Notes          | offline migrations, alpha                                                                                                             | 1322-1327               | 5      |

**"Deployment" was the wrong name and mostly a restatement.** Of its five items, three
already appeared elsewhere: at-least-once under Tasks (and now Workers, which owns runs
and retries), additive provisioning under Workers and Configuration, offline migrations
under Workers. What survived — the privilege `migrate` needs, and adopting an existing
database — is database setup, so the section says that. pg_cron's extension and grants
stay under Cron jobs, conditional on a scheduler choice made there, with Database setup
pointing at them so one privileged role's whole job is reachable from either end.

## Size

Density comes from cutting content, not from cutting words. Measured on this file:

- The structural work — internals out, duplicates folded, tables instead of reference
  prose — bought ~1,700 tokens.
- A light prose-compression pass over the whole file afterwards bought **120 tokens
  (0.8%)**, and most of that was the Deployment fold rather than the wording. Once prose
  is example-first and tight, there is nothing lexical left to take. Don't spend a pass
  on it again.
- What remains is genuine reference: Cron jobs 3,327 · Testing 2,541 · Workflows 2,079 ·
  Configuration 1,669 · Tasks 1,603. Two schedulers, four durable primitives, a fixture
  with three snapshot vocabularies.
- Standalone costs ~1,500 tokens against the site, concentrated in Cron jobs (+1,110,
  mostly the embedded Dockerfile and compose flags the site links out to) and Monitoring
  (+334, the ORM performance rule and non-default-`DATABASE` admin caveat the site cut).
  Deployment/database setup has no site page at all.

Three further cuts were then tried, and the estimates for them were wrong. Recorded so
nobody re-costs them the same way:

| Attempt                                                                   | Estimated | Actual   | Kept?                          |
| ------------------------------------------------------------------------- | --------- | -------- | ------------------------------ |
| Snapshot field tables → annotated example attribute reads                 | −1,200    | **−118** | yes, for the form not the size |
| Embedded pg_cron `Dockerfile` + `compose.yaml` → prose and two repo links | −250      | **+18**  | **no — reverted**              |
| Admin-authoring caveats, 5 bullets → 3                                    | −200      | **−23**  | yes                            |

What the misses teach:

- **A table converted to an example is token-neutral.** The −1,200 assumed deleting the
  field documentation; converting it instead keeps every field, which is the right call
  for an agent (it copies an example, it translates a table) but buys nothing in size.
  Judge such a change on form, not on tokens.
- **Prose plus links can be larger than the code it replaces.** Two Markdown link
  targets and the sentences needed to describe a Dockerfile outweighed the eight-line
  Dockerfile and compose stanza — and lost standalone at the same time. Reverted.
  Embedded code that a reader would otherwise have to be told about in words is already
  the compact form.
- Real size reduction from here means dropping user-facing facts, not reshaping them.

## Cut — internals and maintainer material

| Today's lines     | What                                                                                                                                                                             | Why                                                                                    |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| 538-561           | `ScheduledTask` wrapper model: column-by-column layout, `django_absurd_run_scheduled` reassembling jsonb server-side, `public`-vs-`absurd` schema rationale, reverse-drop hazard | Internals. Keep one user fact: option edits take effect next fire, no `cron.job` touch |
| 558-561           | "reconcile never stores `{}`… a directly-inserted row would pass the wrapper's `IS NOT NULL` check"                                                                              | Maintainer-only                                                                        |
| 511, 966, 978-980 | `tests/pg_cron/settings.py`, `tests/pg_cron/utils.py::build_pg_cron_tasks`, "as `tests/pg_cron` does"                                                                            | Repo-internal paths absent from a user's venv                                          |
| 1026-1030         | "verified on PostgreSQL 18 — refused with `permission denied for database`"                                                                                                      | Evidence for a decision; rule alone is the user fact. Already `WHY.md:48-51`           |
| 677-681           | `absurd.enable_cron` / `absurdctl cron` job-naming internals                                                                                                                     | Compress to the standing warning: drive cleanup one way only                           |
| 242-248, 250      | deferred-enqueue wrapper mechanics; "logs as the run-level `task suspended` line"                                                                                                | Keep the admin-visible row name only; drop log trivia                                  |
| 604-606           | `_dj:<db>:s:…` / `_dj:<db>:a:…` job-name namespacing                                                                                                                             | Named in the site spec's own cut list; reader never types it                           |

Cut rationale needs no new home — `docs/WHY.md` already records the projection-table
design (`347-363`) and the create-namespace privilege finding (`48-51`). No
`capture-why` run. Optional one-liner WHY.md gap: why wrapper + projection table live in
`public` rather than `absurd`.

## Fold — same fact stated twice in one file

- `absurd.E007` bullet list: 148-149 + 609-624 → one list, under Cron jobs; the ID table
  in Configuration links to it.
- `absurd.E012`: 156-158 + 189 + 626-629 → ID table row + one bullet under operator
  setup.
- pg_cron grammar rules: 465-480 + 615-617 → one place.
- pg_cron test-database gating: 497-529 + 971-981 → one place, in Testing, linked from
  Cron jobs.
- `## Validate schedules` (609-629) disappears as a section; content lands in Cron
  jobs + the Configuration ID table.

## Backfill — site has it, AGENTS.md doesn't

Drift direction is site → AGENTS (site rewritten after this file). Each already exists
in `docs/web/`, so no research:

| Fact                                                                                                                                        | Source                   |
| ------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| `retry_strategy` default `kind: "none"`; `fixed` waits `base_seconds` 60; `exponential` 30 × 2^(n−1), uncapped without `max_seconds`        | `tasks.md:104-111`       |
| pg_cron timezone is the `cron.timezone` GUC, default GMT — not Django's `TIME_ZONE`                                                         | `cron-jobs.md:91`        |
| Always reconcile as the same role — pg_cron keys jobs on `(jobname, username)`; mixing duplicates jobs and breaks pruning                   | `cron-jobs.md:118`       |
| Run `--teardown` BEFORE removing the app from `INSTALLED_APPS`; removing it leaves jobs firing                                              | `cron-jobs.md:159-165`   |
| Schedule a `cron.job_run_details` purge — only place fire-time failures show, grows unbounded                                               | `cron-jobs.md:192-194`   |
| Managed Postgres exposes operator flags as parameter groups                                                                                 | `cron-jobs.md:188`       |
| Step results go through `json.dumps`: no sets, `datetime`, custom classes; `tuple` → `list`                                                 | `workflows.md:55`        |
| Inserting/removing/reordering a step corrupts replay → retire the task, add a new one                                                       | `workflows.md:50-52`     |
| `heartbeat()` in a loop for long steps; `claim_timeout` default 120                                                                         | `workflows.md:78-91`     |
| Sleeps share the step namespace and counter                                                                                                 | `workflows.md:128`       |
| `context.headers` read example                                                                                                              | `workflows.md:220-231`   |
| `absurd_flush`: scheduled jobs survive and error on each fire until queues re-provisioned; the `CLEANUP` job survives harmlessly            | `cleanup.md:92-94`       |
| `cleanup_limit` applies separately to task and event rows; unknown queue name raises the raw DB error                                       | `cleanup.md:25-26, 70`   |
| One worker per queue — `--queue` takes one name                                                                                             | `workers.md:29`          |
| `result.errors` populated when FAILED                                                                                                       | `tasks.md:39`            |
| Single `OPTIONS` table: adds `SCHEDULE`, `SYNC_SCHEDULES_ON_MIGRATE`, `SYNC_SCHEDULES_ON_TEST_DB`, `PG_CRON_ON_TEST_DB` (today: prose only) | `configuration.md:59-70` |

## Out of scope

- `docs/web/` — unchanged; backfill flows site → AGENTS, not back.
- `README.md` — unchanged; stays the trim quickstart.
- `docs/WHY.md` — unchanged (optional one-liner above is a judgement call at review
  time).
- No API or behavior change. No content invention: every fact exists in current docs or
  code.

## Maintenance rule lands in the skill

`sync-docs` step 2 rewritten to own the rules this spec establishes: standalone,
example-first house style, no internals, new `##` needs a **What's here** row, renamed
heading needs its in-file anchors repointed. Audience-map row updated to the new section
list. Done ahead of the rewrite so the skill describes the target.

## Verification

- 10 in-file anchors (`#sleep`, `#test-databases`, `#scheduling-recurring-tasks`,
  `#retrieving-results`, `#exceptions`, `#workers`, `#validate-schedules`, `#timeout`,
  `#querying-queue-state-orm`, `#configure`) repointed. Nothing outside the file links
  to them (swept the repo including `.claude/`).
- `django_absurd/context.py:182` docstring cites AGENTS.md's "await_task_result is not
  provided" section; that heading becomes a bullet, so the docstring is updated with it.
- `tests/core/test_packaging.py` asserts only that the shipped guide is non-empty — no
  content pins.
- Outbound links checked against live pages (site spec's own rule: verify the anchor, do
  not guess it).
- `uv run pre-commit run --all-files` — prettier owns Markdown (printWidth 88,
  `proseWrap = "always"`).
- No test run: docs plus one docstring, no behavior change.
