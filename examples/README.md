# django-absurd example

A single-file [nanodjango](https://nanodjango.dev) app using **django-absurd** as the
[Django Tasks](https://docs.djangoproject.com/en/6.0/topics/tasks/) backend, backed by
Postgres. Fully containerized — `docker compose up` runs the whole thing; no host
Python, uv, or Postgres needed.

It enqueues a task from a web form, runs it in a separate worker process, and exposes
Absurd's queue tables through the Django admin (which django-absurd auto-registers). A
`ping` task is scheduled every minute via **pg_cron** — Postgres fires it directly, no
beat process needed.

## Layout

```
examples/
  compose.yaml       # db (pg_cron Postgres) + app (web/admin) + worker (absurd_worker)
  Dockerfile         # image built from the repo root so it installs local django-absurd
  initdb.d/          # Postgres init scripts: 01_pgcron.sql installs the extension
  migrations/        # reference Django migration for non-nanodjango projects (see note)
  pyproject.toml     # deps: nanodjango, django-absurd (local path), psycopg[binary]
  app.py             # the whole app: Django(...) config, add + ping tasks, views, admin
```

## Run it

From this `examples/` directory:

```bash
docker compose up --build
```

That brings up three services:

1. **db** — pg_cron-enabled Postgres (`shared_preload_libraries=pg_cron`). On first
   start, `initdb.d/01_pgcron.sql` runs as superuser and creates the `pg_cron`
   extension. This is the operator-side installation step that django-absurd does not
   ship — each project installs its own `CREATE EXTENSION`.
2. **app** — migrates (with `post_migrate` reconciling the schedule into pg_cron),
   creates an `admin` / `admin` superuser (idempotent), and serves the web app + admin
   on **http://localhost:8000/**.
3. **worker** — a long-lived `absurd_worker` consuming the `default` queue; it starts
   once the app is healthy. pg_cron fires `ping` every minute; the worker drains it and
   logs **"pong 🏓"**.

Then, in a browser:

- **http://localhost:8000/** — submit `add(a, b)`; you're redirected to a task page that
  auto-refreshes until the worker finishes and shows the result.
- **http://localhost:8000/admin/** — log in as **admin / admin** and browse **Tasks**,
  **Runs**, **Checkpoints**, **Events**, **Waits**, and the **Queues** catalog (all
  read-only, filterable by queue).

Tear down with:

```bash
docker compose down -v
```

## Try more

```bash
# Tail the worker's logs — per-task lines plus "pong 🏓" every minute (pg_cron fires it)
docker compose logs -f worker

# Run a one-off management command against the stack
docker compose run --rm worker nanodjango manage app.py absurd_sync_queues
docker compose run --rm worker nanodjango manage app.py absurd_sync_crons
docker compose run --rm worker nanodjango manage app.py check
```

## Notes

- The superuser is created by `nanodjango run … --user=admin --pass=admin` (in the
  Dockerfile) — insecure, for the local demo only.
- `nanodjango run` makes + applies migrations and creates the superuser before serving,
  so a single `docker compose up` is fully self-provisioning.
- django-absurd requires the **psycopg (v3)** PostgreSQL backend — `app.py` overrides
  nanodjango's sqlite default with `django.db.backends.postgresql`.
- **pg_cron installation:** `initdb.d/01_pgcron.sql` runs
  `CREATE EXTENSION IF NOT EXISTS pg_cron` at DB init time (as superuser). This is the
  **operator-side** installation step that django-absurd never ships — each project owns
  its pg_cron setup. In a standard Django project (not nanodjango), this would be a
  migration `RunSQL("CREATE EXTENSION IF NOT EXISTS pg_cron")` in your app. The
  `migrations/` directory in this example shows what such a migration looks like.
- With `SCHEDULER="pg_cron"`, the worker runs without `--beat` — Postgres schedules
  tasks directly. Beat and pg_cron are mutually exclusive per backend.
- Tasks are delivered at-least-once, so handlers should be idempotent.
