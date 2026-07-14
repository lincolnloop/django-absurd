# django-absurd — integration guide

A guide for developers integrating **django-absurd** into a Django project. This file
ships inside the installed package (`site-packages/django_absurd/AGENTS.md`), so it
stays discoverable from a project's virtualenv (and by coding agents working there).

django-absurd plugs [Absurd](https://earendil-works.github.io/absurd/), a
Postgres-native workflow engine, into Django's Tasks framework. It reuses Django's
database connection and ships Absurd's schema as Django migrations — no separate broker.

**Runnable examples** live in the repo's
[`examples/`](https://github.com/lincolnloop/django-absurd/tree/main/examples) — three
single-file [nanodjango](https://github.com/radiac/nanodjango) demos, each
`docker compose up`: `web` (enqueue + result), `beat` (beat scheduler), and `pg_cron`
(pg_cron scheduler).

## Hard requirements

- **Python 3.12+**, **Django 6.0+**.
- **PostgreSQL via the psycopg (v3) Django backend** — `django.db.backends.postgresql`
  with psycopg3 installed. The Absurd SDK reuses Django's connection; psycopg2 will not
  work. The package asserts this at runtime; do not work around it.

## Configure

Add the app and point Django's `TASKS` setting at the backend:

```python
INSTALLED_APPS = [
    # ...
    "django_absurd",
]

TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default"],  # optional — defaults to ["default"]
    },
}
```

`QUEUES` is optional: omit it to use just the `"default"` queue. List names here for
additional queues, or use `OPTIONS["QUEUES"]` (below) to set per-queue policy.

Backend `OPTIONS` (all optional):

- `DATABASE` — which `DATABASES` alias to use (default: `"default"`).
- `DEFAULT_MAX_ATTEMPTS` — retry ceiling per task (default: `5`).
- `QUEUES` — a map of queue name → `absurd_sdk.CreateQueueOptions` for per-queue config.
  Use this _instead of_ the top-level `QUEUES` list (which only names queues) — declare
  queues in one place or the other, never both (setting both is a configuration error).
- `ENABLE_ADMIN` — register Absurd models in Django admin (default: `True`). Set to
  `False` to disable.
- `ADMIN_SITE` — tuple of dotted paths to `AdminSite` instances to register on (default:
  `("django.contrib.admin.site",)`).

Only when you point `DATABASE` at a **non-default** alias, also register the router so
django-absurd's schema and queries route to that database:

```python
DATABASE_ROUTERS = ["django_absurd.routers.AbsurdRouter"]
```

## Run

```bash
python manage.py migrate              # apply Absurd's schema + provision declared queues
python manage.py absurd_worker        # run a worker
```

`migrate` provisions everything: a `post_migrate` handler runs `sync_queues`, creating
the declared queues and (re)building the admin views to match. A worker does the same
full sync on start, and `absurd_sync_queues` runs it on demand (also reconciling
per-queue policy changes). Declared queues are additionally auto-created on first
enqueue. Only queues declared in `QUEUES` are created; an undeclared queue name is
rejected.

## Admin introspection

When `django.contrib.admin` is in `INSTALLED_APPS`, django-absurd automatically
registers six read-only admin entries: **Tasks**, **Runs**, **Checkpoints**, **Events**,
and **Waits** (each spanning all queues via a UNION-ALL view, filterable by queue) plus
the **Queues** catalog. No configuration required; the list views stay in sync with the
live queue catalog.

To disable: set `OPTIONS["ENABLE_ADMIN"] = False`. To register on a custom admin site:
set `OPTIONS["ADMIN_SITE"]` to a tuple of dotted paths, e.g.
`("myapp.admin.custom_site",)`.

A queue created only by an enqueue (no worker started, no sync run) is not yet part of
the admin views, so its tasks won't appear. The changelist detects this and shows a
warning naming the unindexed queue(s) and pointing you to `absurd_sync_queues`; running
that command (or starting a worker on the queue) indexes them.

**Non-default `DATABASE`:** when Absurd lives on a database other than `"default"`, the
synthesized models read from the Absurd DB while Django's `LogEntry`, sessions, and
`ContentType` tables must still be present in `"default"` (run `migrate` on it).

## Querying queue state (ORM)

The same read-only models the admin uses are public:

```python
from django_absurd.models import Task, Run, Checkpoint, Event, Wait, Queue

Task.objects.filter(queue="reports", state="failed")
Task.objects.get(queue="reports", task_id=task_id)
```

`Task`, `Run`, `Checkpoint`, `Event`, and `Wait` are ordinary chainable Django models —
`.filter()`, `.exclude()`, `.order_by()`, `.count()`, slicing all work. Each spans every
queue (a `UNION ALL` over the per-queue tables) and carries a synthesized **`queue`**
column identifying the source queue. They are **read-only**: `save()`/`delete()` raise
`QueueReadOnlyError`. `Queue` is the queue catalog (`queue_name` is its key).

These models are backed by Postgres views, (re)built by `migrate` (post_migrate), worker
start, and `absurd_sync_queues`. A queue that appears only afterwards — e.g. declared
after the last migrate and reached by an enqueue before the next migrate/worker/sync —
is absent from results until the next provisioning step; the admin changelist flags this
with a warning. Dropping a queue (`drop_queue`) removes its view; re-provision to
rebuild.

**Performance.** The views have no cross-queue index. Filtering by **`queue=`** prunes
to a single per-queue table — fast. An unfiltered query (e.g. ordering by `enqueue_at`
or filtering only on `state` across all queues) scans every queue's table. On large
multi-queue deployments, scope queries with `queue=` whenever you can.

## Validate

Run `python manage.py check django_absurd` and resolve everything it reports before
relying on the setup. Fix the configuration it points at rather than silencing the
check.

System check IDs:

- `absurd.E001` — backend/DB misconfiguration.
- `absurd.E002` — `QUEUES` declared in both top-level and `OPTIONS`.
- `absurd.E003` — invalid per-queue policy options.
- `absurd.E004` — multiple Absurd backends targeting different databases.
- `absurd.E005` — `AbsurdRouter` missing from `DATABASE_ROUTERS`.
- `absurd.E006` — `ENABLE_ADMIN` is not a bool, or `ADMIN_SITE` paths don't resolve to
  `AdminSite` instances.
- `absurd.E007` — invalid `SCHEDULE` entry (bad task path, bad cron expression, unknown
  key, non-serializable args/kwargs, or undeclared queue). See
  [Scheduling recurring tasks](#scheduling-recurring-tasks).
- `absurd.E008` — `SCHEDULER="pg_cron"` is configured but `"django_absurd.pg_cron"` is
  not in `INSTALLED_APPS`. See [pg_cron backend](#pg_cron-backend).
- `absurd.W003` (Warning) — `"django_absurd.pg_cron"` is in `INSTALLED_APPS` but ordered
  before `"django_absurd"`, causing its `post_migrate` cron reconcile to run before
  queue provisioning. See [pg_cron backend](#pg_cron-backend).

## Defining and enqueuing tasks

Use Django's Tasks API. Tasks may be **sync (`def`) or async (`async def`)** — one
worker runs both, and `async def` tasks may use Django's async ORM. Tasks are resolved
by import path, so they can live in any importable module (no `tasks.py` requirement).

Enqueuing rides the surrounding Django transaction — a task spawned inside `atomic()` is
rolled back if the block fails (enqueue-on-commit, automatic).

Absurd parameters attach two ways — both live in `django_absurd.params`:

- **Per-task defaults** — the `@absurd_default_params(...)` decorator, applied _below_
  `@task` (applying it above a `Task` raises `TypeError`):

  ```python
  from django.tasks import task
  from django_absurd.params import absurd_default_params

  @task
  @absurd_default_params(max_attempts=3)
  def send_report(...): ...
  ```

- **Per-invocation** — pass an `AbsurdSpawnParams` as the `absurd_spawn_params` kwarg to
  `.enqueue()`:

  ```python
  from django_absurd.params import AbsurdSpawnParams

  send_report.enqueue(..., absurd_spawn_params=AbsurdSpawnParams(idempotency_key="abc"))
  ```

Parameter fields (see `django_absurd.params`): `max_attempts`, `retry_strategy`,
`cancellation` (defaults and per-call), plus `headers` and `idempotency_key` (per-call
only). Field types come from `absurd_sdk` (`RetryStrategy`, `CancellationPolicy`,
`JsonObject`). Backend capabilities: result retrieval is supported; async enqueue,
defer, and priority are not.

## Workers

```bash
python manage.py absurd_worker            # consumes the "default" queue
python manage.py absurd_worker --queue reports
```

A single worker runs **both** sync and async tasks: `async def` tasks run on an event
loop (true concurrency for I/O-bound work), sync `def` tasks run in a thread pool. On
start it runs a full sync — reconciling **every** declared queue (creating missing ones,
applying declared policy changes) and rebuilding the admin views so they reflect the
whole catalog, not just the served queue — and reports to stdout.

- **Blocking** (default): long-running; polls until `SIGINT`/`SIGTERM`.
- **Burst** (`--burst`): drain the current backlog, then exit `0` (cron / one-shot).
- `--queue` (default `"default"`): which queue to consume.
- `--concurrency N` (default `1`): max tasks in flight — sizes both the event-loop
  concurrency and the sync thread pool. Other flags: `--claim-timeout`,
  `--poll-interval`, `--batch-size`, `--worker-id`, and `--alias` (required only when
  several Absurd backends are configured).

## Scheduling recurring tasks

django-absurd supports two schedulers, selected per backend with `OPTIONS["SCHEDULER"]`:

| Value       | Default | Description                                                      |
| ----------- | ------- | ---------------------------------------------------------------- |
| `"beat"`    | yes     | In-process beat; evaluates cron and enqueues via the normal path |
| `"pg_cron"` | no      | Database-side; Postgres fires jobs directly via `pg_cron`        |

### Declare schedules

Add a `SCHEDULE` map to `OPTIONS`. The schema is the same for both schedulers:

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            # "SCHEDULER": "beat",   # default; omit for beat
            "SCHEDULE": {
                "nightly-report": {
                    "task": "myapp.tasks.generate_report",  # dotted import path
                    "cron": "0 2 * * *",                   # cron expression (see table)
                },
                "hourly-cleanup": {
                    "task": "myapp.tasks.cleanup",
                    "cron": "0 * * * *",
                    "queue": "low-priority",               # optional; must be a declared queue
                    "args": [30],                          # optional positional args
                    "kwargs": {"dry_run": False},          # optional keyword args
                },
            },
        },
    },
}
```

**Spec keys:**

| Key      | Required | Description                                                                                                                                                                                                                                                                            |
| -------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `task`   | yes      | Dotted import path to a `@task`-decorated function                                                                                                                                                                                                                                     |
| `cron`   | yes      | Cron expression, parsed by [croniter](https://pypi.org/project/croniter/): standard **5-field** `min hour dom mon dow` (e.g. `"0 2 * * *"`), or **6-field** with a leading seconds column for sub-minute cadences (e.g. `"*/30 * * * * *"` = every 30s) (beat only — see pg_cron note) |
| `queue`  | no       | Queue name; omit to use the backend's default queue. Must be a declared queue (see Configure), else `check` reports `absurd.E007`                                                                                                                                                      |
| `args`   | no       | List of positional arguments passed to the task on each firing                                                                                                                                                                                                                         |
| `kwargs` | no       | Dict of keyword arguments passed to the task on each firing                                                                                                                                                                                                                            |

### Beat scheduler

Cron expressions are evaluated in Django's configured
[`TIME_ZONE`](https://docs.djangoproject.com/en/stable/ref/settings/#time-zone).
Sub-minute (6-field) schedules are supported; each slot enqueues a task, so size the
cadence to what your worker can keep up with.

Start the beat scheduler as a standalone process:

```bash
python manage.py absurd_beat
```

Or run it co-located with a worker (saves a process in simple deployments):

```bash
python manage.py absurd_worker --beat
```

**Per-slot idempotency.** Each scheduled spawn carries an idempotency key — a
`cron:`-prefixed SHA-256 of the schedule name, cron, and slot time (UTC, second
resolution) — following the
[Absurd cron pattern](https://earendil-works.github.io/absurd/patterns/cron/). If two
beat processes briefly overlap, or a beat restarts and re-fires a slot it already
attempted, Absurd collapses the duplicate to one task — each slot fires **at most
once**. Single-instance is still the recommendation (leader election is not built in),
but brief overlap is now safe.

**Run exactly one beat process.** Running two or more beat processes against the same
schedule causes double-firing under normal conditions: both processes independently fire
each task at the same time. Per-slot idempotency protects against brief overlaps; it
does not replace proper single-instance supervision. Use process supervision or a
container orchestrator to enforce a single instance.

**Fire-forward only.** The beat does not backfill missed firings. If it is down when a
scheduled time passes, that firing is skipped; the next firing proceeds on schedule.

### pg_cron backend

Set `SCHEDULER = "pg_cron"` to let Postgres fire schedules directly — no beat process
needed.

**Prerequisites (operator-side — a migration cannot do these):**

- **`pg_cron` ≥ 1.4** (`cron.alter_job`, used every reconcile, was added in 1.4).
- `shared_preload_libraries = pg_cron` in `postgresql.conf` (requires a server restart).
- `cron.database_name = <your_db>` pointing at the Absurd database.

**Extension creation:** the `django_absurd.pg_cron` app's `0001_initial` migration runs
`CREATE EXTENSION IF NOT EXISTS pg_cron` as its first operation. This is a no-op when
the extension is already present (managed Postgres, pre-created by a superuser), and a
loud `permission denied` / `must be superuser` failure when the extension is absent and
the migrate role lacks superuser rights — exactly the fail-fast you want for an opt-in
app. On managed Postgres where the migrate role is not a superuser, pre-create the
extension as a superuser first so the migration no-ops cleanly. (Reversing it runs
`DROP EXTENSION IF EXISTS pg_cron` — stock Django `CreateExtension` behavior.)

**Enabling:**

Add `"django_absurd.pg_cron"` to `INSTALLED_APPS` **after** `"django_absurd"` — the
opt-in app owns the projection table and wrapper function migrations and reconciles the
`SCHEDULE` on `post_migrate`. Running `manage.py check` reports `absurd.E008` if
`SCHEDULER="pg_cron"` is set but the app is absent, and `absurd.W003` if the app is
present but ordered before `"django_absurd"`.

```python
INSTALLED_APPS = [
    # ...
    "django_absurd",
    "django_absurd.pg_cron",   # must come after "django_absurd"
]
```

Then configure the scheduler:

```python
OPTIONS = {
    "SCHEDULER": "pg_cron",
    "SCHEDULE": {
        "nightly-report": {"task": "myapp.tasks.send_report", "cron": "0 2 * * *"},
    },
}
```

`pg_cron` validates its own schedule grammar: a 5-field cron **or** the interval form
`<n> seconds` (1-59). Sub-minute cadence therefore works under `pg_cron` via
`30 seconds` — distinct from beat's 6-field croniter syntax, which `pg_cron` does not
accept. This grammar is validated by the database (at sync for settings schedules, at
save time for admin ones), not by `check`.

Beat and pg_cron are **mutually exclusive** per backend: running `absurd_beat` or
`absurd_worker --beat` against a backend with `SCHEDULER="pg_cron"` raises
`CommandError`.

**Reconcile:**

```bash
python manage.py migrate              # reconciles on every deploy (recommended)
python manage.py absurd_sync_crons    # explicit reconcile / backstop
python manage.py absurd_sync_crons --teardown  # unschedule all jobs (prompts; --no-input)
```

`migrate` fires `post_migrate`, which reconciles the declared `SCHEDULE` into `pg_cron`
jobs automatically — a settings-only change needs no new migration file.
`absurd_sync_crons` is the backstop for pipelines that skip `migrate`.

`--teardown` unschedules every owned `pg_cron` job for the backend — **including
admin-authored ones** — and deletes their `ScheduledTask` rows (settings **and** admin).
Deleting the admin rows is deliberate: the next `migrate` re-emits a job for every
surviving admin row, so keeping the rows would silently resurrect the jobs teardown just
killed. Because it destroys admin-authored schedules, it prompts for confirmation unless
`--no-input` is passed. Migrate-time teardown (switching a backend off `pg_cron`) is
narrower — it only clears settings jobs and rows, never admin ones.

**Wrapper model:** each schedule is materialised as a `ScheduledTask` row (the
projection table, `django_absurd_scheduledtask`). The row stores explicit option columns
— `args`, `kwargs`, `max_attempts`, `retry_strategy`, `headers`, `cancellation`,
`idempotency_key` — one typed column per spawn option. The `pg_cron` job command is a
constant call to `public.django_absurd_run_scheduled(source, alias, name)`; the wrapper
reads the row at fire time, reassembles `params`/`options` jsonb from those named
columns server-side, then calls `absurd.spawn_task`. Editing args/kwargs/options takes
effect on the next fire without touching `cron.job`. Both the projection table and the
wrapper function live in the `public` schema (Django app tables live there); the
`absurd` schema is owned by the Absurd SDK's migration and is dropped wholesale on
reverse, which would remove a wrapper placed there while the `ScheduledTask` table
survived — keeping both in `public` avoids that hazard. They are created and managed by
the `django_absurd_pg_cron` app migration, applied by `manage.py migrate`.

The reconcile path never stores `{}` in `retry_strategy` or `cancellation` — it stores
`None` (SQL `NULL`) when those options are absent. A row inserted directly (not via
reconcile) that stores `{}` in either column would pass the wrapper's `IS NOT NULL`
check; settings-managed rows are unaffected.

**Non-default-backend schedules.** A schedule entry without an explicit `queue` falls
back to the task function's own `queue_name`. When the backend is not the default one,
that queue may not be declared for that backend — set `queue` explicitly for every
schedule on a non-default backend (mirrors `task.using(backend=...)` semantics). For
`pg_cron` schedules `absurd.E007` also validates this resolved fallback queue; under the
beat scheduler only an explicit `queue` key is checked, so setting `queue` explicitly
matters most there.

**Admin.** `ScheduledTask` rows appear in Django admin. Settings-declared rows
(`ScheduledTask.Source.SETTINGS`) are **read-only** — `SCHEDULE` in settings is their
source of truth. Admins can additionally author `ScheduledTask.Source.ADMIN` schedules
directly in the admin (create/edit/delete): choose the **Backend** (a configured
`pg_cron` backend), a name, task, optional queue, and a cron expression. `alias` and
`name` are immutable once created (they form the job identity); the cron expression is
validated by `pg_cron` itself at save time (so `<n> seconds` is accepted and an invalid
expression is rejected with `pg_cron`'s own message). **`max_attempts`** defaults to `5`
(Absurd's default retry ceiling) and must be `≥ 1`; clearing it stores `NULL`, which
Absurd treats as **retry forever** — a deliberate opt-in, so a mistyped schedule can't
loop unbounded by accident. Saving or deleting an admin row immediately (un)schedules
its `pg_cron` job — the row is the source of truth, so any write that persists it
(admin, ORM, or `loaddata`) keeps `pg_cron` in step (`cron.schedule` is an idempotent
upsert). A write forced onto a **different** database (`loaddata --database=…`,
`.using(…)`) raises `NotImplementedError` — schedules live only on the absurd DB, so a
misplaced row is rejected before it's inserted rather than paired with a phantom job.
(When Absurd is on a **non-default** database, `loaddata` bypasses the router and
targets `default`, so pass `--database=<alias>` to load schedules onto the absurd DB.)
Writes that bypass `.save()` — a **data migration** (the historical model isn't the
signal's sender), `bulk_create`, `QuerySet.update`, raw SQL — don't emit directly, but
`migrate` (and `absurd_sync_crons`) reconciles admin rows, so their jobs materialize
then. A settings schedule and an admin schedule **may** share a name: they are distinct,
source-namespaced jobs (`absurd:s:…` vs `absurd:a:…`, the source abbreviated to keep the
job name short). Removing admin-authored jobs at teardown is a guarded action (see
Reconcile).

### Validate

`python manage.py check django_absurd` validates every schedule entry and reports
`absurd.E007` for:

- an unimportable or non-`@task` `task` path
- an invalid cron expression (beat only; `pg_cron` grammar is validated by the database,
  not by `check`)
- unknown keys in the spec
- `args`/`kwargs` values that are not JSON-serializable
- a `queue` that is not declared in `OPTIONS["QUEUES"]`
- an unknown `SCHEDULER` value
- (`pg_cron` only) schedule name or backend alias containing characters outside
  `[A-Za-z0-9_-]`
- (`pg_cron` only) composed job name (`absurd:s:<alias>:<name>`) exceeding 63 bytes

Fix everything `absurd.E007` reports before relying on the schedule in production.

## Retrieving results

`enqueue` returns a `TaskResult`; refresh it or fetch one later by id:

```python
result = send_report.enqueue(42)
result.refresh()              # reload status / return_value / errors from the store
result.status                 # READY | RUNNING | SUCCESSFUL | FAILED
result.return_value           # available once SUCCESSFUL

send_report.get_result(result.id)              # fetch by id (sync)
await send_report.aget_result(result.id)       # async variant
```

## Deployment notes

- **Database privileges.** `migrate` runs `CREATE EXTENSION IF NOT EXISTS "uuid-ossp"`
  and `CREATE SCHEMA IF NOT EXISTS absurd`, so the migrating role needs rights to create
  extensions and schemas (a superuser, or a role granted those — with `uuid-ossp`
  allow-listed on managed Postgres). The schema name `absurd` is fixed.
- **At-least-once delivery.** A task may run more than once (e.g. a crash between the
  handler committing and Absurd's bookkeeping). Keep handlers idempotent; use
  `idempotency_key` where it helps.
- **Queue creation is automatic and additive.** Declared queues are created at `migrate`
  (post_migrate), on worker start, by `absurd_sync_queues`, and on first enqueue;
  provisioning also reconciles mutable policy. Nothing ever drops queues removed from
  config. A queue's `storage_mode` is immutable after creation (a declared change is
  reported as a warning, not applied). Only queues declared in `QUEUES` are created — an
  undeclared queue name is rejected, not silently created.
- **Teardown is destructive.** `migrate django_absurd zero` drops the `absurd` schema
  and all data in it.

## Adopting an existing Absurd database

If the target database already runs Absurd (its schema managed outside Django), you can
fake django-absurd's migration so Django records it as applied without re-running the
DDL:

```bash
python manage.py migrate --fake django_absurd
```

**Use extreme caution.** Faking tells Django the schema is already present without
checking it. Only do this when the existing `absurd` schema exactly matches the version
django-absurd targets (`django_absurd.ABSURD_SCHEMA_VERSION`) — a mismatch causes
runtime failures Django cannot detect. Verify the versions line up before faking.

## Notes

- Migrations are offline — the schema comes only from the pinned Absurd version shipped
  with this package; never fetch at migrate time.
- Alpha software; APIs may change between versions.
