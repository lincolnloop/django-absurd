# Examples: three nanodjango apps (web / beat / pg_cron) — design

Replace the single dual-scheduler example (`examples/config` + `examples/demo` +
top-level compose) with THREE self-contained single-file **nanodjango** apps, each in
its own dir with its own isolated compose. Each demos one facet of django-absurd. Run
one at a time.

Unblocked by: the `django_absurd.pg_cron` app now creates the pg_cron extension via its
OWN migration (`CreateExtension`), so an example needs no custom migration — the
friction that forced the move off nanodjango is gone.

## Goal

Small, focused, runnable demos a reader can `docker compose up` and poke at
`http://localhost:8000`. One facet each; no cross-contamination; single-file `app.py`.

## Layout

```
examples/
  web/      app.py  compose.yaml  Dockerfile  pyproject.toml  README.md
  beat/     app.py  compose.yaml  Dockerfile  pyproject.toml  README.md
  pg_cron/  app.py  compose.yaml  Dockerfile  pyproject.toml  README.md
```

Per app: deps in `pyproject.toml` (`nanodjango`, `django`, `psycopg[binary]`,
`django-absurd` as editable path dep on `../..`), source bind-mounted (dev style, like
the current example). Nanodjango single-file `app.py` =
`Django(ADMIN_URL="admin/", EXTRA_APPS=[...], DATABASES=..., TASKS=..., LOGGING=...)` +
`@task`s + (web only) `@app.route` views.

## The three apps

### web — enqueue + result (no scheduler)

Revive the pre-`5d5051b` `app.py` view (`git show 5d5051b~1:examples/app.py`), trimmed
to enqueue+result (drop its `SCHEDULER="pg_cron"`/`SCHEDULE`): an `add(a, b)` `@task`,
`/` form that enqueues it, `/tasks/<result_id>/` page that fetches the `TaskResult` via
`get_result`, admin at `/admin/`. Plain Postgres. `EXTRA_APPS=["django_absurd"]`. Keep
minimal — improve the view over time.

### beat — beat scheduler + admin (plain Postgres)

A `tick` `@task`; `OPTIONS["SCHEDULER"]="beat"` (default) + `SCHEDULE` firing every
minute; worker runs with `--beat`. Observe `Tasks`/`Runs` filling in the auto-registered
admin. `EXTRA_APPS=["django_absurd"]`. No custom frontend — the django-absurd admin is
the UI.

### pg_cron — pg_cron scheduler + admin (pg_cron Postgres)

A `ping` `@task`; `EXTRA_APPS=["django_absurd","django_absurd.pg_cron"]`;
`OPTIONS["SCHEDULER"]="pg_cron"` + `SCHEDULE`; worker WITHOUT `--beat`. The pg_cron
app's migration creates the extension (no demo migration). Postgres built from
`Dockerfile.pg_cron` with `shared_preload_libraries=pg_cron` +
`cron.database_name=<db>`. Observe in admin.

## Compose (per app, isolated)

Services per app:

- **db** — Postgres (plain for web/beat; `Dockerfile.pg_cron` + GUCs for pg_cron).
- **web** — starts the nanodjango server on host **8000** (reachable). Its start script
  first runs migrations, then creates the superuser **admin / admin** non-interactively
  (`createsuperuser --noinput`, `DJANGO_SUPERUSER_USERNAME=admin` /
  `DJANGO_SUPERUSER_PASSWORD=admin` / email env), then serves.
- **worker** — `absurd_worker` (beat app adds `--beat`; pg_cron app does not).

No host ports beyond 8000. Run each app independently
(`cd examples/<app> && docker compose up`), so all three can reuse 8000 without
collision.

## Shared / constraints

- Superuser **admin:admin** set in the web service's start script (env-kwargs
  mechanism).
- django-absurd auto-registers the read-only admin (Tasks/Runs/Checkpoints/Events/Waits/
  Queues) when `django.contrib.admin` is installed (nanodjango installs it).
- **No durable-workflow authoring API** exists (enqueue + `get_result` + read-only
  introspection only) — so `web` is enqueue+result; no sleep/wait/checkpoint demo.
- psycopg3 required (nanodjango defaults to sqlite → override `DATABASES`).
- nanodjango 0.16.x supports Django ≥5.2 (6.0 floor satisfied) — verified.

## Non-goals

Sub-minute cron (beat-only, not demoed). Multi-DB / dual-backend (the old example's
two-backends-one-DB is dropped — each app is single-backend). A fancy web UI (later).
Unit tests for the examples (verified live).

## Testing / verification

Examples carry no unit tests. Verify each live during build:

- **web**: `docker compose up`; open `/`, enqueue `add`, follow to `/tasks/<id>/` →
  result; `/admin/` (admin:admin) shows the Task/Run.
- **beat**: `up`; within ~1 min a `tick` Task/Run appears in admin + `tock ⏰` in logs.
- **pg_cron**: `up`; extension present (`\dx`), a `ping` fires (`pong 🏓`), admin shows
  it.

## Branch

Bundle into draft PR #43 (`pgcron-scheduler`) — it already carries
`django_absurd.pg_cron`, which the pg_cron app depends on.
