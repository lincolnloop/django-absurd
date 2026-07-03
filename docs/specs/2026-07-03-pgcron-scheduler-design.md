# pg_cron scheduler (SP2) — design

Issue: [#20](https://github.com/lincolnloop/django-absurd/issues/20). Database-side
execution backend for django-absurd's recurring schedules. SP2 of #20 (SP1 = beat,
merged). Reuses SP1's settings-declared `SCHEDULE`; swaps the in-process beat for
pg_cron jobs that call `absurd.spawn_task` directly in Postgres. No new tables, no
admin, no separate app.

Depends on the beat leading-seconds fix (PR #40): the 6-field seconds convention here
assumes leading seconds (`second_at_beginning=True`).

## Goal

Declare recurring tasks in settings (same as beat). Select DB-side execution with
`OPTIONS["SCHEDULER"]="pg_cron"`. A reconcile step turns declared entries into pg_cron
jobs; Postgres fires them; existing workers run the enqueued tasks. Celery-beat shape,
DB-side engine, no scheduler process.

## Scope (SP2)

In: `SCHEDULER="pg_cron"` selector; `sync_crons` reconcile (upsert + prune owned jobs);
`absurd_sync_crons` command + `post_migrate` hook; croniter→pg_cron schedule translation
with the sub-minute shim; `E007` extension (pg_cron-translatable cron) plus `E008`
(pg_cron availability, error); beat/pg_cron mutual exclusion; docs.

Out: multi-DB topologies T2/T3 (`schedule_in_database`, cron-DB separate from Absurd DB)
— seam-ready, not built. Runtime TZ check (docs-only now). Admin/model-managed schedules
(not pursued). Idempotency key (single DB scheduler — none needed). Retry/headers/
cancellation beyond `max_attempts` (additive later).

## Topology (T1 only)

pg_cron co-located with Absurd on one DB (the backend's `DATABASE`). `cron.schedule`
runs locally. pg_cron's scheduler + `cron.job` table live in exactly one DB
(extension-install DB); T1 = that DB is the Absurd DB. T2 (designated cron DB via
`schedule_in_database`) and T3 (Absurd on non-default Django DB) are future — isolated
behind the reconcile seam so the later change is localized, not a redesign.

## Components / files

`django_absurd/scheduler.py` — add:

- `sync_crons(backend) -> None` — the one seam. Reads `get_settings_schedules(backend)`;
  per entry runs `select cron.schedule(<jobname>, <pg_schedule>, <spawn_sql>)`; prunes
  owned-but-undeclared jobs via `cron.unschedule`. All pg_cron SQL confined here (local
  `cron.schedule` for T1). Runs on Absurd DB connection (`backend.database`).
- `to_pg_cron_schedule(cron: str) -> str` — translate. 5-field → passthrough. 6-field
  `*/N` (N 1–59) or `*` in seconds AND other five fields all `*` → `"N seconds"`
  (`*`→1). Anything else (seconds combined with non-`*` units; non-step seconds
  list/value) → raise `ValueError`. Used by reconcile (emit) and E007 (validate).
- `build_spawn_sql(schedule) -> str` — the `$$ select absurd.spawn_task(...) $$` body.
- job naming: `absurd:<backend_alias>:<schedule_name>`. Prune scope = `absurd:<alias>:%`
  (never touches hand-made `cron.job` rows or other aliases/projects).

`django_absurd/management/commands/absurd_sync_crons.py` — calls `sync_crons`; logs
upserted/pruned counts; refuses (CommandError) unless `SCHEDULER="pg_cron"`.

`django_absurd/apps.py` — `post_migrate` handler runs `sync_crons` when
`SCHEDULER="pg_cron"` (mirrors existing `sync_queues` post_migrate).

`absurd_beat` / `absurd_worker --beat` — raise `CommandError` when `SCHEDULER="pg_cron"`
("SCHEDULER is pg_cron — beat disabled; run `absurd_sync_crons`"). No double-fire path.

`django_absurd/checks.py` — extend E007; add E008.

Conventions: verb names, helpers below callers, absolute imports, `import typing as t`.

## Settings

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "SCHEDULER": "pg_cron",       # default "beat"; pg_cron = DB-side
            "SCHEDULE": {                 # unchanged schema, shared with beat
                "nightly-report": {
                    "task": "myapp.tasks.send_report",
                    "cron": "0 2 * * *",
                },
            },
        },
    },
}
```

`SCHEDULER` ∈ `{"beat"(default), "pg_cron"}`, exactly one per backend. `SCHEDULE` schema
identical to beat: required `task`, `cron`; optional `queue`, `args`, `kwargs`.

## Spawn mapping

Verified
`absurd.spawn_task(p_queue_name text, p_task_name text, p_params jsonb, p_options jsonb default '{}')`.
`p_options` reads `max_attempts`, `retry_strategy`, `idempotency_key`, `headers`,
`cancellation`. Emit:

```sql
select cron.schedule(
  'absurd:default:nightly-report',
  '0 2 * * *',
  $$ select absurd.spawn_task(
       'default',
       'myapp.tasks.send_report',
       '{"args": [], "kwargs": {}}'::jsonb,
       '{"max_attempts": 5}'::jsonb
     ); $$
);
```

`p_task_name` = dotted path (worker resolves via `import_string`, same as enqueue).
`p_params` = `{"args": …, "kwargs": …}` (byte-shape the worker deserializes).
`p_options` carries `max_attempts` (from task `@absurd_default_params` / backend
default); omit for defaults. SQL literals built with proper JSON quoting/escaping
(parameterized where the `cron.schedule` API allows; else safe json dump — args/kwargs
already E007-JSON-validated). No idempotency key (single DB scheduler).

## Cron translation + sub-minute shim

- 5-field standard → passthrough (croniter validates; pg_cron's Vixie parser accepts).
- 6-field leading seconds: `*/N` (N 1–59) or `*` in the seconds field, **other five
  fields all `*`** → `"N seconds"` (pass N through; `*`→1). Accepts alignment/boundary
  imprecision (`*/7`→`"7 seconds"` fires evenly on pg_cron vs beat's uneven {0,7,…,56}).
  "Good enough."
- Reject (raise): seconds combined with any non-`*` unit (pg_cron hard rule: "cannot use
  seconds with other time units"), or non-step seconds (specific value / list — no
  interval to translate).

## Checks

- **E007** (extend, existing per-entry SCHEDULE validation): when `SCHEDULER="pg_cron"`,
  each `cron` must pass `to_pg_cron_schedule` (else precise reject msg — e.g. "pg_cron
  can't combine seconds with other fields; use beat for this schedule"). Beat path
  unchanged (accepts full croniter).
- **E008** (new, **error**): when `SCHEDULER="pg_cron"` — pg_cron extension installed
  (`pg_extension` has `pg_cron`), `cron` schema usable, Absurd co-located on `DATABASE`.
  `msg` = problem, `hint` = fix (enable extension / grant / co-locate). No DB access
  beyond a read; follows existing `absurd.Exxx` pattern.

## Timezone

Docs-only (v1). pg_cron fires in `cron.timezone` (GMT default; global GUC, not per-job).
Document: (a) state it's GMT/cron.timezone-native and differs from beat's
Django-`TIME_ZONE` local-time semantics; (b) recommend setting `cron.timezone` = Django
`TIME_ZONE` when non-UTC. Common modern case (both UTC) = no-op. Runtime warn/check
deferred (own follow-on).

## Testing

Function-based pytest, behavior-driven. pg_cron added to the dev compose Postgres image
(build/extend the DB service so `create extension pg_cron` works on the host suite).

- `to_pg_cron_schedule`: unit table — 5-field passthrough; `*/30`→`"30 seconds"`;
  `*/7`→`"7 seconds"`; `*`→`"1 seconds"`; reject `*/30 9 * * * *`, reject
  `15,45 * * * * *`, reject `30 * * * * *`. RED-first.
- `absurd_sync_crons` (behavioral): run command, assert emitted text AND resulting
  `cron.job` rows (jobname, schedule, command) via SQL. Upsert idempotent (re-run = same
  rows). Prune: remove a declared entry, re-sync, assert its `absurd:<alias>:%` job
  gone; a hand-made non-prefixed `cron.job` survives.
- `post_migrate` reconcile fires only under `SCHEDULER="pg_cron"`.
- E007 pg_cron-cron rejects (full text per entry); E008 states (extension absent → error
  text). Drive with real DB conditions where possible.
- beat commands raise `CommandError` under `SCHEDULER="pg_cron"` (assert message).
- End-to-end: sync a `*/1 * * * *` schedule, let pg_cron fire, worker burst, assert task
  ran.

## Docs

`docs/web/cron-jobs.md` Database-side section: "coming soon" → real. Enable extension,
`SCHEDULER="pg_cron"`, `absurd_sync_crons` (+ auto on migrate), TZ note (both framings),
sub-minute rules (5-field / clean `*/N`; rejects), beat mutual-exclusion, E007/E008.
`AGENTS.md` scheduling section: add pg_cron backend, SCHEDULER selector, reconcile,
availability. README unchanged. WHY.md: capture DB-side-vs-beat rationale after build.

## Decisions (resolved in brainstorming)

- **Settings-declared, not admin/DB tables.** Abandoned the CronJob model / admin /
  task-dropdown path: Django Tasks has no task registry (only lazy `import_string`
  resolution), a dropdown would be the sole thing needing whole-codebase discovery, and
  it cut against the project grain. Settings + E007 reuse the existing dotted-path
  model.
- **Reconcile on migrate AND command** (both) — mirrors `sync_queues`.
- **Own-prefix prune** (`absurd:<alias>:`) — destructive for our jobs only.
- **beat vs pg_cron mutually exclusive** — one `SCHEDULER` per backend; beat commands
  refuse under pg_cron.
- **E008 = error** (not warn).
- **TZ docs-only** in v1; runtime check deferred.
- **Sub-minute shim = pass-through `*/N`**, imprecision accepted; reject only genuinely-
  impossible combos. No PyPI lib parses pg_cron's `"N seconds"` grammar — shim is ~20
  lines.
- **No idempotency key** — DB-side single scheduler, no concurrent runs.
- **T1 only** — reconcile seam isolates the future `schedule_in_database` swap.

## Decomposition (future)

- SP3 — multi-DB topologies (T2 designated cron DB via `schedule_in_database`; T3 Absurd
  on non-default Django DB). Seam-ready.
- Runtime TZ check (E-code warning when `cron.timezone` ≠ Django `TIME_ZONE`).
- Extended `p_options` (retry_strategy, headers, cancellation) from schedule spec.
