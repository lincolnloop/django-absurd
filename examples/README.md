# django-absurd example

A single-file [nanodjango](https://nanodjango.dev) app using **django-absurd** as the
[Django Tasks](https://docs.djangoproject.com/en/6.0/topics/tasks/) backend, backed by
Postgres. Fully containerized — `docker compose up` runs the whole thing; no host
Python, uv, or Postgres needed.

It enqueues a task from a web form, runs it in a separate worker process, and exposes
Absurd's queue tables through the Django admin (which django-absurd auto-registers).

## Layout

```
examples/
  compose.yaml   # db (Postgres, internal-only) + app (web/admin) + worker (absurd_worker)
  Dockerfile     # image built from the repo root so it installs local django-absurd
  pyproject.toml # deps: nanodjango, django-absurd (local path), psycopg[binary]
  app.py         # the whole app: Django(...) config, the add task, two views, admin
```

## Run it

From this `examples/` directory:

```bash
docker compose up --build
```

That brings up three services:

1. **db** — Postgres, reachable only over the compose network (no published host port).
2. **app** — migrates, creates an `admin` / `admin` superuser (idempotent), and serves
   the web app + admin on **http://localhost:8000/**.
3. **worker** — a long-lived `absurd_worker` consuming the `default` queue; it starts
   once the app is healthy (i.e. migrations have run).

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
# Tail just the worker's per-task logs
docker compose logs -f worker

# Run a one-off management command against the stack
docker compose run --rm worker nanodjango manage app.py absurd_sync_queues
docker compose run --rm worker nanodjango manage app.py check
```

## Notes

- The superuser is created by `nanodjango run … --user=admin --pass=admin` (in the
  Dockerfile) — insecure, for the local demo only.
- `nanodjango run` makes + applies migrations and creates the superuser before serving,
  so a single `docker compose up` is fully self-provisioning.
- django-absurd requires the **psycopg (v3)** PostgreSQL backend — `app.py` overrides
  nanodjango's sqlite default with `django.db.backends.postgresql`.
- Tasks are delivered at-least-once, so handlers should be idempotent.
