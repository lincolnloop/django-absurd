# django-absurd — integration guide

A guide for developers integrating **django-absurd** into a Django project. This file
ships inside the installed package (`site-packages/django_absurd/AGENTS.md`), so it
stays discoverable from a project's virtualenv (and by coding agents working there).

django-absurd plugs [Absurd](https://earendil-works.github.io/absurd/), a
Postgres-native workflow engine, into Django's Tasks framework. It reuses Django's
database connection and ships Absurd's schema as Django migrations — no separate broker.

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
