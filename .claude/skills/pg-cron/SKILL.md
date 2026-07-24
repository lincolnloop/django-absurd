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

## Inspecting / probing a live instance

```bash
psql -c "SHOW cron.database_name"                          # which DB is blessed
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

## In this repo (brief)

`django_absurd.pg_cron` schedules via `cron.schedule` (`pg_cron/models.py`,
`pg_cron/reconcile.py`) and today assumes `cron.database_name` == the app/absurd DB (its
`0001` migration runs `CREATE EXTENSION` there). The single-DB-per-cluster constraint is
what makes installing the app break pytest-xdist / `test_` isolation — see the design
work in `docs/specs/` and the `schedule_in_database` exploration for the fix. Repo
servers: `db_pg_cron` (compose) runs pg_cron 1.6 with `cron.database_name` set to the
suite's test DB (which is why `pytest --create-db` needs the evict dance in CLAUDE.md).
