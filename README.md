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

Backend `OPTIONS` (all optional): `DATABASE` (the `DATABASES` alias to use, default
`"default"`), `DEFAULT_MAX_ATTEMPTS` (default `5`), and `QUEUES` (a map of queue name →
`absurd_sdk.CreateQueueOptions` for per-queue config).

## Setup

```console
python manage.py migrate              # create Absurd's schema (offline, shipped SQL)
python manage.py absurd_sync_queues   # create/update the configured queues
python manage.py absurd_worker        # run a worker
```

`migrate` does not create any queues — run `absurd_sync_queues` after configuring
`QUEUES`. Validate configuration at any time with
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

## Deployment notes

- **Database privileges.** `migrate` runs `CREATE EXTENSION IF NOT EXISTS "uuid-ossp"`
  and `CREATE SCHEMA IF NOT EXISTS absurd`, so the migrating role needs rights to create
  extensions and schemas (a superuser, or a role granted those — with `uuid-ossp`
  allow-listed on managed Postgres). The schema name `absurd` is fixed.
- **At-least-once delivery.** A task may run more than once (e.g. a crash between the
  handler committing and Absurd's bookkeeping). Keep handlers idempotent; use
  `idempotency_key` where it helps.
- **Queue sync is additive.** `absurd_sync_queues` creates/updates configured queues but
  never drops queues removed from config. A queue's `storage_mode` is immutable after
  creation (a change is reported as a warning, not applied).
- **Teardown is destructive.** `migrate django_absurd zero` drops the `absurd` schema
  and all data in it.

## Adopting an existing Absurd database

If the target database already runs Absurd (its schema managed outside Django), fake the
initial migration so Django records it without re-running the DDL:

```console
python manage.py migrate --fake django_absurd 0001
```

## License

MIT — see [LICENSE](LICENSE).
