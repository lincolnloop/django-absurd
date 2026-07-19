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
- `DEFAULT_MAX_ATTEMPTS` — retry ceiling per task (default: `5`; must be an integer
  `>= 1`).
- `QUEUES` — a map of queue name → `absurd_sdk.CreateQueueOptions` for per-queue config.
  Use this _instead of_ the top-level `QUEUES` list (which only names queues) — declare
  queues in one place or the other, never both (setting both is a configuration error).
- `CLEANUP` — a map `{"schedule": "<cron>"}` to run cleanup automatically on cadence
  (beat: in-process; pg_cron: native database job). Omit to skip scheduled cleanup.
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
- `absurd.E004` — more than one Absurd backend is configured. django-absurd supports
  exactly one Absurd backend per project.
- `absurd.E005` — `AbsurdRouter` missing from `DATABASE_ROUTERS`.
- `absurd.E006` — `ENABLE_ADMIN` is not a bool, or `ADMIN_SITE` paths don't resolve to
  `AdminSite` instances.
- `absurd.E007` — invalid `SCHEDULE` entry (bad task path, bad cron expression, unknown
  key, non-serializable or wrong-shaped args/kwargs, or undeclared queue). See
  [Scheduling recurring tasks](#scheduling-recurring-tasks).
- `absurd.E009` — `OPTIONS["DEFAULT_MAX_ATTEMPTS"]` is not an integer `>= 1`.
- `absurd.E010` — invalid `CLEANUP` configuration (not a `{"schedule": …}` map, or
  unknown keys; cron grammar checked at `check` time for beat, at sync for pg_cron).
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
  `--poll-interval`, `--batch-size`, and `--worker-id`.

## Scheduling recurring tasks

django-absurd supports two schedulers, selected by whether `"django_absurd.pg_cron"` is
in `INSTALLED_APPS`:

| State                               | Scheduler   | Description                                                      |
| ----------------------------------- | ----------- | ---------------------------------------------------------------- |
| app absent (default)                | `"beat"`    | In-process beat; evaluates cron and enqueues via the normal path |
| `"django_absurd.pg_cron"` installed | `"pg_cron"` | Database-side; Postgres fires jobs directly via `pg_cron`        |

### Declare schedules

Add a `SCHEDULE` map to `OPTIONS`. The schema is the same for both schedulers:

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
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

Install `"django_absurd.pg_cron"` to let Postgres fire schedules directly — no beat
process needed.

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
opt-in app owns the projection table and wrapper function migrations, switches the
backend's scheduler to `"pg_cron"`, and reconciles the `SCHEDULE` on `post_migrate`.
Running `manage.py check` reports `absurd.W003` if the app is present but ordered before
`"django_absurd"`.

```python
INSTALLED_APPS = [
    # ...
    "django_absurd",
    "django_absurd.pg_cron",   # must come after "django_absurd"
]
```

Then declare your schedule:

```python
OPTIONS = {
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

Beat and pg_cron are **mutually exclusive**: running `absurd_beat` or
`absurd_worker --beat` while `django_absurd.pg_cron` is installed raises `CommandError`.

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
projection table, `django_absurd_scheduledtask`). The row stores explicit, typed option
columns — `args`, `kwargs`, `max_attempts`, the retry strategy split into `retry_kind`
(a choice of `fixed`/`exponential`/`none`) + `retry_base_seconds`/`retry_factor`/
`retry_max_seconds`, the cancellation policy as `cancellation_max_duration`/
`cancellation_max_delay`, `headers` (free-form JSON), and `idempotency_key`. Typed
columns validate at save time (a bad retry kind or non-numeric timing is rejected in the
admin, not at fire time). The `pg_cron` job command is a constant call to
`public.django_absurd_run_scheduled(source, name)`; the wrapper reads the row at fire
time, reassembles `params`/`options` jsonb from those named columns server-side
(rebuilding the `retry_strategy`/`cancellation` objects, omitting null keys), then calls
`absurd.spawn_task`. Editing args/kwargs/options takes effect on the next fire without
touching `cron.job`. Both the projection table and the wrapper function live in the
`public` schema (Django app tables live there); the `absurd` schema is owned by the
Absurd SDK's migration and is dropped wholesale on reverse, which would remove a wrapper
placed there while the `ScheduledTask` table survived — keeping both in `public` avoids
that hazard. They are created and managed by the `django_absurd_pg_cron` app migration,
applied by `manage.py migrate`.

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
directly in the admin (create / edit / delete) via a **two-step flow**:

1. **Add form** — fill only **Name**, **Task** (dotted import path), and **Cron**
   expression. On save, the remaining spawn options (`queue`, `max_attempts`, retry
   strategy, cancellation policy, `headers`, `idempotency_key`) are resolved from the
   task's `@task` / `@absurd_default_params` decorators and stored. **Queue is
   required** — a blank queue is rejected; it always resolves to a concrete queue. The
   row is created **disabled** (`enabled=False`) so it does not fire yet. Resolution is
   frozen at create: later decorator edits do not change existing rows.

2. **Change form** — review the resolved values, fill `args` / `kwargs` if the task
   needs them, and set **Enabled** to activate. Once enabled, saving or deleting the row
   immediately (un)schedules its `pg_cron` job.

`name` is immutable once created (it forms the job identity); the cron expression is
validated by `pg_cron` itself at save time (so `<n> seconds` is accepted and an invalid
expression is rejected with `pg_cron`'s own message). **`max_attempts`** defaults to `5`
(Absurd's default retry ceiling) and must be `≥ 1`; clearing it stores `NULL`, which
Absurd treats as **retry forever** — a deliberate opt-in, so a mistyped schedule can't
loop unbounded by accident. The row is the source of truth: any write that persists it
(admin, ORM, or `loaddata`) keeps `pg_cron` in step (`cron.schedule` is an idempotent
upsert). A write forced onto a **different** database (`loaddata --database=…`,
`.using(…)`) raises `NotImplementedError` — schedules live only on the absurd DB. (When
Absurd is on a **non-default** database, `loaddata` bypasses the router and targets
`default`, so pass `--database=<alias>` to load schedules onto the absurd DB.) Writes
that bypass `.save()` — a **data migration** (the historical model isn't the signal's
sender), `bulk_create`, `QuerySet.update`, raw SQL — don't emit directly, but `migrate`
(and `absurd_sync_crons`) reconciles admin rows, so their jobs materialize then. A
settings schedule and an admin schedule **may** share a name: they are distinct,
source-namespaced jobs (`_dj:s:…` vs `_dj:a:…`, the source abbreviated to keep the job
name short). Removing admin-authored jobs at teardown is a guarded action (see
Reconcile).

### Validate

`python manage.py check django_absurd` validates every schedule entry and reports
`absurd.E007` for:

- an unimportable or non-`@task` `task` path
- an invalid cron expression (beat only; `pg_cron` grammar is validated by the database,
  not by `check`)
- unknown keys in the spec
- `args`/`kwargs` values that are not JSON-serializable
- an `args` that is not a JSON array, or a `kwargs` that is not a JSON object
- a `queue` that is not declared in `OPTIONS["QUEUES"]`
- (`pg_cron` only) schedule name containing characters outside `[A-Za-z0-9_-]`
- (`pg_cron` only) composed job name (`_dj:s:<name>`) exceeding 63 bytes

Fix everything `absurd.E007` reports before relying on the schedule in production.

## Cleanup / retention

`cleanup_queues()` enforces each queue's `cleanup_ttl` / `cleanup_limit` retention knobs
(configured via `OPTIONS["QUEUES"]` — see [Configure](#configure)). It deletes terminal
task rows (completed, failed, cancelled) older than the queue's TTL, up to the batch
limit, and returns one dict per queue:

```python
from django_absurd.cleanup import cleanup_queues

cleanup_queues()                       # every declared queue
cleanup_queues(["reports", "emails"])  # only these queues
# → [{"queue_name": "default", "tasks_deleted": 12, "events_deleted": 0}]
```

→ [Absurd: Cleanup](https://earendil-works.github.io/absurd/cleanup/) (the underlying
`absurd.cleanup_all_queues()` behaviour and the full retention model).

**On demand:** `manage.py absurd_cleanup` runs it and prints per-queue counts; pass
queue names to limit it, or omit them for all:

```bash
python manage.py absurd_cleanup            # all queues
python manage.py absurd_cleanup reports    # just 'reports'
# default: 12 tasks, 0 events deleted
```

**Scheduled:** add `OPTIONS["CLEANUP"] = {"schedule": "<cron>"}` to run cleanup
automatically on cadence — zero user code required:

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "CLEANUP": {"schedule": "0 3 * * *"},   # 3am daily
        },
    },
}
```

This works under **either** scheduler: beat runs cleanup in-process on the declared
cadence; pg_cron schedules Absurd's own native cleanup job (`absurd_cleanup_all`, the
same identity `absurdctl cron` uses — compatible, not a parallel job). When
`django_absurd.pg_cron` is installed, django-absurd is authoritative over that job: it
schedules it from `OPTIONS["CLEANUP"]` and removes it otherwise — including at migrate
teardown / scheduler-flip even when `CLEANUP` was never set — so a job created via
`absurdctl cron` is reclaimed and removed. Drive cleanup one way only —
`OPTIONS["CLEANUP"]` **or** `absurdctl cron`, not both. `manage.py check` reports
`absurd.E010` for a malformed `CLEANUP` (the beat cron grammar is checked then too;
pg_cron's is validated by the database at sync). Retention knobs (`cleanup_ttl`,
`cleanup_limit`) remain per-queue policy — set them in `OPTIONS["QUEUES"]`.

**Reset (destructive):** `manage.py absurd_flush` **deletes all task history** — it
removes every queue (its per-queue tables and registry entry) along with all tasks,
runs, and events in them. It does **not** uninstall Absurd: the schema, migrations, and
functions stay, so you never re-`migrate` — only re-provision the queues. It prompts for
confirmation; pass `--noinput` (alias `--no-input`) to skip the prompt in automation:

```bash
python manage.py absurd_flush            # prompts, then drops on 'yes'
python manage.py absurd_flush --noinput  # drops without prompting
```

Re-provision declared queues afterward with `migrate`, `absurd_sync_queues`, or by
starting a worker.

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
  reported as a warning, not applied); `storage_mode="partitioned"` is declarable but
  **experimental — not tested yet**, with no automated partition lifecycle. Only queues
  declared in `QUEUES` are created — an undeclared queue name is rejected, not silently
  created.
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

## Durable workflows

Call `durable_context()` **inside** a running task to reach Absurd's durable primitives.
It is orthogonal to Django's `TaskContext` — you do **not** need `takes_context=True`
(add that only if you also want `context.task_result`/`.attempt`).

```python
from django_absurd import durable_context
```

`durable_context()` auto-selects by task kind:

- **Async task → the SDK's own `absurd_sdk.AsyncTaskContext`** (a py.typed object) —
  pure passthrough, you `await` its methods. Annotate with `absurd_sdk.AsyncTaskContext`
  for full editor autocomplete and mypy checking.
- **Sync task → `django_absurd.AbsurdTaskContext`**, a thin bridge mirroring the SDK's
  sync signatures (no `await`); it also carries `run_step` (sync only). Annotate with
  `AbsurdTaskContext` (exported from the package root).

Called outside a running Absurd task, `durable_context()` raises `RuntimeError`.

### Steps (checkpoints)

`context.step(name, fn)` runs `fn()`, persists the result as a checkpoint, and skips it
on replay — the core of durable execution. Step names must be deterministic and stable
across replays; Absurd uses them to locate the right checkpoint on resume.

→
[Absurd — Concepts: Steps (Checkpoints)](https://earendil-works.github.io/absurd/concepts/#steps-checkpoints)

```python
from django.tasks import task
from django_absurd import durable_context


@task
def process_order(order_id: int) -> None:
    context = durable_context()
    context.step("charge", lambda: charge_card(order_id))
    context.step("ship", lambda: ship(order_id))
```

`context.run_step` is a convenience decorator alternative to `context.step` (sync only):

```python
@task
def process_order(order_id: int) -> None:
    context = durable_context()

    @context.run_step
    def charge():
        return charge_card(order_id)           # step name derived from function name

    @context.run_step("ship-item")             # explicit name
    def ship_item():
        return ship(order_id)
```

The async `step`'s `fn` must return an awaitable — pass an `async def`, not a plain
lambda (a sync lambda returns a non-awaitable and raises `TypeError`):

```python
import absurd_sdk
from django.tasks import task
from django_absurd import durable_context


@task
async def process_order(order_id: int) -> None:
    context: absurd_sdk.AsyncTaskContext = durable_context()

    async def charge():
        return await charge_card(order_id)

    await context.step("charge", charge)

    async def ship_order():
        return await ship(order_id)

    await context.step("ship", ship_order)
```

For long-running steps, call `context.heartbeat()` periodically to extend the claim
timeout and keep the run alive.

### Sleep

`context.sleep_for(step_name, duration)` suspends the task for `duration` seconds.
`context.sleep_until(step_name, wake_at)` suspends until a specific moment. Both are
checkpointed steps — the step name is required and must be stable across replays.

→ [Absurd — Concepts: Sleep](https://earendil-works.github.io/absurd/concepts/#sleep)

```python
@task
def process_order(order_id: int) -> None:
    context = durable_context()
    context.step("charge", lambda: charge_card(order_id))
    context.sleep_for("cooldown", 5)          # suspend for 5 seconds
    context.step("ship", lambda: ship(order_id))
```

`sleep_until` `wake_at`: pass a timezone-aware `datetime` — a naive `datetime` raises
when compared against Absurd's timezone-aware clock. A Unix timestamp (`int` or `float`)
is always unambiguous. Sleep resume re-claims the same run — the attempt counter does
not increment.

### API reference

| Method / property                 | Sync | Async   | What it does                                              |
| --------------------------------- | ---- | ------- | --------------------------------------------------------- |
| `step(name, fn)`                  | yes  | `await` | Run `fn()`, checkpoint the result; skip on replay         |
| `sleep_for(step_name, duration)`  | yes  | `await` | Suspend the task for `duration` seconds                   |
| `sleep_until(step_name, wake_at)` | yes  | `await` | Suspend until a `datetime`, Unix timestamp, or float      |
| `heartbeat(seconds=None)`         | yes  | `await` | Extend the claim timeout (keep the run alive)             |
| `headers`                         | yes  | yes     | Read-only mapping of headers passed at enqueue time       |
| `run_step([name])` (decorator)    | yes  | —       | Convenience wrapper around `step`; derives name from `fn` |

### Footguns

**(a) Effectively-once, not exactly-once.** A step's result is persisted to the database
after `fn` returns, on a separate connection. In the window between `fn` completing and
the checkpoint being written, a crash re-runs the step. Design side effects to be
idempotent (for example, use `idempotency_key` on downstream enqueues, or make database
writes upserts).

**(d) Never swallow `SuspendTask` or `CancelledTask`.** Absurd uses these exceptions
internally to suspend and cancel runs. If you have a bare `except Exception` (or
broader) inside a step's `fn`, re-raise them:

```python
from absurd_sdk import SuspendTask, CancelledTask


def my_fn():
    try:
        ...
    except (SuspendTask, CancelledTask):
        raise
    except Exception:
        ...
```

**(f) Absurd backend only.** `durable_context()` (and `step`/`sleep_for`/`sleep_until`
on it) is Absurd-specific. Calling it under any other Django task backend — where the
Absurd runtime context is never set — raises `RuntimeError`.

Absurd's durable-execution rules also apply — deterministic step naming/order,
JSON-serializable step return values, and finishing a step within `claim_timeout` (or
calling `context.heartbeat()`); see
[Absurd — Concepts](https://earendil-works.github.io/absurd/concepts/).

## Notes

- Migrations are offline — the schema comes only from the pinned Absurd version shipped
  with this package; never fetch at migrate time.
- Alpha software; APIs may change between versions.
