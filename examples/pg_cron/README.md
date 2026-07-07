# django-absurd — pg_cron example

Demonstrates DB-side scheduling with django-absurd and nanodjango.

- Postgres fires `ping` every minute via pg_cron — no beat process needed.
- The worker drains the queue and logs `pong 🏓` each run.
- The `django_absurd.pg_cron` app migration creates the `pg_cron` extension.
- The compose `db` service sets `shared_preload_libraries=pg_cron` and
  `cron.database_name=demo` (Postgres server prerequisites for pg_cron).
- Browse queue tables and task runs in the auto-registered admin.

django-absurd is installed from the local checkout so the demo runs against this
branch's code.

## Run

```
docker compose up
```

- `docker compose logs worker` — watch for `pong 🏓` each minute
- `http://localhost:8000/admin/` — Tasks / Runs growing (login: **admin** / **admin**)

Tear down (remove volumes before re-running): `docker compose down -v`
