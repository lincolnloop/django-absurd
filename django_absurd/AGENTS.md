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
- `absurd.E011` — `OPTIONS["SYNC_SCHEDULES_ON_TEST_DB"]` is `True` without
  `OPTIONS["PG_CRON_ON_TEST_DB"]`. See [Test databases](#test-databases).
- `absurd.E012` — the central `cron.database_name` database (auto-discovered) is
  unreachable or missing the `pg_cron` extension; a deploy-time check, see
  [Validate schedules](#validate-schedules).
- `absurd.W002` (Warning) — a queue's declared `storage_mode` differs from the database;
  `storage_mode` is immutable once the queue exists.
- `absurd.W003` (Warning) — `"django_absurd.pg_cron"` is in `INSTALLED_APPS` but ordered
  before `"django_absurd"`, causing its `post_migrate` cron reconcile to run before
  queue provisioning. See [pg_cron backend](#pg_cron-backend).

## Defining and enqueuing tasks

Use Django's Tasks API. Tasks may be **sync (`def`) or async (`async def`)** — one
worker runs both, and `async def` tasks may use Django's async ORM. Tasks are resolved
by import path, so they can live in any importable module (no `tasks.py` requirement).

Enqueuing rides the surrounding Django transaction — a task spawned inside `atomic()` is
rolled back if the block fails (enqueue-on-commit, automatic).

Absurd parameters attach two ways, both through `absurd_params` — exported from the
package root (`from django_absurd import absurd_params`; its home module,
`django_absurd.params`, keeps working too):

- **Per-task defaults** — the `@absurd_params(...)` decorator, applied _below_ `@task`
  (applying it above a `Task` raises `TypeError`):

  ```python
  from django.tasks import task
  from django_absurd import absurd_params

  @task
  @absurd_params(max_attempts=3)
  def send_report(...): ...
  ```

- **Per-invocation** — `.bind(task)` an `absurd_params(...)` call onto the task before
  enqueuing:

  ```python
  from django_absurd import absurd_params

  absurd_params(idempotency_key="abc").bind(send_report).enqueue(...)
  ```

  `bind` returns an ordinary `Task`: `isinstance(bound, Task)` holds, and
  `aenqueue`/`call`/`get_result`/`using` all work through it exactly as on the original
  task. Django's own Task API options — routing (`.using(queue_name=...)`), `backend`, …
  — stay on `.using()`, never on `absurd_params`; routing composes with binding in
  either order. `bind` attaches the params whatever backend the task is currently on, so
  binding and `.using(backend=...)` compose in either order too. Only the Absurd backend
  reads them — if the task is still on another backend at enqueue, the params are
  silently inert and `AbsurdTask.enqueue`/`aenqueue` log one `WARNING` naming the task
  and the backend it ran on (deduped per task).

Parameter fields (see `django_absurd.params`): `max_attempts`, `retry_strategy`,
`cancellation` (defaults and per-call), plus `headers` and `idempotency_key` (per-call
only) — the split is enforced by an overload pair on `absurd_params`, not just
convention. Field types come from `absurd_sdk` (`RetryStrategy`, `CancellationPolicy`,
`JsonObject`). The decorator's `max_attempts` and `cancellation` mirror the defaults
accepted by Absurd's own [task definition](https://earendil-works.github.io/absurd/)
(`default_max_attempts`, `default_cancellation`), but not field-for-field: Absurd's
`register_task` takes no `retry_strategy`, so that field is ours alone, applied at spawn
time. Backend capabilities: result retrieval, async tasks, and deferred (run-later)
enqueue are supported; priority is not.

`max_attempts=None` is accepted (and typed) at both sites, and means **retry forever** —
Absurd stores a NULL ceiling and retries while it is NULL. Omitting `max_attempts` is
not the same thing: the backend fills in its own `OPTIONS["DEFAULT_MAX_ATTEMPTS"]` (5)
on every enqueue, so only an explicit `None` reaches the unbounded behaviour.

**An idempotency key is scoped to its queue, not to your task.** A key reserves itself
against one queue, with no task name and no arguments in the comparison. Whichever
enqueue arrives first owns the key; every later enqueue is swallowed and handed the
FIRST task's id — even a different task, even with different arguments, so
`absurd_params(idempotency_key="nightly").bind(purge_cache).enqueue()` after the same
key was used for `send_report` returns `send_report`'s id and never runs `purge_cache`.
Namespace the key so it identifies the work (`f"send_report:{report_id}:{date}"`), as
the beat scheduler does for its own spawns (see
[Scheduling recurring tasks](#scheduling-recurring-tasks)). Two further properties:
different queues never collide (the same key on `default` and on `reports` reserves
independently and both run), and a key is held for as long as its task row exists —
freed only once the task is terminal and cleanup deletes it, `cleanup_ttl` (default 30
days) after it finished, with a pending/running/sleeping task holding its key
indefinitely. A key is therefore not a time window; it is "once until the row is swept."

**Deferred enqueue.** `task.using(run_after=<aware datetime>).enqueue(...)` defers one
enqueue. Absurd's spawn has no `available_at`, so a deferred enqueue spawns a second row
named `<your task's dotted path>:run_after` that waits, then enqueues yours with the
options you passed — both rows show up in the admin, filterable by that name. The id
`enqueue` returned keeps working throughout: `READY` while the wrapper waits, then your
task's own status and return value once it runs; a struggling wrapper leaves that id
`READY` with no visible errors until it runs out of attempts, then `FAILED`.

A deferral logs as the run-level `task suspended` line, not as sleep or step lines.

## Workers

```bash
python manage.py absurd_worker            # consumes the "default" queue
python manage.py absurd_worker --queue reports
```

A single worker runs **both** sync and async tasks: `async def` tasks run on an event
loop (true concurrency for I/O-bound work), sync `def` tasks run in a thread pool. On
start it runs a full sync — reconciling **every** declared queue (creating missing ones,
applying declared policy changes) and rebuilding the admin views so they reflect the
whole catalog, not just the served queue — and reports to stdout. It then polls until
`SIGINT`/`SIGTERM`.

- `--queue` (default `"default"`): which queue to consume.
- `--concurrency N` (default `1`): number of tasks in flight at once. A slot is refilled
  as soon as it frees, rather than waiting for the whole batch to finish — one slow task
  no longer idles the others. Sizes both the event-loop concurrency and the sync thread
  pool.
- A stop signal (`SIGINT`/`SIGTERM`) stops claiming and lets in-flight tasks finish
  before the worker exits. Other flags: `--claim-timeout`, `--poll-interval`,
  `--batch-size`, and `--worker-id`.

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

**pg_cron is a cluster-wide extension, not a per-app-database one.**
`cron.database_name` (a GUC, one value per Postgres cluster) names the single database
allowed to hold `CREATE EXTENSION pg_cron` — every other database in the cluster,
including your Absurd database, has no `cron` schema at all. django-absurd never
installs the extension on the Absurd database and never touches `cron.*` there: it opens
a short-lived connection to whichever database `cron.database_name` names
(auto-discovered via `current_setting('cron.database_name')` — nothing to set in Django)
and schedules each job **cross-database** with
[`cron.schedule_in_database`](https://github.com/citusdata/pg_cron#cross-database-scheduling),
targeting your Absurd database by name. If your cluster happens to run pg_cron with
`cron.database_name` set to the Absurd database itself (the traditional single-database
setup), this degenerates to scheduling into "itself" and behaves exactly as before —
zero reconfiguration needed.

**Operator setup (one-time, on the central `cron.database_name` database — a migration
cannot do this):**

- **`pg_cron` ≥ 1.4** (`cron.schedule_in_database`'s full signature and
  `cron.alter_job`, used every reconcile, both landed in 1.4).
- `shared_preload_libraries = pg_cron` in `postgresql.conf` (requires a server restart).
- `CREATE EXTENSION pg_cron`, run once on the database named by `cron.database_name`
  (**not** the Absurd database, unless they're the same database).
- Grant the scheduling role (the role django-absurd's `migrate` / `absurd_sync_crons`
  connects as) the privileges to call the two functions it needs, on that same central
  database:

  ```sql
  GRANT USAGE ON SCHEMA cron TO <scheduling_role>;
  GRANT EXECUTE ON FUNCTION
      cron.schedule_in_database(text, text, text, text, text, boolean)
      TO <scheduling_role>;
  GRANT EXECUTE ON FUNCTION
      cron.alter_job(bigint, text, text, text, text, boolean)
      TO <scheduling_role>;
  ```

  The `alter_job` grant is required, not optional: `schedule_in_database`'s `active`
  argument only takes effect when it first creates a job, so disabling an
  already-scheduled job needs an explicit `alter_job` call. Skip both grants entirely if
  the scheduling role **owns** the extension (e.g. it ran the `CREATE EXTENSION` itself,
  or is a superuser) — an owner already has execute rights on every function in the
  schema.

**Local pg_cron via Docker.** Stock `postgres` doesn't bundle pg_cron, so build it in:

```dockerfile
# pg_cron.Dockerfile
FROM postgres:18
RUN apt-get update && apt-get install -y postgresql-18-cron
```

```yaml
# compose.yaml
services:
  db:
    build:
      dockerfile: pg_cron.Dockerfile
    command: postgres -c shared_preload_libraries=pg_cron
    environment: { POSTGRES_PASSWORD: postgres }
```

pg_cron runs against the `postgres` database by default (`cron.database_name` defaults
to `postgres`); pass `-c cron.database_name=<db>` to point it elsewhere. Either way, run
the `CREATE EXTENSION` + grants above once against whichever database that names — see
this repo's own `Dockerfile.pg_cron`, which writes a `/docker-entrypoint-initdb.d`
script (`\connect postgres` + `CREATE EXTENSION`) for a worked example.

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

#### Test databases

Any `cron.*` write for a backend is a hazard on a **test** database or during an active
test run: a plain test database never carries the central `pg_cron` extension (it lives
only on the `cron.database_name` database, above), so an unguarded write there would
fail outright — and even where it wouldn't fail (e.g. `PG_CRON_ON_TEST_DB` routed it to
a real central catalog), pg_cron's launcher runs independently of pytest/Django, so a
schedule left behind fires for real, on cadence, against test data, for the rest of the
session. So this scheduling seam is **inert by default under tests** — detected
automatically (no settings changes needed) — and every `cron.*` write silently no-ops
until you opt in.

**`OPTIONS["PG_CRON_ON_TEST_DB"]`** (default `False`) is that opt-in: set it to `True`
for a backend whose tests genuinely need real `pg_cron` jobs (this project's own
`tests/pg_cron` suite does exactly this — see `tests/pg_cron/settings.py`). With it
unset, `absurd_sync_crons` **refuses to run** rather than silently doing nothing:

```
CommandError: Refusing to reconcile pg_cron jobs: scheduling is inert here — this is a
test database or an active test run and PG_CRON_ON_TEST_DB is not enabled for backend
'<alias>'.
```

Two further `OPTIONS` keys govern _`migrate`'s automatic_ reconcile specifically (on top
of the `PG_CRON_ON_TEST_DB` gate above, which still applies):
`SYNC_SCHEDULES_ON_MIGRATE` (default `True`) governs sync against a real database;
`SYNC_SCHEDULES_ON_TEST_DB` (default `False`) governs sync when Django's test framework
has swapped in a test database. Setting `SYNC_SCHEDULES_ON_TEST_DB = True` without also
setting `PG_CRON_ON_TEST_DB = True` is a reported misconfiguration (`absurd.E011`) — the
sync toggle is meaningless while the seam itself stays inert. `absurd_sync_crons` is
never gated by either sync key — it's a deliberate, explicit invocation, not an
automatic side effect of `migrate` — but it **is** gated by `PG_CRON_ON_TEST_DB` like
every other `cron.*` write, per the `CommandError` above.

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
   task's `@task` / `@absurd_params` decorators and stored. **Queue is required** — a
   blank queue is rejected; it always resolves to a concrete queue. The row is created
   **disabled** (`enabled=False`) so it does not fire yet. Resolution is frozen at
   create: later decorator edits do not change existing rows.

2. **Change form** — review the resolved values, fill `args` / `kwargs` if the task
   needs them, and set **Enabled** to activate. Once enabled, saving or deleting the row
   immediately (un)schedules its `pg_cron` job.

`name` is immutable once created (it forms the job identity); the cron expression is
validated by `pg_cron` itself at save time (so `<n> seconds` is accepted and an invalid
expression is rejected with `pg_cron`'s own message). **`max_attempts`** defaults to `5`
(Absurd's default retry ceiling) and must be `≥ 1`; clearing it stores `NULL`, which
Absurd treats as **retry forever** — a deliberate opt-in, so a mistyped schedule can't
loop unbounded by accident. The row is the source of truth: any write that persists it
(admin, ORM, or `loaddata`) keeps `pg_cron` in step (`cron.schedule_in_database` is an
idempotent upsert). A write forced onto a **different** database
(`loaddata --database=…`, `.using(…)`) raises `NotImplementedError` — schedules live
only on the absurd DB. (When Absurd is on a **non-default** database, `loaddata`
bypasses the router and targets `default`, so pass `--database=<alias>` to load
schedules onto the absurd DB.) Writes that bypass `.save()` — a **data migration** (the
historical model isn't the signal's sender), `bulk_create`, `QuerySet.update`, raw SQL —
don't emit directly, but `migrate` (and `absurd_sync_crons`) reconciles admin rows, so
their jobs materialize then. A settings schedule and an admin schedule **may** share a
name: they are distinct, source-namespaced jobs (`_dj:<db>:s:…` vs `_dj:<db>:a:…`,
namespaced by the Absurd database name and a one-letter source abbreviation — `s` for
settings, `a` for admin). Removing admin-authored jobs at teardown is a guarded action
(see Reconcile).

### Validate schedules

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

Fix everything `absurd.E007` reports before relying on the schedule in production.

`python manage.py check --database default` additionally reports `absurd.E012` when the
central `cron.database_name` database is unreachable or missing the `pg_cron` extension
— a deploy-time check (it runs on `migrate` and any `check --database` invocation, never
on a plain DB-free `check`, and it stays quiet under the test suite).

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
cadence; pg_cron calls Absurd's own native cleanup function (`absurd.cleanup_all_queues`
— a SQL function in Absurd's schema, not something the Python SDK exposes) from a job on
django-absurd's own database-namespaced lane, `_dj:<absurd database>:c:cleanup_all`.

Absurd's built-in maintenance scheduler is a separate mechanism: `absurd.enable_cron`,
which `absurdctl cron --enable <queue>` drives, creates **per-queue** jobs
(`absurd_cleanup_<suffix>`, `absurd_partitions_<suffix>`, `absurd_detach_plan_<suffix>`)
and needs `cron.schedule` in the Absurd database itself — which a central
`cron.database_name` topology never provides. django-absurd does not use it.

So django-absurd is authoritative over **its own** job only: it schedules that from
`OPTIONS["CLEANUP"]` and removes it otherwise, including at migrate teardown or a
scheduler flip even when `CLEANUP` was never set. It never sees or removes an
`absurd_cleanup_<suffix>` job created by `absurdctl cron` — those names sit outside the
namespace django-absurd manages, so they survive every teardown and would fire alongside
it. **Drive cleanup one way only** — `OPTIONS["CLEANUP"]` **or** `absurdctl cron`, never
both. `manage.py check` reports `absurd.E010` for a malformed `CLEANUP` (the beat cron
grammar is checked then too; pg_cron's is validated by the database at sync). Retention
knobs (`cleanup_ttl`, `cleanup_limit`) remain per-queue policy — set them in
`OPTIONS["QUEUES"]`.

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

Every id has the same `"<queue>:<uuid>"` shape — including `context.task_result.id`
inside a `takes_context` task, so that can be handed straight back to `get_result`.

## Exceptions

django-absurd raises its own exception types for conditions specific to this package,
all subclasses of `django_absurd.exceptions.DjangoAbsurdError`:

- `QueueNotDeclaredError` — a queue name doesn't match any queue declared for the
  backend. Raised by `emit_event` and the test fixture's `drain()`. `enqueue` raises it
  too, but only when the backend's `QUEUES` option is empty/unset; with `QUEUES`
  configured, a typo'd queue name at enqueue is instead rejected earlier as Django's own
  `InvalidTask`.
- `QueueNotProvisionedError` — a queue is declared but its Absurd table hasn't been
  provisioned yet (run `manage.py absurd_sync_queues`). Raised by `emit_event` and
  `drain()`.
- `BackendNotConfiguredError` — no `AbsurdBackend`, or more than one, is configured in
  `TASKS`. The `absurd_worker`/`absurd_beat`/`absurd_sync_crons` commands translate it
  to a `CommandError`; `emit_event` and the test fixture's `drain()` propagate it
  untranslated.
- `QueueReadOnlyError` — an attempt to `.save()`/`.delete()` one of the read-only admin
  models mapping Absurd's own tables (`Queue`, and the admin entity views over
  tasks/runs/waits/etc.).
- `ViewNotProvisionedError` — one of those admin entity views (a `UNION` over every
  declared queue's own Absurd table) hits a missing relation because a queue was never
  provisioned.
- `TaskIdQueueMismatchError` — the test fixture's `get_result(task_id, queue=...)` was
  given a `"queue:uuid"` id and an explicit `queue=` that name different queues.
- `TaskNotFoundError` — the test fixture's `get_result(task_id, queue=...)` found no
  task by that id on that queue.

Catch `DjangoAbsurdError` to handle any of django-absurd's own typed errors generically;
other failures (schema-not-installed, config validation, clock misuse) still raise plain
`ImproperlyConfigured`/`RuntimeError`/`TypeError` outside the hierarchy.

## Testing

Installing django-absurd registers a
[`pytest11` entry point](https://docs.pytest.org/en/stable/how-to/writing_plugins.html#making-your-plugin-installable-by-others)
automatically — no configuration needed. The plugin builds on
[pytest-django](https://pytest-django.readthedocs.io/); install it in your test
environment (`pip install pytest-django`).

### Cleanup is automatic

pytest users do nothing: the plugin's `pytest_configure` calls
`install_absurd_cleanup()`, wiring Absurd state cleanup into Django's own test teardown
(`TransactionTestCase._post_teardown`) — exact parity with how Django resets its own
tables. No fixture to request, no marker to add.

- Plain
  `TestCase`/[`db`](https://pytest-django.readthedocs.io/en/latest/helpers.html#db)
  tests are cleaned by Django's own
  [rollback](https://docs.djangoproject.com/en/stable/topics/testing/overview/#rollback-emulation)
  — an `enqueue()` rides the same uncommitted transaction, so there's nothing left to
  flush.
- `transaction=True`/[`transactional_db`](https://pytest-django.readthedocs.io/en/latest/helpers.html#transactional-db)
  tests (real
  [`TransactionTestCase`](https://docs.djangoproject.com/en/stable/topics/testing/tools/#django.test.TransactionTestCase)s)
  commit for real, so django-absurd's hook truncates queue state — and, when
  `django_absurd.pg_cron` is installed, unschedules its own settings- and admin-authored
  jobs (and the `OPTIONS["CLEANUP"]` cron job, if configured) — right alongside Django's
  own post-test flush.

### No database access, no Absurd access

A test with no DB access can't touch Absurd either: `enqueue()` goes through Django's
database connection, so it trips pytest-django's own
[database access blocking](https://pytest-django.readthedocs.io/en/latest/database.html)
the same as any other query — the `RuntimeError` telling you to request
`django_db`/`db`/`transactional_db`.

In a multi-DB project, cleanup only runs for a test whose declared
[`databases`](https://docs.djangoproject.com/en/stable/topics/testing/tools/#django.test.TransactionTestCase.databases)
attribute includes the Absurd alias (respecting the `"__all__"` sentinel) — an
undeclared alias is skipped, matching Django's own per-alias flush scoping.

### `manage.py test` (non-pytest)

The pytest wiring above is pytest-specific — Django's own `manage.py test`/
[`DiscoverRunner`](https://docs.djangoproject.com/en/stable/topics/testing/advanced/#django.test.runner.DiscoverRunner)
has no equivalent auto-hook (pytest is django-absurd's primary test surface). Wire the
same public hook yourself, from a runner subclass:

```python
from django.test.runner import DiscoverRunner

from django_absurd.test import install_absurd_cleanup


class MyTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        install_absurd_cleanup()
```

Then point your project's `TEST_RUNNER` at it. `install_absurd_cleanup()` is idempotent
— calling it again where pytest's plugin already installed it is a no-op.

### The `dj_absurd` fixture: durable time, drain, and read

Running a task, moving Absurd's own notion of "now", and inspecting what actually ran
all go through one fixture, `dj_absurd` — whether the test is a one-line "my task
completes" or a [durable sleep](#sleep), an [`await_event` timeout](#timeout), a retry
backoff, or a chain of several sleeps. It returns an `AbsurdTestRuntime` with six
members:

| Member                                      | Does                                                                                         |
| ------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `freeze_time(instant=None)`                 | context manager pinning durable time (`None` = real now at entry)                            |
| `now`                                       | virtual now, timezone-aware, as Postgres itself reports it                                   |
| `sync_queues()`                             | provision every declared queue (rarely needed; see below)                                    |
| `drain(queue="default")`                    | run a queue's claimable tasks to completion, returning `list[RunSnapshot]`                   |
| `emit(name, payload=None, queue="default")` | deliver an event, resolving a task suspended in `await_event`                                |
| `get_result(task_id, queue=...)`            | look up one task, returning `TaskSnapshot` (raises `TaskNotFoundError` on a miss; see below) |

`freeze_time` yields a `FrozenTime` handle. `FrozenTime`, `AbsurdTestRuntime` (what
`dj_absurd` itself is typed as), `TaskSnapshot`, and `RunSnapshot` are all importable
from `django_absurd.test`, for annotating helpers and fixture parameters.
`move_to(datetime)` and `shift(timedelta)` are the only way durable time moves — `shift`
by absolute elapsed time, `move_to` to an absolute aware instant. Leaving the block
releases both halves of the clock, so windows can be sequential; opening one inside
another raises, since two frozen instants cannot both be "now", and a mover used after
its own block exited raises rather than silently re-freezing from real now.

```python
import datetime as dt

import pytest

pytestmark = pytest.mark.django_db(transaction=True)


def test_a_task_sleeps_seven_days_then_completes(dj_absurd):
    with dj_absurd.freeze_time(dt.datetime(2026, 1, 1, tzinfo=dt.UTC)) as frozen_time:
        result = my_weekly_followup_task.enqueue()  # enqueue INSIDE the block
        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(days=7))
        assert [run.state for run in dj_absurd.drain()] == ["completed"]

        snapshot = dj_absurd.get_result(result.id)
        assert snapshot.state == "completed"
```

All six members work unchanged from an `async def` test — same names, nothing to `await`
on the fixture, no separate API. Enqueue with Django's own `await task.aenqueue()`
there, since `enqueue()` is synchronous.

**`sync_queues()`** provisions every declared queue — the runtime counterpart of
`manage.py absurd_sync_queues`. Rarely needed: `migrate` already provisions the declared
catalog, so reach for this only when the test itself changed queue topology — a
`settings` override declaring a queue the migration never saw, or a fixture that dropped
the queues.

**`drain()`** runs every currently-claimable task on `queue` to completion, in-process —
no [worker](#workers) subprocess, no polling loop to manage — one at a time, returning
one `RunSnapshot` per run executed, in claim order. It provisions nothing (unlike the
CLI, which provisions declared queues on start): `migrate` provisions every declared
queue already, so a test database arrives ready, but a queue a single test declares by
overriding `TASKS` needs `dj_absurd.sync_queues()` first or `drain()` raises
`QueueNotProvisionedError` naming that command (a queue that isn't declared at all
raises `QueueNotDeclaredError`) — see [Exceptions](#exceptions) above for the full
typed-error taxonomy.

**`RunSnapshot` fields:** `queue`/`task_id` (which task this run belongs to), `run_id`
(this run's id — the same value appears twice for a re-armed `await_event` waiter),
`task_name` (dotted task path), `args`/`kwargs` (decoded from the enqueued params),
`attempt` (1-based attempt number), `state` (see below), `result` (the task's return
value, once `completed`), `failure`
(`{"message": str, "name"?: str, "traceback"?: str}`, once `failed`).

**Observable `state` values:** `pending` (claimable, not yet run), `sleeping` (suspended
— a durable [sleep](#sleep), an `await_event` wait, or a retry backoff,
indistinguishable from a `RunSnapshot` alone), `completed` (finished successfully),
`failed` (raised, and out of retries), `cancelled` (cancelled before or during
execution).

**`get_result()` honours a prefixed id's own queue.** Where `my_task.get_result(id)`
([Retrieving results](#retrieving-results) above) reads Django's own
`TaskResult.status`, `dj_absurd.get_result` reads Absurd's own states directly —
including `sleeping`, a state `TaskResult.status` can't show — and skips the worker
round-trip; like Django's own method, it raises on a miss (`TaskNotFoundError`).
`task_id` accepts either a bare uuid or Django's own `TaskResult.id` (`"queue:uuid"`) —
whatever `enqueue()` handed back. When it carries a queue prefix, that prefix is what
gets queried, not `queue`'s default:

```python
result = reports_task.enqueue()   # id is "reports:<uuid>"
dj_absurd.get_result(result.id)   # queries the "reports" queue
```

An explicit `queue=` — even `queue="default"` — that disagrees with a prefixed id's own
queue raises `TaskIdQueueMismatchError` naming both. A bare uuid resolves an unpassed
`queue` to `"default"`, same as always.

**Requires `transaction=True`**: Absurd's own work runs on a connection separate from
the test's; under a plain `db` test the enqueued row is invisible to it. `drain`,
`emit`, and `get_result` detect the open transaction and raise rather than silently
no-opping. In a multi-DB project, declare the Absurd alias in that same test's
`databases` too — draining commits real state via the worker's own connection, and an
undeclared alias means the cleanup guard above skips it, leaking that state into the
next test.

**Freeze BEFORE enqueueing.** `freeze_time` and its movers move both Python's clock (via
[time-machine](https://github.com/adamchainz/time-machine)) and Postgres's
`absurd.fake_now` GUC. Freezing to a PAST instant after rows already exist leaves those
rows' deadlines in the database's future relative to the new frozen now, so nothing is
claimable until a later `move_to`/`shift` passes them. A test that never opens a
`freeze_time` block pays nothing: the other members never touch the clock.

**Install time-machine yourself** — it is a dev/test dependency of _your_ project, not
bundled with django-absurd and not one of its extras (`pip install time-machine`).
`sync_queues`/`drain`/`emit`/`get_result`/`now` work without it; only `freeze_time`
imports it, lazily, on first use, and raises `ImproperlyConfigured` naming the install
command if it's missing.

**`TaskSnapshot` fields:** `queue`/`task_id` (which task this is; no queue prefix on
`task_id`), `task_name` (dotted task path), `args`/`kwargs` (decoded from the enqueued
params), `state` (see the state vocabulary above), `attempts` (created, not completed —
see caveats below), `enqueued_at` (when `enqueue()` ran), `result` (the task's return
value, once `completed`), `failure` (`None` except on a terminal failure — see caveats
below).

**`TaskSnapshot` caveats — use `RunSnapshot` for an in-flight retry.** `get_result`
returns a task-level view, which cannot express an in-flight
[retry](https://github.com/lincolnloop/django-absurd/blob/main/docs/web/tasks.md#retries--spawn-options):

- `attempts` counts attempts CREATED, not completed — a task with one failed attempt and
  a pending backoff already reads `attempts=2`, before the second attempt has run.
- `state="sleeping"` covers a retry backoff as well as a durable [sleep](#sleep) — a
  test asserting "my workflow is asleep" would pass just as readily on a task that
  crashed and is waiting to retry.
- `failure` is `None` mid-backoff — `last_attempt_run` already points at the fresh
  pending run by the time the backoff is showing.

`drain()`'s `RunSnapshot` tells these apart: it reports each run's own state right after
that run executes, so a retry sequence reads attempt-by-attempt instead of collapsing to
one ambiguous final read.

**Hazards:**

- A freeze doesn't reach [pg_cron](#pg_cron-backend): its launcher runs in another
  database on its own clock, so moving durable time cannot make a schedule fire. Testing
  one stays "reconcile it in, then inspect `cron.job`", as `tests/pg_cron` does.
- A savepoint rollback inside the block reverts Django's session clock, so a later
  `enqueue()` stamps real time and won't look claimable. Don't enqueue across a rollback
  boundary.

### Getting a `SCHEDULE` into pg_cron for a test

Auto-cleanup only tears down — it has no say over whether a `SCHEDULE` entry lands in
`pg_cron` in the first place, and by default every `cron.*` write is inert under tests
(see [Test databases](#test-databases)). Opt in with
`OPTIONS["PG_CRON_ON_TEST_DB"] = True`, then either let `migrate`'s automatic reconcile
run (also needs `OPTIONS["SYNC_SCHEDULES_ON_TEST_DB"] = True` —
`tests/pg_cron/utils.py::build_pg_cron_tasks` sets both) or call
`call_command("absurd_sync_crons")`. Cleanup then clears whatever ended up in
`cron.job`/`ScheduledTask` — settings-synced, admin-authored, or created directly by the
test — it doesn't care how it got there, only what's present.

## Deployment notes

### Logging

- **`django.tasks`** — Django's own task lifecycle; `AbsurdBackend` emits its signals.
  Portable across backends.
- **`django_absurd`** — what Absurd did: attempts, durations, worker and beat lifecycle,
  steps, replays, sleeps, event waits. One child per module, so
  `django_absurd.scheduler` is the beat and `django_absurd.context` the durable
  primitives — level either down on its own.

`absurd_worker` and `absurd_beat` attach a `StreamHandler` at `INFO` so a fresh project
is not silent. Name `django_absurd` or a child in
[`LOGGING`](https://docs.djangoproject.com/en/6.0/topics/logging/#configuring-logging)
and they stop; name only `root` and they add no handler but still raise the level, so a
`WARNING` root does not swallow them:

```python
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        # everything from this package...
        "django_absurd": {"handlers": ["console"], "level": "INFO"},
        # ...or quiet one part of it
        "django_absurd.scheduler": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
```

Neither logger is the complete record. Postgres is:
[queue state](#querying-queue-state-orm) and the [stored result](#retrieving-results).

### Operational notes

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

## Workflows

Absurd calls these primitives **Steps (Checkpoints)**, **Sleep**, and **Events** — see
[Absurd — Concepts](https://earendil-works.github.io/absurd/concepts/). Call the
matching accessor **inside** a running task to reach them. They let a task break its
work into checkpointed steps, sleep between them, and suspend until a named signal
arrives — persisting progress so retries and resumes pick up where they left off. Both
are orthogonal to Django's `TaskContext` — you do **not** need `takes_context=True` (add
that only if you also want `context.task_result`/`.attempt`).

```python
from django_absurd import aget_absurd_context, get_absurd_context
```

Pick the accessor by task kind — each returns one concrete, fully-typed context, so
there is no cast and no union to narrow:

- **Sync task → `get_absurd_context()`** returns `django_absurd.AbsurdTaskContext`, a
  thin bridge mirroring the SDK's sync signatures (no `await`); it also carries
  `run_step` (sync only).
- **Async task → `aget_absurd_context()`** returns
  `django_absurd.AsyncAbsurdTaskContext`, a wrapper mirroring the SDK's
  `absurd_sdk.AsyncTaskContext` async surface (`await` its methods); `.absurd_ctx`
  reaches the raw SDK context for anything unmirrored.

Called outside a running Absurd task, either accessor raises `RuntimeError`.

### Steps (checkpoints)

`context.step(name, fn)` runs `fn()`, persists the result as a checkpoint, and skips it
on replay — the core of durable execution. Step names must be deterministic and stable
across replays; Absurd uses them to locate the right checkpoint on resume.

→
[Absurd — Concepts: Steps (Checkpoints)](https://earendil-works.github.io/absurd/concepts/#steps-checkpoints)

```python
from django.tasks import task
from django_absurd import get_absurd_context


@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()
    context.step("charge", lambda: charge_card(order_id))
    context.step("ship", lambda: ship(order_id))
```

`context.run_step` is a convenience decorator alternative to `context.step` (sync only):

```python
@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()

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
from django.tasks import task
from django_absurd import aget_absurd_context


@task
async def process_order(order_id: int) -> None:
    context = aget_absurd_context()

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
    context = get_absurd_context()
    context.step("charge", lambda: charge_card(order_id))
    context.sleep_for("cooldown", 5)          # suspend for 5 seconds
    context.step("ship", lambda: ship(order_id))
```

`sleep_until` `wake_at`: pass a timezone-aware `datetime` — a naive `datetime` raises
when compared against Absurd's timezone-aware clock. A Unix timestamp (`int` or `float`)
is always unambiguous. Sleep resume re-claims the same run — the attempt counter does
not increment.

### Events

`context.await_event(event_name, step_name=None, timeout=None)` suspends the task until
a named event arrives, then returns its JSON payload.
`context.emit_event(event_name, payload=None)` emits an event on the task's own queue
(in-task, replay-safe — a re-emit after a retry is a no-op). Events are awaited by name,
carry an optional JSON payload, and **first emit per name wins** (immutable) — a
business-keyed name like `"warehouse.packed:order-42"` targets exactly one waiter.

→ [Absurd: Concepts — Events](https://earendil-works.github.io/absurd/concepts/#events)

Events are **queue-scoped**: `await_event`/`emit_event` operate on the task's own queue.
An event emitted on queue X only wakes a waiter on queue X.

#### The outside-a-task signal: top-level `emit_event`

`ctx.emit_event` only reaches code running _inside_ a task. The real-world signal that
wakes a waiter — a webhook, a view, an API handler — is ordinary Django code, not a
task. `django_absurd.emit_event(event_name, payload=None, *, queue="default")` is that
entry point:

```python
from django.http import HttpResponse

from django_absurd import emit_event


def warehouse_webhook(request, order):
    emit_event(f"warehouse.packed:{order}", {"tracking": request.POST["tracking"]},
               queue="default")
    return HttpResponse(status=204)
```

End-to-end: a task calls `await_event(f"warehouse.packed:{order}")` → suspends (worker
freed) → the warehouse system POSTs the webhook → the view emits the event on the task's
queue → the task's next claim finds it → resumes with the payload.

`queue` must match the queue the waiting task actually runs on — it targets the
client-level `emit_event`'s `queue_name`, not a database alias. An undeclared queue
raises `QueueNotDeclaredError` immediately (fail fast on a typo); a declared but
unprovisioned queue raises `QueueNotProvisionedError` naming
`manage.py absurd_sync_queues` (see [Exceptions](#exceptions) above). `emit_event` is
sync; from an async view, wrap it in `sync_to_async`.

#### Sync

```python
from django.tasks import task
from django_absurd import get_absurd_context


@task
def process_order(order_id: int) -> None:
    context = get_absurd_context()
    context.step("charge", lambda: charge_card(order_id))
    payload = context.await_event(f"warehouse.packed:{order_id}")
    context.step("ship", lambda: ship(order_id, payload))
```

#### Async

```python
from django.tasks import task
from django_absurd import aget_absurd_context


@task
async def process_order(order_id: int) -> None:
    context = aget_absurd_context()
    payload = await context.await_event(f"warehouse.packed:{order_id}")

    async def ship_order():
        return await ship(order_id, payload)

    await context.step("ship", ship_order)
```

#### Timeout

Pass `timeout` (seconds) to stop waiting after a bound. On timeout, `await_event` raises
`absurd_sdk.TimeoutError` — **not** the builtin `TimeoutError`:

```python
import absurd_sdk
from django.tasks import task
from django_absurd import get_absurd_context


@task
def process_order(order_id: int) -> str:
    context = get_absurd_context()
    try:
        context.await_event(f"warehouse.packed:{order_id}", timeout=3600)
    except absurd_sdk.TimeoutError:
        return "gave up waiting for the warehouse"
    return "shipped"
```

An **uncaught** `TimeoutError` fails the run, which then retries and re-waits the full
`timeout` on each attempt until `max_attempts` — catch it if you want a one-shot
timeout.

#### `await_task_result` is not provided

Absurd's SDK version of this polls + heartbeats inside a step rather than suspending
(holding the worker slot), and is cross-queue-only. For a child task's result, use
Django's `get_result()` / `aget_result()` instead.

### API reference

| Method / property                                       | Sync | Async   | What it does                                              |
| ------------------------------------------------------- | ---- | ------- | --------------------------------------------------------- |
| `step(name, fn)`                                        | yes  | `await` | Run `fn()`, checkpoint the result; skip on replay         |
| `sleep_for(step_name, duration)`                        | yes  | `await` | Suspend the task for `duration` seconds                   |
| `sleep_until(step_name, wake_at)`                       | yes  | `await` | Suspend until a `datetime`, Unix timestamp, or float      |
| `await_event(event_name, step_name=None, timeout=None)` | yes  | `await` | Suspend until the named event arrives; return its payload |
| `emit_event(event_name, payload=None)`                  | yes  | `await` | Emit an event on the task's own queue (replay-safe)       |
| `heartbeat(seconds=None)`                               | yes  | `await` | Extend the claim timeout (keep the run alive)             |
| `headers`                                               | yes  | yes     | Read-only mapping of headers passed at enqueue time       |
| `run_step([name])` (decorator)                          | yes  | —       | Convenience wrapper around `step`; derives name from `fn` |

### Caveats

**Effectively-once, not exactly-once.** A step's result is persisted to the database
after `fn` returns, on a separate connection. In the window between `fn` completing and
the checkpoint being written, a crash re-runs the step. Design side effects to be
idempotent (for example, use `idempotency_key` on downstream enqueues, or make database
writes upserts).

**Don't catch-all `except` in a task.** Absurd suspends and cancels runs via
control-flow exceptions raised inside `step`/`sleep_for`/`sleep_until`/`await_event`. A
bare `except:` or `except Exception:` around a durable call swallows them and silently
breaks suspension — let them propagate.

**Absurd backend only.** `get_absurd_context()` / `aget_absurd_context()` (and
`step`/`sleep_for`/`sleep_until`/`await_event`/`emit_event` on the returned context) are
Absurd-specific. Calling them under any other Django task backend — where the Absurd
runtime context is never set — raises `RuntimeError`.

Absurd's durable-execution rules also apply — deterministic step naming/order,
JSON-serializable step return values, and finishing a step within `claim_timeout` (or
calling `context.heartbeat()`); see
[Absurd — Concepts](https://earendil-works.github.io/absurd/concepts/).

**Events are subject to cleanup_ttl.** An event emitted long before a delayed
`await_event` can be cleaned up by the queue's `cleanup_ttl` before the waiter ever
checks — the waiter then never wakes. Keep `cleanup_ttl` generous relative to how long a
waiter might sleep before checking.

**`TimeoutError` is `absurd_sdk.TimeoutError`, not the builtin.** `except TimeoutError:`
silently catches nothing — `import absurd_sdk` and catch `absurd_sdk.TimeoutError`.

## Notes

- Migrations are offline — the schema comes only from the pinned Absurd version shipped
  with this package; never fetch at migrate time.
- Alpha software; APIs may change between versions.
