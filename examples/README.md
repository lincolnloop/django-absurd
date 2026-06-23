# django-absurd example

A minimal Django project using **django-absurd** as the
[Django Tasks](https://docs.djangoproject.com/en/6.0/topics/tasks/) backend, backed by
Postgres. Fully containerized — `docker compose up` runs the whole thing; no host
Python, uv, or Postgres needed.

It defines two `@task` functions in `demo/tasks.py`, enqueues them, and runs the
`absurd_worker` to execute them.

## Layout

```
examples/
  compose.yaml              # db (Postgres) + app (the Django demo); no host ports
  Dockerfile                # app image (built from the repo root so it uses local django-absurd)
  pyproject.toml            # deps: django>=6, django-absurd (local path), psycopg[binary]
  manage.py
  demo_project/settings.py  # INSTALLED_APPS + DATABASES + TASKS(AbsurdBackend) + router
  demo/tasks.py             # @task add / create_user
  demo/management/commands/enqueue_demo.py
```

## Run it

From this `examples/` directory:

```bash
docker compose up --build
```

That brings up Postgres, waits for it, then the `app` service runs the full flow and
exits `0`:

1. `manage.py migrate` — creates the auth tables **and** the Absurd schema.
2. `manage.py absurd_sync_queues` — provisions the queues declared in `TASKS`.
3. `manage.py enqueue_demo` — enqueues `add(2, 3)` and `create_user("alice")`.
4. `manage.py absurd_worker --queue default --burst` — drains the queue, runs both tasks
   (you'll see per-task start/completed logs), then exits.

You'll see the worker execute both tasks in the logs. Clean up with:

```bash
docker compose down -v
```

## Try more

Run one-off commands against the running stack with `docker compose run`:

The image autoloads the virtualenv (it's on `PATH`), so commands are plain
`python manage.py …` — no `uv run` prefix:

```bash
# Enqueue again
docker compose run --rm app python manage.py enqueue_demo

# Run a long-lived blocking worker (Ctrl-C to stop); supports --concurrency N etc.
docker compose run --rm app python manage.py absurd_worker --queue default --concurrency 4

# Validate the TASKS / queue configuration
docker compose run --rm app python manage.py check
```

## Worker modes

- **Burst** (`--burst`): process the available backlog, then exit `0` — what the default
  `compose up` uses (good for cron / one-shot drains).
- **Blocking** (no `--burst`): long-running; polls until `SIGINT`/`SIGTERM`. Supports
  `--concurrency N` (thread pool), `--claim-timeout`, `--poll-interval`, `--batch-size`,
  `--worker-id`.

## Notes

- Tasks must live in an installed app's `tasks.py` (the worker discovers them there).
- django-absurd requires the **psycopg (v3)** PostgreSQL backend — Django selects it
  automatically for `django.db.backends.postgresql` when `psycopg` is installed.
- The app connects to Postgres over the compose network (`db:5432`); the demo task
  `create_user` uses `get_or_create` because Absurd delivers at-least-once (handlers
  should be idempotent).
