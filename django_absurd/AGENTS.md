# django-absurd — agent guide

Guidance for coding agents integrating **django-absurd** into a Django project. This
file ships inside the installed package (`site-packages/django_absurd/AGENTS.md`) so it
is discoverable from a project's virtualenv.

django-absurd plugs [Absurd](https://earendil-works.github.io/absurd/), a
Postgres-native workflow engine, into Django's Tasks framework. It reuses Django's
database connection and ships Absurd's schema as Django migrations.

## Hard requirements

- **Python 3.12+**, **Django 6.0+**.
- **PostgreSQL via the psycopg (v3) Django backend** — `django.db.backends.postgresql`
  with psycopg3 installed. The Absurd SDK reuses Django's connection; psycopg2 will not
  work. The package asserts this at runtime; do not work around it.

## Configure

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

- `DATABASE` — which `DATABASES` alias to use (default: `"default"`).
- `DEFAULT_MAX_ATTEMPTS` — retry ceiling per task (default: `5`).
- `QUEUES` — map of queue name → `absurd_sdk.CreateQueueOptions` for per-queue config.

## Run

```bash
python manage.py migrate              # apply Absurd's schema (offline, shipped SQL)
python manage.py absurd_sync_queues   # create/update declared queues
python manage.py absurd_worker        # run a worker
```

## Validate

Run `python manage.py check django_absurd` and resolve everything it reports before
relying on the setup. Fix the configuration it points at rather than silencing the
check.

## Enqueue

Use Django's Tasks API. Absurd parameters attach two ways — both live in
`django_absurd.params`:

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

## Notes

- Migrations are offline — the schema comes only from the pinned Absurd version shipped
  with this package; never fetch at migrate time.
- Alpha software; APIs may change between versions.
