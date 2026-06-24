# django-absurd

Django integration for [Absurd](https://earendil-works.github.io/absurd/), the
Postgres-native workflow engine. Wraps Absurd's SDK so it reuses Django's database
connection, ships its schema as Django migrations, and exposes its queues and tasks
through Django settings, management commands, and system checks.

> **Alpha.** APIs and behavior may change between releases.

## Requirements

- Python 3.12+
- Django 6.0+
- PostgreSQL with the **psycopg (v3)** Django backend (the Absurd SDK reuses Django's
  connection and requires psycopg3)

## Installation

```console
pip install django-absurd
```

Pre-release tags (e.g. `v0.1.0a1`) upload as PyPI pre-releases, which `pip install`
skips unless you pass `--pre`:

```console
pip install --pre django-absurd
```

## Configuration

Add the app, register the router, and point Django's `TASKS` setting at the backend:

```python
INSTALLED_APPS = [
    # ...
    "django_absurd",
]

DATABASE_ROUTERS = ["django_absurd.routers.AbsurdRouter"]

TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default"],  # queue names this app enqueues to
    },
}
```

Backend `OPTIONS` (all optional):

- `DATABASE` — the `DATABASES` alias to use (default `"default"`).
- `DEFAULT_MAX_ATTEMPTS` — retry ceiling per task (default `5`).
- `QUEUES` — a map of queue name → `absurd_sdk.CreateQueueOptions`, for per-queue
  customization. Use this _instead of_ the top-level `QUEUES` list (which only names
  queues) — declare queues in one place or the other, never both (setting both is a
  configuration error).

## Setup

```console
python manage.py migrate              # create Absurd's schema (offline, shipped SQL)
python manage.py absurd_worker        # run a worker (auto-creates its queue)
```

Declared queues are **created automatically** on first use — the first `enqueue` to a
queue, or worker start — so no provisioning step is required. `absurd_sync_queues`
remains available for eager/explicit provisioning and for reconciling per-queue policy
changes, but it is no longer a prerequisite. Validate configuration at any time with
`python manage.py check django_absurd`.

## Defining and enqueuing tasks

Use Django's Tasks API. Attach Absurd options per task (a decorator, applied _below_
`@task`) or per call:

```python
from django.tasks import task
from django_absurd.params import AbsurdSpawnParams, absurd_default_params

@task
@absurd_default_params(max_attempts=3)
def send_report(user_id): ...

send_report.enqueue(42)
send_report.enqueue(42, absurd_spawn_params=AbsurdSpawnParams(idempotency_key="report-42"))
```

Parameters: `max_attempts`, `retry_strategy`, `cancellation` (defaults and per call),
plus `headers` and `idempotency_key` (per call). Enqueuing rides the surrounding Django
transaction — a task spawned inside `atomic()` is rolled back if the block fails
(enqueue-on-commit, automatic).

Tasks may be **sync (`def`) or async (`async def`)** — one worker runs both (see
[Workers](#workers)); `async def` tasks may use Django's async ORM. Tasks are resolved
by import path, so they can live in any importable module (no `tasks.py` requirement).

## Workers

```console
python manage.py absurd_worker --queue default
```

A single worker runs **both** sync and async tasks: `async def` tasks run on an event
loop (true concurrency for I/O-bound work), sync `def` tasks run in a thread pool.

- **Blocking** (default): long-running; polls until `SIGINT`/`SIGTERM`.
- **Burst** (`--burst`): drain the current backlog, then exit `0` (cron / one-shot).
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
- **Queue creation is automatic and additive.** Declared queues are created on first use
  (enqueue / worker start); worker start also reconciles a served queue's mutable
  policy. Neither auto-create nor `absurd_sync_queues` ever drops queues removed from
  config. A queue's `storage_mode` is immutable after creation (a declared change is
  reported as a warning, not applied). Only queues **declared in `QUEUES`** are
  auto-created — an undeclared queue name is rejected, not silently created.
- **Teardown is destructive.** `migrate django_absurd zero` drops the `absurd` schema
  and all data in it.

## Adopting an existing Absurd database

If the target database already runs Absurd (its schema managed outside Django), you can
fake django-absurd's migration so Django records it as applied without re-running the
DDL:

```console
python manage.py migrate --fake django_absurd
```

> **Use extreme caution.** Faking tells Django the schema is already present without
> checking it. Only do this when the existing `absurd` schema exactly matches the
> version django-absurd targets (`django_absurd.ABSURD_SCHEMA_VERSION`) — a mismatch
> causes runtime failures Django cannot detect. Verify the versions line up before
> faking.

## License

MIT — see [LICENSE](LICENSE).
