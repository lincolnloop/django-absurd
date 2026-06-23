# django-absurd — agent guide

Guidance for coding agents integrating **django-absurd** into a Django project. This
file ships inside the installed package (`site-packages/django_absurd/AGENTS.md`) so it
is discoverable from a project's virtualenv.

django-absurd is a Django integration for
[Absurd](https://earendil-works.github.io/absurd/), a Postgres-native workflow engine.
It reuses Django's database connection, ships Absurd's schema as Django migrations, and
surfaces queues/tasks through Django settings, management commands, and system checks.

## Hard requirements

- **Python 3.12+**, **Django 6.0+**.
- **PostgreSQL via the psycopg (v3) Django backend.** The Absurd SDK reuses Django's
  connection and requires psycopg3 — `django.db.backends.postgresql` with psycopg3
  installed. A psycopg2 setup will not work. The app asserts this; do not work around
  it.
- Targets `DATABASES['default']`.

## Setup checklist

1. Install: `pip install django-absurd` (pre-releases: add `--pre`).
2. Add `django_absurd` to `INSTALLED_APPS`.
3. Configure Django's `TASKS` setting to use the Absurd backend (see the project README
   and the upstream Absurd docs for backend path and `OPTIONS`).
4. Run `python manage.py migrate` — Absurd's schema is applied via shipped SQL
   migrations (no network access needed at migrate time).
5. Sync declared queues: `python manage.py absurd_sync_queues`.
6. Run a worker: `python manage.py absurd_worker`.

## Validation

- Run `python manage.py check django_absurd` — system checks flag misconfiguration
  (wrong DB backend, queues declared but not synced, missing router, etc.). Treat check
  failures as blocking; fix configuration rather than silencing them.

## Notes

- Migrations are offline and sourced only from the pinned Absurd schema — never fetch at
  migrate time.
- This is alpha software; confirm behavior against the installed version's README rather
  than assuming API stability.
