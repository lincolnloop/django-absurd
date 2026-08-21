# django-absurd — pg_cron example

Demonstrates DB-side scheduling with django-absurd and nanodjango.

- Postgres fires `ping` every minute via pg_cron — no beat process needed.
- The worker drains the queue and logs `pong 🏓` each run.
- The `pg_cron` extension is installed once, centrally, by a
  `/docker-entrypoint-initdb.d` script that `Dockerfile.pg_cron` writes on container
  startup — not by a django-absurd migration. django-absurd schedules jobs
  cross-database into the app's own `demo` database, which never holds the extension.
- The compose `db` service sets `shared_preload_libraries=pg_cron` and
  `cron.database_name=postgres` (Postgres server prerequisites for pg_cron); `demo` is a
  separate, ordinary database on the same server.
- Browse queue tables and task runs in the auto-registered admin.

django-absurd is installed from the local checkout so the demo runs against this
branch's code.

## Run

```
docker compose up
```

- `docker compose logs worker` — watch for `pong 🏓` each minute
- `http://localhost:8000/admin/` — Tasks / Runs growing (login: **admin** / **admin**)

Running more than one example at once? Override the published port:
`APP_PORT=8010 docker compose up`.

Tear down (remove volumes before re-running): `docker compose down -v`

## Test

This suite is only meaningful **in-stack** — a host run reaches the repo's plain
database on 5432, which has no `pg_cron` extension:

```
docker compose up -d --build --wait db
docker compose run --rm --build app sh -c "cd /app && pytest"
```
