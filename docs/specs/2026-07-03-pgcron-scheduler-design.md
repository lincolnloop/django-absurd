# pg_cron scheduler (SP2) — design

Issue: [#20](https://github.com/lincolnloop/django-absurd/issues/20). Database-side
execution backend for django-absurd's recurring schedules. SP2 of #20 (SP1 = beat,
merged). Settings-declared `SCHEDULE` (shared with beat) reconciled into `pg_cron` jobs
that fire tasks via `absurd.spawn_task`. No admin UI (SP3, deferred).

Depends on the beat leading-seconds fix (merged, PR #40) only for the shared `SCHEDULE`
schema; `pg_cron` here is minute-granularity (see Cron handling).

## Goal

Declare recurring tasks in settings. Select DB-side execution with
`OPTIONS["SCHEDULER"]="pg_cron"`. A reconcile step materializes each declared entry into
(a) a row in a projection table and (b) a `pg_cron` job whose command is a **constant**
call into a wrapper function that reads the row and spawns the task. Postgres fires;
existing workers run the task. No scheduler process.

## Scope (SP2)

In: `SCHEDULER="pg_cron"` selector; a projection model (`ScheduledJob`) + a wrapper SQL
function; `sync_crons` / `teardown_crons` reconcile; `absurd_sync_crons` command +
`post_migrate` hook; static `E007` (no DB); beat/pg_cron mutual exclusion; docs.

Out: sub-minute on `pg_cron` (beat-only). Admin/DB-declared schedules (SP3 — the model
reserves a `source` column so it slots in without rework). Multi-DB topologies T2/T3.
Runtime TZ check. Retry/headers/cancellation beyond what the enqueue path resolves. A
DB-touching system check (validation happens at sync).

## Topology (T1 only)

`pg_cron` co-located with Absurd on one DB (the backend's `DATABASE`); `cron.schedule`
runs locally; the projection table + wrapper function live on that same DB. `pg_cron`'s
scheduler and its `cron.job` table live in the extension-install DB (= the Absurd DB in
T1). T2 (`schedule_in_database`) and T3 (Absurd on a non-default Django DB) are future,
isolated behind the reconcile seam.

## Architecture: projection table + wrapper function

The cron command never contains task data — only a schedule name. Data lives in a table;
a wrapper function reads it at fire time. This removes SQL-string-from-data entirely (no
`format('%L')` over args/kwargs, no injection surface).

- **`django_absurd.models.ScheduledJob`** (managed model, own migration; table
  `django_absurd_scheduledjob` in the default `public` schema). Columns: `name`,
  `source` (`"settings"` | `"admin"`, default `"settings"`), `alias` (backend alias),
  `task` (dotted path), `queue`, `params` (jsonb `{"args": …, "kwargs": …}`), `options`
  (jsonb — resolved spawn options), `cron`, `enabled` (bool), timestamps. **Unique
  together `(source, alias, name)`** — NOT `name` alone, so two backends (aliases) and
  the future admin lane can reuse a schedule name (M-A). Settings lane owns
  `source="settings"` rows; a future admin owns `source="admin"` (SP3).
- **Wrapper function** `django_absurd_run_scheduled(p_name text)` created by a `RunSQL`
  migration. It MUST be search-path-independent: define it
  `SET search_path = pg_catalog` and **fully schema-qualify every object** — reads
  `public.django_absurd_scheduledjob`, calls `absurd.spawn_task(...)` (C-B). `pg_cron`
  fires each job in a fresh background worker as the stored role with that role's
  default `search_path` (`"$user", public`), NOT the reconcile session's — an
  unqualified reference would fail invisibly into `cron.job_run_details`. Reads the row
  by name; if absent or `not enabled`, **no-op** (a pruned/disabled job can't error);
  else `select absurd.spawn_task(queue, task, params, coalesce(options, '{}'::jsonb))`.
  Fire-time reads live values, so a param edit takes effect on the next fire without
  touching `cron.job`.
- **`pg_cron` job**: name `absurd:settings:<alias>:<name>`, schedule = the (5-field)
  cron, command = a **constant** `select django_absurd_run_scheduled('<name>')` (only
  literal is the schedule name, `format('%L')`-quoted server-side; charset-restricted by
  E007). `cron.schedule` upserts by name.

## `sync_crons(backend)` (the seam)

Runs on the Absurd DB connection, inside one transaction guarded by
`pg_advisory_xact_lock(<const>)` to serialize concurrent reconcilers (parallel deploys).

1. Resolve declared entries: `get_settings_schedules(backend)`; per entry compute
   `params`, `options` (Spawn parity), effective `queue` (Effective queue), validated
   `cron`.
2. **Table**: upsert `source="settings"` rows (ORM / parameterized DML — no string SQL);
   `DELETE … WHERE source='settings' AND alias=<alias> AND name NOT IN (declared)`. The
   `source='settings'` scope means an `source='admin'` row is never touched (M-A).
3. **pg_cron**: per declared entry
   `cron.schedule('absurd:settings:<alias>:<name>', <cron>, format('select django_absurd_run_scheduled(%L)', <name>))`;
   then `cron.alter_job(jobid, active := true)` so an operator-disabled settings job is
   re-armed to match its declaration (owned policy — see Docs). Prune:
   `SELECT jobid FROM cron.job WHERE jobname LIKE 'absurd:settings:<alias>:%'` minus
   declared, then `cron.unschedule(jobid)` **each wrapped in a savepoint that swallows
   the not-found error** — `pg_cron` `ereport`s with no errcode → SQLSTATE `XX000` →
   Django `InternalError` (NOT `ProgrammingError`); catch
   `InternalError`/`DatabaseError` + message (C-1). Set-based `DELETE FROM cron.job` is
   NOT usable (`pg_cron` grants no DELETE on `cron.job`).

**Consistency invariant.** The advisory lock serializes reconcilers; it does NOT
coordinate with `pg_cron`'s independent launcher. Correctness does not rest on
transactional co-commit of rows and jobs — it rests on the **wrapper's no-op**: a job
that fires while its row is missing/disabled simply does nothing (I-A). Do not "harden"
the wrapper to raise on a missing row.

All `pg_cron` SQL confined here (local `cron.schedule` for T1; `schedule_in_database` is
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
  `sync_crons` when `SCHEDULER="pg_cron"`, else `teardown_crons`. Connect **after**
  `provision_queues_after_migrate` (N-5) so queue tables the jobs target already exist.
  Catch and skip-with-log: `ImproperlyConfigured` / `OperationalError` /
  `ProgrammingError` / `InternalError` / `ImportError` / `TypeError` (pg_cron absent,
  faked migration, bad dotted path, unserializable arg, pre-1.4 pg_cron).

`absurd_beat` / `absurd_worker --beat` raise `CommandError` when `SCHEDULER="pg_cron"`
("SCHEDULER is pg_cron — beat disabled; run `absurd_sync_crons`"). No double-fire path.

**Deploy workflow.** The intended path is "**run `migrate` on deploy**" — nothing more.
`manage.py migrate` fires `post_migrate` on **every** invocation, even with no pending
migrations, so the reconcile re-reads the current settings and applies the diff each
deploy; a settings-only `SCHEDULE` change needs no new migration. `absurd_sync_crons` is
the backstop for pipelines that skip `migrate` when no migration files changed. (No
worker-boot reconcile — `post_migrate` + the command cover it.)

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

Computed in Python at reconcile, written to the row; the wrapper passes them through.

- **`resolve_spawn_options(backend, schedule) -> dict`**: import the task;
  `defaults = getattr(task.func, "absurd_default_params", None)`;
  `merged = build_merged_spawn_options(defaults, None)` (a `Schedule` carries no
  per-call params);
  `merged["max_attempts"] = merged.pop("max_attempts", backend.default_max_attempts)`
  (I1 — the **configured** `DEFAULT_MAX_ATTEMPTS`, not literal 5; a NULL `max_attempts`
  means retry-forever in `spawn_task`, so this fallback is load-bearing). **Reshape via
  the SDK's own helpers** — `import` `absurd_sdk`'s `_normalize_spawn_options` /
  `_serialize_retry_strategy` / cancellation normalizer with a version-pin note (I-C).
  Do NOT hand-duplicate them (the `if retry_strategy:` truthiness drop + the
  `kind/base_seconds/factor/max_seconds` and `max_duration/max_delay` whitelists drift
  silently). Do NOT route through `client.spawn`: the enqueue client is `Absurd(conn)`
  with a hardcoded default and an empty registry, so its default-resolution path ignores
  `DEFAULT_MAX_ATTEMPTS` and is dead code for us (I2). No `idempotency_key` (single DB
  scheduler; beat sets one, `pg_cron` intentionally doesn't — documented asymmetry).
- **Effective queue** = `schedule.queue or import_string(task).queue_name` (N-3 — beat
  routes to the task's own `queue_name` when unset; `pg_cron` must match). The command
  has no lazy queue-create rescue (unlike `enqueue`), so E007 validates the
  **effective** queue is declared → migrate provisions its tables → fire-time INSERT
  works (N-4).

Verified:
`absurd.spawn_task(queue text, task text, params jsonb, options jsonb default '{}')`;
`options` reads `max_attempts` / `retry_strategy` / `idempotency_key` / `headers` /
`cancellation`. It is plain `plpgsql` (not `SECURITY DEFINER`), so it runs with the
fire-time role's privileges — that role is the reconcile role (`pg_cron` runs jobs as
their stored `username`), i.e. the Django app role that already enqueues, so INSERT on
the queue tables is inherently present.

## Cron handling — 5-field only, no shim

`pg_cron` backend accepts standard 5-field cron only. Sub-minute is beat-only in v1:
E007 rejects a 6-field entry under `SCHEDULER="pg_cron"` ("pg_cron backend is
minute-granularity; use the beat scheduler for sub-minute"). Drops the
croniter→`"N seconds"` shim and its costs (a `pg_cron` ≥1.5 floor, the `*`→`"1 seconds"`
runaway ~86k `cron.job_run_details` rows/day, the false "both backends behave
identically" promise). The cron string passes to `cron.schedule` unchanged; `pg_cron` is
the authoritative parser at apply time.

## Checks — static only, no DB

`manage.py check` stays DB-free; `pg_cron` facts (grammar, privilege, extension) are
validated **at sync** by the real `cron.schedule` (loud in the command; skip-with-log at
migrate). Deletes the E008 DB-probe and its failure surface.

- **`E007`** (`@register("absurd")`, static): per `SCHEDULE` entry — task imports to a
  Django `Task`; `cron` is croniter-parseable and 5-field (reject 6-field under
  `pg_cron`); `args`/`kwargs` JSON-serializable; `SCHEDULER` value ∈ the known set (a
  typo like `"pgcron"` must not silently fall through to beat + teardown); schedule
  `name` matches `[A-Za-z0-9_-]+`; the **composed jobname**
  `absurd:settings:<alias>:<name>` — including the operator-controlled `alias` — matches
  a safe charset and is **≤ 63 bytes** (`cron.job.jobname` is Postgres `name`, silently
  truncated past 63 → the entry is pruned-right-after-create every sync and never fires,
  C-2 / M-B); effective `queue` is declared. All offline.

No `E008`, no dry-run, no DB-tagged check.

## Timezone

Docs-only. `pg_cron` fires in `cron.timezone` (GMT default, global GUC, not per-job) —
differs from beat's Django `TIME_ZONE` local time. Document: (a) state the GMT-native
default; (b) recommend `cron.timezone` = Django `TIME_ZONE` when non-UTC. Common
UTC-both case = no-op. The emitted command carries no timestamp (`spawn_task` stamps
server-side), so no payload/slot TZ mismatch. Runtime check deferred.

## Privileges & version

- **`pg_cron` ≥ 1.4 required** — `cron.alter_job` (used every sync to re-arm `active`)
  was added in **1.4**, not 1.3 (C-A). No ≥1.5 need (no seconds). Not statically
  checkable (checks are DB-free) → a too-old `pg_cron` fails at sync (loud in the
  command; skip-with-log at migrate). Document the floor.
- Reconcile role needs: EXECUTE on `cron.schedule` / `cron.unschedule` /
  `cron.alter_job` (`pg_cron`-privileged role or superuser), plus ownership of the
  projection table + wrapper function (created by our migration as that role). Fire-time
  = same role.
- **M1 (constraint):** `cron.schedule` upserts on `(jobname, username)` and jobs run as
  the stored `username`. Two projects sharing one DB under **different** roles get
  duplicate firing (no upsert collision); RLS (`username = current_user`) also hides the
  other role's rows from our prune, so we can't clean them up. Reconcile must run as a
  single stable role. Documented; a configurable namespace is a future knob.

## Testing

Function-based pytest, behavior-driven, against real `pg_cron`.
`CREATE EXTENSION pg_cron` is only allowed in `cron.database_name`, and the suite uses a
dynamically-named `test_*` DB — so the compose Postgres needs a concrete answer, not a
hand-wave:

- Build/extend the DB image to a `pg_cron`-enabled Postgres and start it with
  `command: postgres -c shared_preload_libraries=pg_cron -c cron.database_name=<db>`.
- Reconcile the dynamic-test-DB-name vs static-`cron.database_name` GUC clash: use a
  **fixed** test DB name (pytest-django `DATABASES["default"]["TEST"]["NAME"]`) set to
  the same value as the GUC, or move the `pg_cron` end-to-end tests to a **dedicated CI
  job/service**. Pin this in the plan.

Cases (RED-first):

- `resolve_spawn_options`: `@absurd_default_params(max_attempts=3)` → `options` has 3; a
  task with **no** default under a backend with `DEFAULT_MAX_ATTEMPTS=7` → `options` has
  **7** (I1, the literal-5 bug can't pass); effective queue = task's `queue_name` when
  `queue` unset (N-3).
- `sync_crons` (real `pg_cron`): assert `ScheduledJob` rows AND `cron.job` rows
  (jobname, schedule, constant command). Idempotent. Prune: drop an entry, re-sync → its
  row + job gone; a hand-made non-prefixed `cron.job` survives; an `source='admin'` row
  survives a settings sync. Prune-tolerance: reconcile a set where a job was already
  removed → no error (savepoint-swallow). Operator-disabled job re-armed on sync.
- **No-op invariant (I-A):** a `cron.job` committed with no matching row (or a disabled
  row) → the fire is a clean no-op, nothing in `cron.job_run_details` as an error.
- `teardown_crons`: switch `SCHEDULER` to beat → `post_migrate` removes all
  `absurd:settings:<alias>:*` jobs + rows. Idempotent.
- `post_migrate`: reconcile under `pg_cron`; teardown when switched away; runs **after**
  provision; extension absent / bad dotted path → skips, `migrate` succeeds (N-2/N-5).
- **Injection:** a schedule whose `args` contain `"'; drop schema absurd cascade; --"`
  and `"$$"` — the value lands in `ScheduledJob.params` via DML and fires as literal
  data; `cron.job.command` is the constant wrapper call; schema `absurd` intact.
- E007 rejects: 6-field under `pg_cron`; unknown `SCHEDULER`; over-63-byte or
  bad-charset composed jobname (incl. a long/odd `alias`); undeclared effective queue —
  full text.
- beat commands raise `CommandError` under `SCHEDULER="pg_cron"`.
- End-to-end: sync a `* * * * *` schedule, let `pg_cron` fire, worker burst, assert task
  ran.

## Docs

`docs/web/cron-jobs.md` Database-side section → real: enable extension (**≥ 1.4**),
`SCHEDULER="pg_cron"`, `absurd_sync_crons` (+ auto on migrate), TZ note (both framings),
**sub-minute = beat-only**, mutual exclusion, the single-stable-role constraint. Two
loud callouts:

- **Kill switch = the schedule, not `cron.alter_job`.** Every reconcile re-arms
  settings-owned jobs (`active := true`), so disabling a job with `cron.alter_job` is
  reverted on the next deploy/migrate. To stop a job, set `enabled=false` in / remove it
  from `SCHEDULE` (I-B).
- **Uninstall is not self-cleaning.** Removing the app / flipping to beat without
  running `migrate` leaves jobs firing — run `absurd_sync_crons --teardown` first. Also
  recommend a `cron.job_run_details` purge job (it's the only fire-failure surface).

`AGENTS.md`: `pg_cron` backend, selector, reconcile, wrapper model. README unchanged.
WHY.md: capture the projection-table / constant-command rationale after build.

## Decisions (resolved in brainstorming + 4 review rounds)

- **Settings-declared** source of truth (not admin). SP3 (admin) deferred; the
  `ScheduledJob.source` column + `absurd:settings:` / `absurd:admin:` name split reserve
  the coexistence seam — a future admin lane writes `source="admin"` rows that reconcile
  reads but `sync_crons` never clobbers (scoped by `source`).
- **Projection table + wrapper function** (not inline commands). Task data lives in a
  row; the cron command is constant. Deletes the SQL-string-from-data injection surface
  (C1); param edits don't churn `cron.job`; parity computed once at reconcile.
- **Wrapper is search-path-independent** (`SET search_path`, fully schema-qualified) —
  else fires fail invisibly under the fire-time role's default path (C-B).
- **Static checks only; validate at sync.** `manage.py check` never touches the DB; the
  real `cron.schedule` is the authoritative gate. Removes E008 (+ its I3/I4/N-1
  pitfalls).
- **No sub-minute on `pg_cron`** — beat-only; drops the shim + its floor and footguns.
- **Reconcile on migrate AND command**, same core; loud in the command, best-effort at
  migrate; advisory-lock serialized; per-entry savepoint isolation. Deploy = "migrate on
  deploy" (`post_migrate` fires every run); command is the skip-migrate backstop.
- **Prune/teardown by jobid with savepoint-swallow** (C-1: both `cron.unschedule`
  overloads raise on missing; tolerance is error-handling, not id-vs-name; catch
  `InternalError`/XX000). No raw `DELETE` (no grant).
- **Correctness rests on the wrapper's no-op, not txn co-commit** (I-A).
- **Spawn parity** uses configured `default_max_attempts` + decorator + the **imported**
  SDK normalizers (I1/I2/I-C), not literal 5 and not the dead SDK-render path.
- **Effective queue = `schedule.queue or task.queue_name`**, E007-validated declared
  (N-3/N-4).
- **Composite unique `(source, alias, name)`; jobname ≤ 63 bytes, name+alias
  charset-restricted** (M-A / C-2 / M-B).
- **`pg_cron` ≥ 1.4** (C-A). **Re-arm `active` every sync = owned policy; kill via
  `SCHEDULE`** (I-B). **Single stable reconcile role** (M1).
- **T1 only** — seam isolates the future `schedule_in_database` swap.

## Decomposition (future)

- **SP3 — admin/DB-declared schedules.** Reuses the `ScheduledJob` model
  (`source="admin"` rows); adds the admin UI + the task-input problem (Django Tasks has
  no registry — the dropdown/discovery design shelved earlier). Reconcile already reads
  all sources; only the editing surface + precedence are new. **Admin rows own their
  `options` verbatim** — `sync_crons` recomputes `options` only for `source="settings"`,
  so an admin row's stored `options` is not auto-refreshed when a decorator /
  `DEFAULT_MAX_ATTEMPTS` changes (M-C); the admin form owns that.
- Multi-DB topologies (T2 designated cron DB via `schedule_in_database`; T3 Absurd on a
  non-default Django DB).
- Runtime TZ check (warn when `cron.timezone` ≠ Django `TIME_ZONE`).
- Sub-minute on `pg_cron` (the `"N seconds"` shim) if demanded.
