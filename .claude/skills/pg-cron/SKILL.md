---
name: pg-cron
description:
  Reusable reference for how pg_cron works — its architecture (background worker +
  cron.database_name), the scheduling API (cron.schedule / schedule_in_database /
  unschedule / alter_job), the cron.job + cron.job_run_details metadata tables, schedule
  syntax, cross-database jobs, and how to inspect a live instance. Use for any pg_cron
  work. Facts verified live against pg_cron 1.6 (postgresql-18-cron); re-probe if
  unsure.
---

# pg_cron

A Postgres extension that runs SQL commands on a schedule via a background worker.
Upstream: https://github.com/citusdata/pg_cron (README is the primary API doc; the
`pg_cron--*.sql` files define the exact function signatures).

## Architecture

- A single **background worker** runs jobs. It is enabled by
  `shared_preload_libraries = pg_cron` (a restart-only GUC) and connects to the database
  named by `cron.database_name` (GUC, default `postgres`, restart to change).
- **`CREATE EXTENSION pg_cron` is required** — pg_cron does nothing without it — **but
  it is installable in only ONE database per cluster**: the one named by
  `cron.database_name`. Running it from any other DB raises
  `can only create extension in database <cron.database_name>`. Every deployment needs
  exactly one `CREATE EXTENSION pg_cron`, in that database, which then holds the `cron`
  schema (functions + metadata tables).
- Jobs can still _run their command_ in other databases — see cross-database jobs below.

## Scheduling API

Signatures (from `pg_cron--*.sql`; verified):

```sql
cron.schedule(job_name text, schedule text, command text) RETURNS bigint   -- jobid
cron.schedule(schedule text, command text) RETURNS bigint                  -- auto-named
cron.schedule_in_database(job_name text, schedule text, command text,
                          database text,
                          username text DEFAULT NULL,
                          active boolean DEFAULT true) RETURNS bigint
cron.unschedule(job_id bigint) RETURNS boolean
cron.unschedule(job_name text) RETURNS boolean
cron.alter_job(job_id bigint, schedule text DEFAULT NULL, command text DEFAULT NULL,
               database text DEFAULT NULL, username text DEFAULT NULL,
               active boolean DEFAULT NULL) RETURNS void   -- added in 1.4
```

- `cron.schedule` is an **idempotent upsert** on `(jobname, username)`: re-scheduling
  the same name replaces the job.
- All scheduling functions must be called **from `cron.database_name`** (that's where
  the functions exist).

## Metadata tables

`cron.job` (verified columns):

```
jobid    bigint  PK
schedule text    not null
command  text    not null
database text    not null default current_database()   -- DB the command RUNS in
username text     not null default current_user          -- role it runs as
active   boolean not null default true
jobname  text     -- UNIQUE (jobname, username)
nodename/nodeport -- for remote targets (usually localhost)
```

Row-level security policy: `USING (username = current_user)` — a role sees only its own
jobs; a superuser sees all.

`cron.job_run_details` — execution history:
`jobid, runid, job_pid, database, username, command, status`
(`succeeded`/`failed`/`running`/…), `return_message`, `start_time`, `end_time`. First
place to look when a job "isn't running."

## Schedule syntax

- Standard **5-field cron** (`min hour dom mon dow`), evaluated in the server timezone.
- **Sub-minute intervals**: `'N seconds'` (1–59), pg_cron ≥ 1.4. (No 6-field seconds
  form — that's a different tool's syntax.)

## Cross-database jobs (schedule_in_database)

- `cron.schedule_in_database(..., database => 'target')` schedules a job whose command
  runs in `target`. **The target database does NOT need the pg_cron extension** —
  VERIFIED: a job scheduled into an extension-less DB both schedules and **fires** (the
  worker connects to the target and runs the command;
  `cron.job_run_details.status='succeeded'`).
- A job is **bound to its target DB**: `database => 'a'` fires only into `a`. This makes
  the `database` column a clean isolation + teardown key
  (`... FROM cron.job WHERE database = 'a'`).
- Metadata (`cron.job`, run history) always lives centrally in `cron.database_name`
  regardless of target.
- **Version floor: pg_cron ≥ 1.4.** `cron.schedule_in_database` — with its full 6-arg
  signature INCLUDING `active` — and `cron.alter_job` both landed in 1.4 (verified in
  `pg_cron--1.3--1.4.sql`). That is already django-absurd's documented floor, unchanged.
  Prefer gating on function existence (`to_regproc('cron.schedule_in_database')`) over
  version parsing, since managed platforms vary.
- **Permissions:** a non-superuser needs `GRANT USAGE ON SCHEMA cron` **+**
  `GRANT EXECUTE ON FUNCTION cron.schedule_in_database(text,text,text,text,text,boolean)`
  in `cron.database_name` (the extension ships it un-granted on purpose: "admin should
  decide whether cron.schedule_in_database is safe by explicitly granting execute"). It
  may then schedule OWN-user jobs into any DB; passing another `username` requires
  superuser.

## Inspecting / probing a live instance

```bash
psql -c "SHOW cron.database_name"                          # which DB holds the extension
psql -d <cron.database_name> -c "\d cron.job"              # schema
psql -d <cron.database_name> -c "SELECT jobid,jobname,database,active,schedule FROM cron.job"
psql -d <cron.database_name> -c \
  "SELECT status,return_message,start_time FROM cron.job_run_details ORDER BY start_time DESC LIMIT 5"
```

Fast end-to-end probe (schedule a 5-second job into a throwaway DB and watch it fire):

```bash
createdb probe; psql -d probe -c "CREATE TABLE hits(t timestamptz)"
psql -d <cron.database_name> -c \
  "SELECT cron.schedule_in_database('probe','5 seconds','insert into hits values(now())','probe')"
sleep 10; psql -d probe -c "SELECT count(*) FROM hits"     # > 0
psql -d <cron.database_name> -c "SELECT cron.unschedule(jobid) FROM cron.job WHERE database='probe'"
dropdb probe
```

## Keeping this reference current

This file was verified against **pg_cron 1.6** (`postgresql-18-cron`, the version pinned
in `Dockerfile.pg_cron`). pg_cron evolves, so re-verify when the pin moves or when
unsure:

1. **Check for new releases:** https://github.com/citusdata/pg_cron/releases and `/tags`
   (or `gh api repos/citusdata/pg_cron/releases/latest --jq .tag_name`). Compare to the
   `postgresql-*-cron=<version>` pin in `Dockerfile.pg_cron`.
2. **Read the delta:** the release notes / `CHANGELOG.md`, and the versioned
   `pg_cron--<old>--<new>.sql` upgrade scripts in the repo (the authoritative source for
   new/changed function signatures) — e.g. `schedule_in_database`, `alter_job` were
   added in 1.4.
3. **Re-probe** the facts here against the running server (the probe commands above,
   plus `\df cron.*` for the current signatures). Update this file with what changed and
   bump the "verified against" version.

**Trigger:** treat a bump of the `postgresql-*-cron` pin in `Dockerfile.pg_cron` (e.g. a
Renovate PR) as a signal to re-verify this skill — a candidate check for the `sync-docs`
flow. If the API changed, the change likely also touches `django_absurd/pg_cron/`.

## In this repo (brief)

`django_absurd.pg_cron` never installs the extension on the app/absurd database and
never runs `CREATE EXTENSION` in a migration. It auto-discovers the central database
(`current_setting('cron.database_name')`) and schedules every job cross-database via
`cron.schedule_in_database(...)` (`pg_cron/catalog.py`), targeting the live app database
by name; job names are namespaced `_dj:<app db>:<source>:<name>` so multiple app
databases sharing one central catalog never collide. A same-database deployment (where
`cron.database_name` IS the app database) works unchanged — it's the degenerate case of
the same call.

Repo servers: `db_pg_cron` (compose) runs pg_cron 1.6 with `cron.database_name` fixed at
the central `postgres` database; `tests/pg_cron` runs against its own ordinary test
database on that same server, which holds no extension. `pytest --create-db` needs no
special handling — the launcher never touches the test database's session, only the
central one's.
