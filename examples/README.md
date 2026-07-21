# django-absurd examples

Three small, self-contained [nanodjango](https://github.com/radiac/nanodjango) demos —
each in its own directory with its own `docker compose`. Run **one at a time** (they all
serve on http://localhost:8000; admin login `admin` / `admin`).

- **[`web/`](web/)** — enqueue `add(a, b)` from a form and watch the result
  (`get_result`); browse the read-only queue tables in the admin. Also demonstrates
  **Steps (checkpoints), Sleep, and Events** at `/workflow/` — an order-fulfillment task
  that checkpoints each step and suspends on `await_event` until a "mark packed" button
  (calling the top-level `emit_event`) wakes it, with a link into the task's admin page
  to watch its checkpoints and suspended wait.
- **[`beat/`](beat/)** — the in-process **beat** scheduler firing a task every minute.
- **[`pg_cron/`](pg_cron/)** — the **pg_cron** scheduler firing a task directly from
  Postgres (no beat process).

```bash
cd web        # or: cd beat / cd pg_cron
docker compose up
# open http://localhost:8000/  (admin at /admin/, login admin / admin)
```

Each demo installs django-absurd from this checkout (editable path dependency), so it
exercises the local source, and `nanodjango run` applies migrations and creates the
`admin`/`admin` superuser on startup.
