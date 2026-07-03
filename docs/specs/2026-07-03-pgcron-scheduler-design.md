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

In: `SCHEDULER="pg_cron"` selector; `sync_crons` reconcile (upsert + prune owned jobs)
and `teardown_crons` (remove all owned jobs when pg_cron is deselected);
`absurd_sync_crons` command + `post_migrate` hook; injection-safe command build
(server-side `format('%L')`); croniter→pg_cron schedule translation with the sub-minute
shim; `E007` extension (pg_cron-translatable cron) plus `E008` (pg_cron availability +
schedulability, error); beat/pg_cron mutual exclusion; docs.

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
  per entry upserts a job (`cron.schedule` upserts by name — verified in the pg_cron
  README named-job form); prunes owned-but-undeclared jobs via `cron.unschedule`. All
  pg_cron SQL confined here (local `cron.schedule` for T1). Runs on the Absurd DB
  connection (`backend.database`).
- `teardown_crons(backend) -> None` — unschedule **every** `absurd:<alias>:%` job.
  Called when pg_cron is deselected (see post_migrate) and by
  `absurd_sync_crons --teardown`, so switching `SCHEDULER` away from `pg_cron` doesn't
  leave orphaned jobs firing (C2).
- `build_schedule_call(jobname, pg_schedule, schedule) -> (sql, params)` — returns a
  **parameterized** statement, NOT an assembled string. The inner spawn command is built
  **server-side** so runtime values can't inject (C1):
  ```sql
  select cron.schedule(%s, %s, format(
      'select absurd.spawn_task(%L, %L, %L::jsonb, %L::jsonb)',
      %s, %s, %s, %s))
  ```
  with bind params
  `(jobname, pg_schedule, queue, dotted_task, params_json, options_json)`.
  `format('%L', …)` does the literal-quoting; Python never interpolates values into SQL.
- `resolve_spawn_options(schedule) -> dict` — reuse the enqueue path's
  `build_merged_spawn_options` (backends.py) so a schedule's task gets the **same**
  merged options (`max_attempts`, retry_strategy, …) it would on `.enqueue()` — parity
  with beat, no silent drop (I2). Requires importing the task (already done by E007's
  validation).
- `to_pg_cron_schedule(cron: str) -> str` — translate. 5-field → validate against
  pg_cron's grammar (a strict Vixie 5-field subset — NOT merely `croniter.is_valid`,
  which accepts exprs pg_cron rejects, I1) then passthrough. 6-field `*/N` (N 1–59) or
  `*` in seconds AND other five fields all `*` → `"N seconds"` (`*`→1). Anything else
  (seconds combined with non-`*` units; non-step seconds list/value) → raise
  `ValueError`. Used by reconcile (emit) and E007 (validate).
- job naming: `absurd:<backend_alias>:<schedule_name>`. Prune/teardown scope =
  `absurd:<alias>:%` (never touches hand-made `cron.job` rows). **Constraint (M1):** two
  Django projects sharing one DB with the same backend alias would prune each other's
  jobs — documented; a configurable namespace is a future knob.

`django_absurd/management/commands/absurd_sync_crons.py` — `sync_crons` (or
`teardown_crons` with `--teardown`); logs upserted/pruned counts; refuses (CommandError)
unless `SCHEDULER="pg_cron"` (except `--teardown`, allowed to clean up after
deselection).

`django_absurd/apps.py` — `post_migrate` handler: run `sync_crons` when
`SCHEDULER="pg_cron"`, else `teardown_crons` (removes orphans after switching away).
**Best-effort**, mirroring `provision_queues_after_migrate`: catch
`ImproperlyConfigured/OperationalError/ProgrammingError` (pg_cron/schema absent) and
skip so a missing extension never breaks `migrate` (I3); E008 is the loud surface.

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

That literal is the **rendered** result; it is NOT assembled by string interpolation in
Python. The command text is produced **server-side** via
`format('select absurd.spawn_task(%L, %L, %L::jsonb, %L::jsonb)', queue, task, params, options)`
with the values passed as bind parameters (C1). `%L` guarantees literal-safe quoting, so
a string arg containing quotes / `$$` / backslashes cannot break out or inject.

`p_task_name` = dotted path (worker resolves via `import_string`, same as enqueue).
`p_params` = `{"args": …, "kwargs": …}` (byte-shape the worker deserializes; args/kwargs
already E007-JSON-validated). `p_options` = the **full merged** spawn options from
`resolve_spawn_options` (same as `.enqueue()` — `max_attempts` and any
`@absurd_default_params`), so pg_cron and beat behave identically for the same task
(I2). No idempotency key (single DB scheduler).

## Cron translation + sub-minute shim

- 5-field standard → validate against **pg_cron's** grammar, then passthrough.
  `croniter` is more permissive than pg_cron's Vixie parser (accepts exprs pg_cron
  rejects), so `croniter.is_valid` alone would let `check` pass and then fail at
  `cron.schedule` runtime (I1). Restrict to the strict common 5-field subset both
  accept, or validate the string against pg_cron's rules directly.
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
  (`pg_extension` has `pg_cron`), Absurd co-located on `DATABASE`, and the connecting
  role can actually **schedule** —
  `has_function_privilege(current_user, 'cron.schedule(text,text,text)', 'EXECUTE')`,
  not merely `USAGE` on the `cron` schema (I4: USAGE doesn't prove schedulability;
  `cron.schedule` requires membership in the pg_cron-privileged role or superuser).
  `msg` = problem, `hint` = fix (enable extension / grant EXECUTE / co-locate).
  Read-only; follows existing `absurd.Exxx` pattern.

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
- `post_migrate`: reconcile fires under `SCHEDULER="pg_cron"`; **teardown** fires when
  switched away (`beat`/unset) — assert prior `absurd:<alias>:*` jobs removed (C2).
  Extension absent → post_migrate skips silently, `migrate` succeeds (I3).
- **Injection (C1):** a schedule with `args=["'; drop schema absurd cascade; --", "$$"]`
  syncs to a `cron.job` whose command still calls `spawn_task` with those exact values
  as data (assert the row's command + that a subsequent fire spawns the literal args) —
  no SQL executed out of band.
- **Spawn parity (I2):** a task with `@absurd_default_params(max_attempts=3)` yields a
  pg_cron `p_options` carrying `max_attempts=3` (assert emitted command / resulting
  run).
- E007 pg_cron-cron rejects incl. a **croniter-valid-but-pg_cron-invalid** 5-field expr
  (full text per entry); E008 states — extension absent, and role lacking EXECUTE on
  `cron.schedule` (I4) → error text. Drive with real DB conditions where possible.
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
- **Own-prefix prune** (`absurd:<alias>:`) — destructive for our jobs only. **Teardown
  on deselect (C2):** switching `SCHEDULER` away from pg_cron removes all owned jobs
  (post_migrate + `--teardown`), so they can't orphan and double-fire with beat.
- **Injection-safe command build (C1):** the spawn command is assembled server-side with
  `format('%L', …)` over bind params — never Python string interpolation.
- **Spawn parity (I2):** pg_cron emits the same merged spawn options as `.enqueue()`
  (via `build_merged_spawn_options`); no silent divergence from beat.
- **Validate against pg_cron grammar, not croniter (I1):** `croniter.is_valid` is too
  permissive; the check must reject exprs pg_cron would reject at schedule time.
- **E008 proves schedulability (I4):** EXECUTE on `cron.schedule`, not just schema
  USAGE.
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
