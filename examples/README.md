# django-absurd example — pg_cron scheduler

A minimal, standard Django project using **django-absurd** as the
[Django Tasks](https://docs.djangoproject.com/en/6.0/topics/tasks/) backend, with the
schedule driven **database-side by [`pg_cron`](https://github.com/citusdata/pg_cron)**.
Postgres fires a `ping` task every minute — no beat process — and a worker drains it and
logs `pong 🏓`.

The `pg_cron` extension is created by a **Django migration** using
[`CreateExtension`](https://docs.djangoproject.com/en/stable/ref/contrib/postgres/operations/#django.contrib.postgres.operations.CreateExtension)
(see [`demo/migrations/0001_pg_cron.py`](demo/migrations/0001_pg_cron.py)) — the
standard way to install an extension in a Django project.

## Layout

```
examples/
  compose.yaml            # db (pg_cron Postgres) + migrate (one-shot) + worker
  Dockerfile              # example image; deps from pyproject, source bind-mounted
  pyproject.toml          # deps: django, psycopg[binary], django-absurd (local path)
  manage.py
  config/                 # project: settings.py (TASKS + pg_cron), urls.py, wsgi.py
  demo/                   # app
    tasks.py              #   the ping/pong @task
    migrations/
      0001_pg_cron.py      #   CreateExtension("pg_cron")
```

## How deps and source work (dev / bind-mount style)

Following the [django-layout](https://github.com/lincolnloop/django-layout) idioms:

- **Dependencies** are declared in [`pyproject.toml`](pyproject.toml). django-absurd is
  installed from the **local checkout** (the parent repo) as an editable path
  dependency, so the example exercises _this branch's_ code — not a released version.
- The image installs those deps into a venv at `/opt/venv`; it does **not** `COPY` the
  app source in. Instead compose **bind-mounts** the example source (`./ → /app`) and
  the `django_absurd` package (`../django_absurd → /src/django_absurd`) at run time, so
  edits are picked up without a rebuild.

## Run it

From this `examples/` directory:

```bash
docker compose up --build
```

Three services come up in order:

1. **db** — Postgres with `pg_cron`. `shared_preload_libraries=pg_cron` is set as a
   server GUC in the compose `command` (it must be loaded at server start, before any
   `CREATE EXTENSION` — a migration can't enable it). `cron.database_name=demo` points
   `pg_cron` at the app's database so the extension can be created there and jobs run
   against it.
2. **migrate** — a one-shot `manage.py migrate`. The `demo.0001_pg_cron` migration runs
   `CreateExtension("pg_cron")` (as the superuser `postgres` role), then django-absurd's
   `post_migrate` handler reconciles the `SCHEDULE` into a `pg_cron` job.
   Extension-first ordering holds naturally: `post_migrate` fires after all migrations.
   The container exits when done.
3. **worker** — a long-lived `absurd_worker` consuming the `default` queue, started once
   `migrate` completes successfully. With `SCHEDULER="pg_cron"` there is **no beat** —
   Postgres fires `ping` every minute; the worker drains it and logs **`pong 🏓`**.

Tail the worker to watch it fire (within a minute):

```bash
docker compose logs -f worker
```

Tear down (removes the volume, so the extension/schedule are recreated next run):

```bash
docker compose down -v
```

## Verify the wiring

```bash
# The extension was created by the migration:
docker compose exec db psql -U postgres -d demo -c '\dx'

# The pg_cron job was materialized by the reconcile:
docker compose exec db psql -U postgres -d demo -c 'select jobname, schedule, active from cron.job;'

# pg_cron's own record of firings:
docker compose exec db psql -U postgres -d demo -c 'select jobid, status from cron.job_run_details order by runid desc limit 5;'
```

## Notes

- django-absurd requires the **psycopg (v3)** PostgreSQL backend — the Absurd SDK reuses
  Django's connection. `config/settings.py` uses `django.db.backends.postgresql`.
- The migration role must be a **superuser** (or hold `CREATE ON DATABASE`) for
  `CreateExtension` to succeed; the demo connects as the compose `postgres` superuser.
- `SCHEDULER="pg_cron"` and the beat are **mutually exclusive** per backend — the worker
  runs without `--beat`.
- Tasks are delivered at-least-once, so handlers should be idempotent.
- Insecure demo settings (`SECRET_KEY`, `DEBUG=True`, `ALLOWED_HOSTS=["*"]`) — local
  demo only. To browse the queue tables in the admin, add `manage.py createsuperuser`
  and mount the app (the admin is auto-registered by django-absurd; see
  [`config/urls.py`](config/urls.py)).
- See the [Cron Jobs docs](../docs/web/cron-jobs.md) for the full `pg_cron` scheduler
  reference.
