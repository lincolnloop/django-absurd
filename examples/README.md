# django-absurd examples

Four small, self-contained [nanodjango](https://github.com/radiac/nanodjango) demos —
each in its own directory with its own `docker compose`. Run **one at a time** (they all
serve on http://localhost:8000; admin login `admin` / `admin`).

- **[`web/`](web/)** — enqueue `add(a, b)` from a form and watch the result
  (`get_result`); browse the read-only queue tables in the admin. Also demonstrates
  **Steps (checkpoints) and Events** at `/workflow/` — an order-fulfillment task that
  checkpoints each step and suspends on `await_event` until a "mark packed" button
  (calling the top-level `emit_event`) wakes it, with a link into the task's admin page
  to watch its checkpoints and suspended wait.
- **[`sleep/`](sleep/)** — **durable sleep** (`context.sleep_for`): a checkpointed
  onboarding-email sequence that suspends for real between messages, resuming on
  schedule even if the worker restarts mid-sleep.
- **[`beat/`](beat/)** — the in-process **beat** scheduler firing a task every minute.
- **[`pg_cron/`](pg_cron/)** — the **pg_cron** scheduler firing a task directly from
  Postgres (no beat process).

```bash
cd web        # or: cd sleep / cd beat / cd pg_cron
docker compose up
# open http://localhost:8000/  (admin at /admin/, login admin / admin)
```

Each demo installs django-absurd from this checkout (editable path dependency), so it
exercises the local source, and `nanodjango run` applies migrations and creates the
`admin`/`admin` superuser on startup.

## Running the tests

Each demo has a pytest suite (high-level HTTP for `web`; enqueue-and-drain for the
scheduled `beat`/`pg_cron` tasks — the schedulers themselves are covered in
django-absurd's own tests). Tests run **inside each demo's own Docker stack**, against
its own compose database:

```bash
cd examples/web        # or: cd examples/beat, cd examples/pg_cron
docker compose up -d --build --wait db
docker compose run --rm --build app sh -c "cd /app && pytest"
```

**pg_cron** runs the same way — no special setup. The extension lives on the central
`cron.database_name` (`postgres`) database, installed once by the compose `db` image;
each demo's own database (and pytest-django's ordinary `test_*` database) holds no
extension and is scheduled into cross-database.
