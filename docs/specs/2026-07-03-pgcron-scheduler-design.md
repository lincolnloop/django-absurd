# pg_cron scheduler (SP2) — design

Issue: [#20](https://github.com/lincolnloop/django-absurd/issues/20). Database-side
execution backend for django-absurd's recurring schedules. SP2 of #20 (SP1 = beat,
merged). Settings-declared `SCHEDULE` (shared with beat) reconciled into pg_cron jobs
that fire tasks via `absurd.spawn_task`. No admin UI (SP3, deferred).

Depends on the beat leading-seconds fix (merged, PR #40) only for the shared `SCHEDULE`
schema; pg_cron itself is minute-granularity here (see Sub-minute).

## Goal

Declare recurring tasks in settings. Select DB-side execution with
`OPTIONS["SCHEDULER"]="pg_cron"`. A reconcile step materializes each declared entry into
(a) a row in a projection table and (b) a pg_cron job whose command is a **constant**
call into a wrapper function that reads the row and spawns the task. Postgres fires;
existing workers run the task. No scheduler process.

## Scope (SP2)

In: `SCHEDULER="pg_cron"` selector; a **projection model** (`ScheduledJob`) + a
**wrapper SQL function**; `sync_crons` / `teardown_crons` reconcile; `absurd_sync_crons`
command + `post_migrate` hook; **static** `E007` (no DB); beat/pg_cron mutual exclusion;
docs.

Out: sub-minute on pg_cron (beat-only — see Sub-minute). Admin/DB-declared schedules
(SP3 — the projection model reserves a `source` column so it slots in without rework).
Multi-DB topologies T2/T3. Runtime TZ check. Retry/headers/cancellation beyond what the
enqueue path already resolves. A DB-touching system check (validation happens at sync).

## Topology (T1 only)

pg_cron co-located with Absurd on one DB (the backend's `DATABASE`); `cron.schedule`
runs locally; the projection table + wrapper function live on that same DB. pg_cron's
scheduler and its `cron.job` table live in the extension-install DB (= the Absurd DB in
T1). T2 (`schedule_in_database`) and T3 (Absurd on a non-default Django DB) are future,
isolated behind the reconcile seam.

## Architecture: projection table + wrapper function

The cron command never contains task data — only a schedule name. Data lives in a table;
a wrapper function reads it at fire time. This removes SQL-string-from-data entirely (no
`format('%L')` over args/kwargs, no injection surface).

- **`django_absurd.models.ScheduledJob`** (managed model, own migration; table
  `django_absurd_scheduledjob`). Columns: `name` (unique), `source` (`"settings"` |
  `"admin"`, default `"settings"`), `alias` (backend alias), `task` (dotted path),
  `queue`, `params` (jsonb `{"args": …, "kwargs": …}`), `options` (jsonb — resolved
  spawn options), `cron` (the schedule string), `enabled` (bool), timestamps. For this
  SP the settings lane owns `source="settings"` rows; a future admin owns
  `source="admin"` (SP3).
- **Wrapper function** `django_absurd_run_scheduled(p_name text)` (public schema,
  created by a `RunSQL` migration). Reads the row by name; if absent or `not enabled`,
  **no-op** (a pruned/disabled job can't error); else
  `select absurd.spawn_task(queue, task, params, options)`. Fire-time reads live values,
  so a param edit takes effect on the next fire without touching `cron.job`.
- **pg_cron job**: name `absurd:settings:<alias>:<name>`, schedule = the (5-field) cron,
  command = a **constant** `select django_absurd_run_scheduled('<name>')` (the only
  literal is the schedule name, built server-side with `format('%L')` — one controlled,
  charset-restricted value; see E007). `cron.schedule` upserts by name.

## `sync_crons(backend)` (the seam)

Runs on the Absurd DB connection, inside one transaction guarded by
`pg_advisory_xact_lock(<const>)` to serialize concurrent deploys.

1. Resolve declared entries: `get_settings_schedules(backend)`; per entry compute
   `params`, `options` (see Spawn parity), effective `queue` (see Effective queue),
   validated `cron`.
2. **Table**: upsert `source="settings"` rows (ORM / parameterized DML — no string SQL);
   `DELETE … WHERE source='settings' AND alias=<alias> AND name NOT IN (declared)`.
3. **pg_cron**: per declared entry
   `cron.schedule('absurd:settings:<alias>:<name>', <cron>, format('select django_absurd_run_scheduled(%L)', <name>))`.
   Prune: `SELECT jobid FROM cron.job WHERE jobname LIKE 'absurd:settings:<alias>:%'`
   minus declared, then `cron.unschedule(jobid)` **each wrapped in a savepoint that
   swallows the not-found error** (pg_cron `ereport`s with no errcode → SQLSTATE `XX000`
   → Django `InternalError`, NOT `ProgrammingError` — catch
   `InternalError`/`DatabaseError` + message, C-1). Set-based `DELETE FROM cron.job` is
   NOT usable — pg_cron grants no DELETE on `cron.job`. Assert `active` on upserted jobs
   (`cron.alter_job(jobid, active := true)`) so an operator-disabled settings job is
   re-enabled to match its declaration.

All pg_cron SQL confined here (local `cron.schedule` for T1; `schedule_in_database` is
the future swap point).

`teardown_crons(backend)` — unschedule every `absurd:settings:<alias>:%` job (same
savepoint-swallow loop) and delete `source="settings"` rows. Idempotent.

## Triggers & error posture

Both call the same `sync_crons`; they differ in how failure surfaces.

- **`absurd_sync_crons`** (management command) — **loud**. A bad cron / missing
  extension / missing privilege is reported per entry; hard failures raise
  `CommandError`. Refuses unless `SCHEDULER="pg_cron"`, except `--teardown` (allowed, to
  clean up after switching away).
- **`post_migrate`** (in `apps.py`) — **best-effort**, must never break `migrate`. Runs
  `sync_crons` when `SCHEDULER="pg_cron"`, else `teardown_crons` (removes orphans after
  a switch to beat). Connect **after** `provision_queues_after_migrate` (N-5) so queue
  tables the jobs target already exist. Catch and skip-with-log:
  `ImproperlyConfigured/OperationalError/ProgrammingError/InternalError/ImportError/TypeError`
  (pg_cron absent, faked migration, bad dotted path, unserializable arg, pre-1.3
  pg_cron).

`absurd_beat` / `absurd_worker --beat` raise `CommandError` when `SCHEDULER="pg_cron"`
("SCHEDULER is pg_cron — beat disabled; run `absurd_sync_crons`"). No double-fire path.

**Deploy workflow.** The intended path is "**run `migrate` on deploy**" — nothing more.
`manage.py migrate` fires `post_migrate` on **every** invocation, even with no pending
migrations, so the reconcile re-reads the _current_ settings and applies the diff on
each deploy; a settings-only `SCHEDULE` change needs no new migration.
`absurd_sync_crons` is the backstop for pipelines that _skip_ `migrate` when no
migration files changed. (No worker-boot reconcile — `post_migrate` + the command cover
it.)

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
                    "cron": "0 2 * * *",  # 5-field only under pg_cron
                },
            },
        },
    },
}
```

`SCHEDULER` ∈ `{"beat"(default), "pg_cron"}`, exactly one per backend. `SCHEDULE` schema
identical to beat: required `task`, `cron`; optional `queue`, `args`, `kwargs`.

## Spawn parity (options + queue)

Computed in Python at reconcile, written to the row; the wrapper just passes them
through.

- **`resolve_spawn_options(backend, schedule) -> dict`**: import the task;
  `defaults = getattr(task.func, "absurd_default_params", None)`;
  `merged = build_merged_spawn_options(defaults, None)` (no per-call params — a
  `Schedule` carries none);
  `merged["max_attempts"] = merged.pop("max_attempts", backend.default_max_attempts)`
  (I1 — the **configured** `DEFAULT_MAX_ATTEMPTS`, NOT literal 5; a NULL `max_attempts`
  means retry-forever in Absurd, so this fallback is load-bearing); reshape
  `retry_strategy` / `cancellation` the way the SDK's `_normalize_spawn_options` does
  (import the private helpers or duplicate ~15 lines with a version-pin note — do NOT
  route through `client.spawn`: the enqueue client has no `default_max_attempts` and an
  empty registry, so the SDK-render path is dead code, I2). No `idempotency_key` (single
  DB scheduler; beat sets one, pg_cron intentionally doesn't — documented asymmetry).
- **Effective queue** = `schedule.queue or import_string(task).queue_name` (N-3 — beat
  routes to the task's own `queue_name` when unset; pg_cron must match). The pg_cron
  command has no lazy queue-create rescue (unlike `enqueue`), so E007 validates the
  **effective** queue is declared → migrate provisions its tables → fire-time INSERT
  works (N-4).

Verified:
`absurd.spawn_task(queue text, task text, params jsonb, options jsonb default '{}')`;
`options` reads `max_attempts/retry_strategy/idempotency_key/headers/cancellation`. It
is plain `plpgsql` (not `SECURITY DEFINER`), so it runs with the fire-time role's
privileges — that role is the reconcile role (pg_cron runs jobs as their stored
`username`), i.e. the Django app role that already enqueues, so INSERT on the queue
tables is inherently present.

## Cron handling — 5-field only, no shim (B-3)

pg_cron backend accepts **standard 5-field** cron only. Sub-minute is **beat-only** in
v1: E007 rejects a 6-field entry under `SCHEDULER="pg_cron"` ("pg_cron backend is
minute-granularity; use the beat scheduler for sub-minute"). This drops the
croniter→`"N seconds"` translation shim entirely and its costs: the pg_cron ≥1.5 floor,
the `*`→`"1 seconds"` runaway (~86k `job_run_details` rows/day), and the false "both
backends behave identically" promise. The cron string passes through to `cron.schedule`
unchanged; pg_cron is the authoritative parser at apply time.

## Checks — static only, no DB (Option A)

`manage.py check` stays DB-free for this feature; pg_cron facts (grammar, privilege,
extension) are validated **at sync** by the real `cron.schedule` (loud in the command;
skip-with-log at migrate). This deletes the E008 DB-probe and its whole failure surface
(wrong function signature, INSERT-probe on not-yet-created tables, catch-set).

- **`E007`** (`@register("absurd")`, static): per `SCHEDULE` entry — task imports to a
  Django `Task`; `cron` is croniter-parseable and **5-field** (reject 6-field under
  pg*cron per B-3); `args`/`kwargs` JSON-serializable; `SCHEDULER` value ∈ known set (a
  typo like `"pgcron"` must not silently fall through to beat + teardown); **schedule
  name** matches
  `[A-Za-z0-9*-]+`(keeps the constant command literal safe and predictable); the composed jobname`absurd:settings:<alias>:<name>` is **≤ 63 bytes** (`cron.job.jobname`is Postgres`name`; over-63 truncates silently → the entry is pruned-right-after-create every sync and never fires, C-2); effective `queue`
  (above) is declared. All offline.

No `E008`, no dry-run, no DB-tagged check.

## Timezone

Docs-only. pg_cron fires in `cron.timezone` (GMT default, global GUC, not per-job) —
differs from beat's Django-`TIME_ZONE` local time. Document: (a) state the GMT-native
default; (b) recommend `cron.timezone` = Django `TIME_ZONE` when non-UTC. Common
UTC-both case = no-op. The emitted command carries no timestamp (`spawn_task` stamps
server-side), so no payload/slot TZ mismatch. Runtime check deferred.

## Privileges & version

- pg_cron **≥ 1.3** required (named `cron.schedule(name,text,text)` upsert). No ≥1.5
  need (no seconds). Not statically checkable (Option A) → a pre-1.3 `cron.schedule`
  fails at sync (loud in the command; skip-with-log at migrate). Document the floor.
- Reconcile role needs: EXECUTE on `cron.schedule`/`cron.unschedule`/`cron.alter_job`
  (pg_cron-privileged role or superuser), plus ownership of the projection table +
  wrapper function (created by our migration as that role). Fire-time = same role.
- **M1 (constraint):** `cron.schedule` upserts on `(jobname, username)` and jobs run as
  the stored `username`. Two projects sharing one DB under **different** roles get
  duplicate firing (no upsert collision); RLS (`username = current_user`) also hides the
  other role's rows from our prune, so we can't clean them up. Reconcile must run as a
  single stable role. Documented; a configurable namespace is a future knob.

## Testing

Function-based pytest, behavior-driven, real pg*cron. Because `CREATE EXTENSION pg_cron`
is only allowed in `cron.database_name` and the suite uses a runner-created
`test*\*`DB, the compose Postgres must set`cron.database_name` to the test DB (or the
pg_cron e2e runs in a dedicated job) — pin this explicitly, don't hand-wave.

- `resolve_spawn_options`: `@absurd_default_params(max_attempts=3)` → `options` has 3; a
  task with **no** default under a backend with `DEFAULT_MAX_ATTEMPTS=7` → `options` has
  **7** (I1 — the literal-5 bug can't pass); effective queue = task's `queue_name` when
  `queue` unset (N-3).
- `sync_crons` (behavioral, real pg_cron): run, assert `ScheduledJob` rows AND
  `cron.job` rows (jobname, schedule, constant command). Idempotent (re-run = same
  rows). Prune: drop a declared entry, re-sync → its row + job gone; a hand-made
  non-prefixed `cron.job` survives. **Prune tolerance:** reconcile a set where a job was
  already removed → no error (savepoint-swallow). `enabled=false`/operator-disabled job
  re-enabled on sync.
- `teardown_crons`: switch `SCHEDULER` to beat → `post_migrate` removes all
  `absurd:settings:<alias>:*` jobs + rows (C2). Idempotent.
- `post_migrate`: reconcile under pg_cron; teardown when switched away; runs **after**
  provision (queue tables exist); pg_cron/extension absent or bad dotted path → skips,
  `migrate` succeeds (N-2/N-5).
- **Injection:** a schedule whose `args` contain `"'; drop schema absurd cascade; --"`
  and `"$$"` — the value lands in the `ScheduledJob.params` column via DML and fires as
  literal data; `cron.job.command` is the constant wrapper call; schema `absurd` intact.
- **Wrapper no-op:** delete a row out from under an existing job → the next fire is a
  no-op (no error in `job_run_details`).
- E007 rejects: 6-field under pg_cron; unknown `SCHEDULER`; over-63-byte jobname; bad
  name charset; undeclared effective queue — full text per entry.
- beat commands raise `CommandError` under `SCHEDULER="pg_cron"`.
- End-to-end: sync a `* * * * *` schedule, let pg_cron fire, worker burst, assert task
  ran.

## Docs

`docs/web/cron-jobs.md` Database-side section → real: enable extension (≥1.3),
`SCHEDULER="pg_cron"`, `absurd_sync_crons` (+ auto on migrate), TZ note (both framings),
**sub-minute = beat-only**, mutual exclusion, the single-stable-role constraint, and a
note that **uninstall is not self-cleaning** (removing the app / flipping to beat
without `migrate` leaves jobs — run `absurd_sync_crons --teardown` first) plus a
`cron.job_run_details` purge recommendation (it's the only fire-failure surface).
`AGENTS.md`: pg_cron backend, selector, reconcile, wrapper model. README unchanged.
WHY.md: capture the projection-table / constant-command rationale after build.

## Decisions (resolved in brainstorming + 3 review rounds)

- **Settings-declared** source of truth (not admin). SP3 (admin) deferred; the
  `ScheduledJob.source` column + `absurd:settings:` / `absurd:admin:` name split reserve
  the coexistence seam — a future admin lane writes `source="admin"` rows that reconcile
  reads but `sync_crons` never clobbers.
- **Projection table + wrapper function** (not inline commands). Moves task data out of
  the cron command into a row; command is constant. Deletes the SQL-string-from-data
  injection surface (C1) rather than mitigating it; param edits don't churn `cron.job`;
  parity is computed once at reconcile.
- **Static checks only; validate at sync** (Option A). `manage.py check` never touches
  the DB; the real `cron.schedule` is the authoritative gate. Removes E008 + I3/I4/N-1.
- **No sub-minute on pg_cron (B-3)** — beat-only; drops the shim + its version floor and
  footguns.
- **Reconcile on migrate AND command**, same core; loud in the command, best-effort at
  migrate; advisory-lock serialized; per-entry savepoint isolation.
- **Prune/teardown by jobid with savepoint-swallow** (C-1: both `cron.unschedule`
  overloads raise on missing; tolerance comes from error-handling, not id-vs-name; catch
  `InternalError`/XX000). No raw `DELETE` (no grant).
- **Spawn parity uses the configured `default_max_attempts` + decorator + SDK
  normalization** (I1/I2), not literal 5 and not the dead SDK-render path.
- **Effective queue = `schedule.queue or task.queue_name`**, E007-validated as declared
  (N-3/N-4).
- **Jobname ≤ 63 bytes, name charset-restricted** (C-2, injection-safety of the constant
  command).
- **Single stable reconcile role** (M1: `(jobname,username)` upsert + RLS).
- **T1 only** — seam isolates the future `schedule_in_database` swap.

## Decomposition (future)

- **SP3 — admin/DB-declared schedules.** Reuses the `ScheduledJob` model
  (`source="admin"` rows); adds the admin UI + the task-input problem (Django Tasks has
  no registry — the dropdown/discovery design shelved earlier). Reconcile already reads
  all sources; only the editing surface + precedence are new.
- Multi-DB topologies (T2 designated cron DB via `schedule_in_database`; T3 Absurd on a
  non-default Django DB).
- Runtime TZ check (warn when `cron.timezone` ≠ Django `TIME_ZONE`).
- Sub-minute on pg_cron (the `"N seconds"` shim) if demanded.
