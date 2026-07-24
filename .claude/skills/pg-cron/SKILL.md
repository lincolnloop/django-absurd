---
name: pg-cron
description:
  Reference for how pg_cron actually behaves — the single-extension-per-cluster
  constraint, cron.database_name, cron.schedule vs cron.schedule_in_database, the
  cron.job schema, and the testing consequences (why installing django_absurd.pg_cron
  breaks pytest-xdist / test_ isolation, and how schedule_in_database dissolves it). Use
  when working on any django_absurd/pg_cron feature, test, migration, or the cron
  test-DB problem. Facts here are empirically verified against pg_cron 1.6
  (postgresql-18-cron), not just docs.
---

# pg_cron

Postgres job scheduler used by `django_absurd.pg_cron`. Its constraints drive most of
the hard design decisions in this repo. **Every fact below was verified live against the
running `db_pg_cron` container (pg_cron 1.6 / postgresql-18-cron) — re-probe if unsure
(commands at the bottom).**

## The one hard constraint: a single blessed database per cluster

- `cron.database_name` is a **server-level GUC** (postgresql.conf / `-c`, restart to
  change). It names the ONE database where pg_cron's metadata (`cron.job`,
  `cron.job_run_details`) and functions live.
- **`CREATE EXTENSION pg_cron` is only legal in that database.** From any other DB it
  raises `can only create extension in database <cron.database_name>`. Docs: "pg_cron
  may only be installed to one database in a cluster."
- The launcher is a single background worker bound to `cron.database_name`; it reads job
  rows from there.

## cron.job schema (verified)

```
database  text not null default current_database()   -- target DB the job RUNS in
username  text not null default current_user          -- role it runs as
jobname   text                                        -- UNIQUE (jobname, username)
```

Row-level security policy on `cron.job`: `USING (username = current_user)` — a role sees
only its own jobs; superuser sees all.

## Scheduling: same-DB vs cross-DB

- `cron.schedule(name, schedule, command)` → the job's `database` defaults to
  `current_database()`, i.e. `cron.database_name` (you can only call it from there,
  since the functions live there). The command runs IN `cron.database_name`.
- `cron.schedule_in_database(job_name, schedule, command, database, username DEFAULT NULL, active DEFAULT true)`
  → schedules a job whose command runs in **any existing `database`**.
- **VERIFIED, load-bearing:** the target database does **NOT** need the pg_cron
  extension. A job scheduled via `schedule_in_database` into an extension-less DB both
  schedules AND **fires** — the launcher connects to the target DB and runs the command
  there (`cron.job_run_details.status='succeeded'`). Only `cron.database_name` needs the
  extension.
- Sub-minute intervals (`'N seconds'`, 1–59) work in pg_cron ≥ 1.4 (we run 1.6) — useful
  for fast tests. 5-field cron otherwise.
- Jobs are bound to their target DB: a job with `database => 'main_db'` fires ONLY into
  `main_db`, never into `test_db`. This target-binding is a structural isolation
  guarantee.

## Why this breaks Django testing (the core problem)

Today django-absurd assumes `cron.database_name` **==** the app/absurd DB (extension +
metadata + the `django_absurd_run_scheduled` fire-wrapper all live in the app DB; the
`pg_cron/migrations/0001` runs `CREATE EXTENSION` on the app DB). Consequences for any
downstream project that installs `django_absurd.pg_cron`:

- **pytest-xdist impossible** — each worker gets `test_<db>_gwN`; only one DB per server
  can be blessed → other workers fail `CREATE EXTENSION`.
- **No clean `test_` isolation** — the test DB must equal `cron.database_name`, forcing
  tests onto the real dev DB (pollution; auto-cleanup can wipe real schedules) or a
  second server.

Two solution directions in play (see
`docs/specs/2026-07-23-pg-cron-test-inert-design.md` and the schedule_in_database
exploration):

1. **test-inert pg_cron** — keep the same-DB model, but skip `CREATE EXTENSION` and gate
   all `cron.*` calls in tests (detected via a name-snapshot + `setup_test_environment`
   conjunct).
2. **schedule_in_database architecture** — make `cron.database_name` a **central
   metadata DB ≠ the app DB**; the app/test DB never gets the extension; schedule via
   `schedule_in_database(database => <current app DB>)`; tear down test jobs with
   `SELECT ... FROM cron.job WHERE database = '<test_db>'`. Empirically viable (above);
   a bigger topology change.

## Test-DB operational gotcha (this repo)

`cron.database_name` on the `db_pg_cron` service points at the pg_cron suite's test DB,
so the launcher holds a session on it and blocks `pytest --create-db`'s DROP. Evict
first (see CLAUDE.md): `ALTER DATABASE <db> WITH ALLOW_CONNECTIONS false` +
`pg_terminate_backend(...)`, then `--create-db`.

## Verify it yourself (probe commands)

```bash
C=django-absurd-db_pg_cron-1   # a pg_cron-enabled server; cron.database_name set
docker exec $C psql -U postgres -d "$(docker exec $C psql -U postgres -tAc 'SHOW cron.database_name')" -c "\d cron.job"
# schedule into an extension-less DB and watch it fire:
docker exec $C createdb -U postgres probe_target
docker exec $C psql -U postgres -d probe_target -c "CREATE TABLE hits(t timestamptz)"
docker exec $C psql -U postgres -d "$(docker exec $C psql -U postgres -tAc 'SHOW cron.database_name')" \
  -c "SELECT cron.schedule_in_database('probe','5 seconds','insert into hits values(now())','probe_target')"
# wait ~10s, then: SELECT count(*) FROM hits;  → > 0, cron.job_run_details status='succeeded'
# cleanup: unschedule WHERE database='probe_target'; dropdb probe_target
```
