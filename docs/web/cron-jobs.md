---
icon: lucide/timer
---

# Cron Jobs

Run [tasks](tasks.md) on a recurring cadence. **Pick one of two schedulers** —
application-side [beat](#application-side-beat), or Postgres-side
[pg_cron](#postgres-side-pg_cron). Both read the same `SCHEDULE` map, so switching is a
deploy-time decision, not a rewrite. You cannot run both: once the pg_cron app is
installed, `absurd_beat` and `absurd_worker --beat` raise `CommandError`.

→ [Absurd's cron patterns](https://earendil-works.github.io/absurd/patterns/cron/).

## Declare a schedule

```python title="settings.py"
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "SCHEDULE": {
                "nightly-report": {
                    "task": "myapp.tasks.send_report",  # dotted path to a @task
                    "cron": "0 2 * * *",  # 2am daily
                },
                "heartbeat": {
                    "task": "myapp.tasks.ping",
                    "cron": "*/5 * * * *",
                    "queue": "monitoring",  # optional; must be declared
                    "kwargs": {"source": "beat"},  # optional
                },
            },
        },
    },
}
```

| Key               | Required | Description                                                                                            |
| ----------------- | -------- | ------------------------------------------------------------------------------------------------------ |
| `task`            | yes      | Dotted import path to a [`@task`](tasks.md#enqueue) function.                                          |
| `cron`            | yes      | Cron expression — grammar differs per scheduler, see below.                                            |
| `queue`           | no       | Queue to enqueue on; defaults to the backend's. Must be [declared](configuration.md#declaring-queues). |
| `args` / `kwargs` | no       | Passed to the task each firing. `args` a JSON array, `kwargs` a JSON object.                           |

Entries are validated by `manage.py check` (`absurd.E007`), names included — a schedule
name may only contain `[A-Za-z0-9_-]`.

## Application-side (beat)

```bash
python manage.py absurd_beat
```

Beat evaluates your cron expressions and enqueues each task when its slot comes due; a
[worker](how-it-works.md#workers) then runs it like any other task. For simple deploys,
co-locate the two in one process with `python manage.py absurd_worker --beat`.

- **Run exactly one beat.** Concurrent beats would each fire every slot; there is no
  leader election.
- **Fire-forward only.** Beat never backfills. Down across a slot means that slot is
  skipped; on start it computes the next slot from _now_.
- Grammar is [croniter](https://pypi.org/project/croniter/): 5-field
  `min hour dom mon dow`, or 6-field with a leading **seconds** column
  (`"*/30 * * * * *"`).
- Expressions are interpreted in Django's
  [`TIME_ZONE`](https://docs.djangoproject.com/en/6.0/ref/settings/#time-zone), so
  `0 2 * * *` means 2am **local**.

Runnable demo:
[`examples/beat/`](https://github.com/lincolnloop/django-absurd/tree/main/examples/beat)
(`docker compose up`).

## Postgres-side (pg_cron)

```python title="settings.py"
INSTALLED_APPS = [
    # ...
    "django_absurd",
    "django_absurd.pg_cron",  # must come AFTER "django_absurd"
]
```

```bash
python manage.py migrate
```

That's it — no beat process. Installing the app makes Postgres fire the schedule
directly; `migrate` reconciles your `SCHEDULE` into
[pg_cron](https://github.com/citusdata/pg_cron) jobs and your existing
[workers](how-it-works.md#workers) run the tasks as usual. A settings-only `SCHEDULE`
change needs no new migration file, so "migrate on deploy" is all you need.

Installing the extension itself is one-time operator work — see
[Operator setup](#operator-setup).

- **Grammar is pg_cron's own**: a 5-field cron, or the interval form `<n> seconds`
  (1–59), so sub-minute cadence works via `"30 seconds"`. Beat's 6-field leading-seconds
  syntax is rejected. pg_cron validates it — at sync for settings schedules, at save
  time for admin ones — so `manage.py check` does not grammar-check these.
- **Timezone is the `cron.timezone` GUC, which defaults to GMT** — not Django's
  `TIME_ZONE`. If your `TIME_ZONE` is non-UTC, set `cron.timezone = 'America/New_York'`
  in `postgresql.conf` to match, or `0 2 * * *` means two different things under the two
  schedulers.
- `manage.py check` reports `absurd.W003` if the app is ordered before
  `"django_absurd"`.

Runnable demo:
[`examples/pg_cron/`](https://github.com/lincolnloop/django-absurd/tree/main/examples/pg_cron)
(`docker compose up`).

??? note "How jobs reach your database"

    pg_cron is a **cluster-wide** extension, not a per-app-database one.
    [`cron.database_name`](https://github.com/citusdata/pg_cron#configuring-pg_cron) —
    one value per Postgres cluster — names the single database allowed to hold
    `CREATE EXTENSION pg_cron`; every other database in the cluster, including your
    Absurd database, has no `cron` schema at all.

    django-absurd never installs the extension on the Absurd database and never touches
    `cron.*` there. It auto-discovers the central database
    (`current_setting('cron.database_name')`, nothing to configure) and schedules each
    job **cross-database** with
    [`cron.schedule_in_database`](https://github.com/citusdata/pg_cron#cross-database-scheduling),
    targeting your Absurd database by name. If your cluster already runs pg_cron with
    `cron.database_name` set to the Absurd database itself, this degenerates to
    scheduling into "itself" — zero reconfiguration needed.

### Reconcile without migrating

```bash
python manage.py absurd_sync_crons
```

For a pipeline that skips `migrate` when no migration files changed. It reports
synced/pruned counts and exits non-zero on error: a malformed `SCHEDULE` entry raises
`CommandError`, a missing extension or insufficient privilege surfaces as the underlying
database error.

- **Every reconcile must connect as the same database role.** pg_cron ties each job to
  the role that scheduled it and runs it as that role, so mixing roles across `migrate`,
  `absurd_sync_crons`, and future deploys creates duplicate jobs (the upsert key is
  `(jobname, username)`) and breaks pruning — each role sees only its own jobs.
- `absurd_sync_crons` is never gated by the sync keys below — it's a deliberate
  invocation, not a side effect of `migrate`.

### Author schedules in the admin

`ScheduledTask` rows appear in Django admin. Settings-declared rows are **read-only** —
`SCHEDULE` is their source of truth. Admins can author their own in two steps:

**1. Add form.** Fill three fields: **Name**, **Task** (dotted path to a
[`@task`](tasks.md#enqueue)), **Cron**. On save, the remaining
[spawn options](tasks.md#retries-spawn-options) — queue, `max_attempts`, retry strategy,
cancellation, `headers`, `idempotency_key` — resolve from the task's decorators and are
stored. Queue is required. The row is created **disabled**.

**2. Change form.** Review the resolved values, fill `args` / `kwargs`, and check
**Enabled** to go live. Saving or deleting an enabled row immediately (un)schedules its
job.

- `name` is fixed once created — it forms the job's identity. Resolution is frozen at
  create, so later decorator edits don't change existing rows.
- `max_attempts` defaults to `5` and must be `≥ 1`. Clearing it stores `NULL`, which
  means **retry forever** — a deliberate opt-in so a mistyped schedule can't loop
  unbounded by accident.
- Any write that persists the row — admin, ORM, `loaddata` — keeps pg_cron in step.
  Writes that bypass `.save()` (data migrations, `bulk_create`, `QuerySet.update`, raw
  SQL) don't, but `migrate` and `absurd_sync_crons` reconcile admin rows, so those jobs
  materialize then.
- A write forced onto a **different** database raises `NotImplementedError` — schedules
  live only on the Absurd database. When Absurd is on a non-default database, `loaddata`
  bypasses the router, so pass `--database=<alias>`.
- A settings schedule and an admin schedule **may** share a name; they are distinct
  jobs.

### Test databases

Every `cron.*` write is **inert by default** on a test database or during an active test
run — detected automatically, no settings changes needed. A plain test database carries
no pg_cron extension, and pg_cron's launcher runs independently of pytest and Django, so
a schedule left behind would fire for real against test data for the rest of the
session.

| Option                      | Default | Effect                                                                                                                                                                                               |
| --------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PG_CRON_ON_TEST_DB`        | `False` | The opt-in. Without it every `cron.*` write no-ops and `absurd_sync_crons` refuses to run (`CommandError`) rather than silently doing nothing.                                                       |
| `SYNC_SCHEDULES_ON_MIGRATE` | `True`  | Governs `migrate`'s automatic reconcile against a real database.                                                                                                                                     |
| `SYNC_SCHEDULES_ON_TEST_DB` | `False` | Governs `migrate`'s automatic reconcile once Django has swapped in a test database. Setting it without `PG_CRON_ON_TEST_DB` is `absurd.E011` — the toggle is meaningless while the seam stays inert. |

→ [Testing — getting a `SCHEDULE` into pg_cron](testing.md#schedule-in-a-test).

### Before you go to production

!!! warning "The kill switch is your `SCHEDULE`, not `cron.alter_job`"

    Every reconcile re-arms all settings-owned jobs (`active := true`), so operator edits
    to `cron.job` are not persistent. To stop a job permanently, remove its entry from
    `SCHEDULE` — the declaration is the source of truth.

!!! warning "Uninstalling is not self-cleaning"

    Removing `"django_absurd.pg_cron"` from `INSTALLED_APPS` stops its `post_migrate`
    reconcile, so nothing tears down existing jobs. Removing django-absurd or switching
    back to beat without a `migrate` leaves orphan jobs firing — and `migrate` never
    touches admin-authored jobs anyway. Run this **before** removing anything:

    ```bash
    python manage.py absurd_sync_crons --teardown   # --noinput in automation
    ```

    `--teardown` unschedules **all** owned jobs for the backend, admin-authored included,
    and deletes their rows. The admin rows go deliberately — otherwise the next `migrate`
    would re-emit a job for each survivor and resurrect what teardown just killed. It
    prompts for confirmation because of that.

Also consider a
[`cron.job_run_details`](https://github.com/citusdata/pg_cron#viewing-job-run-details)
purge job. It is the only surface where fire-time failures appear, and it accumulates
rows indefinitely without pruning.

## Operator setup

Installing pg_cron itself is out of scope for django-absurd — it's the same for any
pg_cron user. Start from [pg_cron's own docs](https://github.com/citusdata/pg_cron):
[installing](https://github.com/citusdata/pg_cron#installing-pg_cron) ·
[configuring](https://github.com/citusdata/pg_cron#configuring-pg_cron) ·
[viewing job run details](https://github.com/citusdata/pg_cron#viewing-job-run-details).

Prerequisites django-absurd assumes are already in place before you `migrate` — all on
the **central** database named by `cron.database_name`, not necessarily the Absurd
database:

- **pg_cron ≥ 1.4** — django-absurd calls `cron.schedule_in_database` (its full 6-arg
  form) and `cron.alter_job`, both added in 1.4, on every reconcile.
- **`shared_preload_libraries = pg_cron`** in `postgresql.conf`. Requires a server
  restart; a migration cannot set it.
- **`CREATE EXTENSION pg_cron`**, run once on the `cron.database_name` database.
- Grants for the scheduling role — whichever role `migrate` / `absurd_sync_crons`
  connects as — unless it owns the extension outright. The `alter_job` grant is
  required, not optional: `schedule_in_database`'s `active` argument only applies when
  it first creates a job, so disabling an existing one needs an explicit `alter_job`
  call.

  ```sql
  GRANT USAGE ON SCHEMA cron TO <scheduling_role>;
  GRANT EXECUTE ON FUNCTION
      cron.schedule_in_database(text, text, text, text, text, boolean)
      TO <scheduling_role>;
  GRANT EXECUTE ON FUNCTION
      cron.alter_job(bigint, text, text, text, text, boolean)
      TO <scheduling_role>;
  ```

### Docker

The stock `postgres` image ships no pg_cron, so build it in:

```dockerfile title="Dockerfile.pg_cron"
FROM postgres:18

# Alpine carries no pg_cron package — the Debian variant is required.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-18-cron; \
    rm -rf /var/lib/apt/lists/*

# Runs once, on a fresh volume. initdb scripts run against POSTGRES_DB, so
# \connect the central database explicitly — the extension may only exist in
# the one cron.database_name names.
RUN printf '%s\n' \
    '\connect postgres' \
    'CREATE EXTENSION IF NOT EXISTS pg_cron;' \
    > /docker-entrypoint-initdb.d/10-pg_cron.sql
```

The preload flag and `cron.database_name` are server settings, so they go on the
command, not in the image:

```yaml title="compose.yaml"
services:
  db:
    build:
      context: .
      dockerfile: Dockerfile.pg_cron
    command:
      - postgres
      - -c
      - shared_preload_libraries=pg_cron
      - -c
      - cron.database_name=postgres
    environment:
      - POSTGRES_PASSWORD=postgres
```

- **Pin the package version** for anything but scratch work —
  `postgresql-18-cron=1.6.7-3.pgdg13+1`, say. Unpinned, a rebuild can move you across a
  pg_cron release.
- **Match the major versions.** `postgresql-18-cron` goes with `postgres:18`.
- `cron.database_name=postgres` keeps the extension in the central database while your
  app database schedules cross-database into it. Point it at your app database instead
  for the traditional single-database setup — nothing else changes.
- The extension is created on a **fresh volume only**. Changing these settings against
  an existing volume needs the volume recreated, or `CREATE EXTENSION` run by hand.

django-absurd runs its own pg_cron suite against exactly this shape — see
[`Dockerfile.pg_cron`](https://github.com/lincolnloop/django-absurd/blob/main/Dockerfile.pg_cron)
and the `db_pg_cron` service in
[`compose.yaml`](https://github.com/lincolnloop/django-absurd/blob/main/compose.yaml)
for a working reference, though those are wired for the test suite rather than for an
app.

### Managed Postgres

Amazon RDS, Google Cloud SQL, Azure Database, … expose the above as parameter-group or
flag options, and typically let you pre-install the extension and grants once,
centrally.

`manage.py check --database default` — and `migrate`, which runs checks first — reports
`absurd.E012` if the central database is unreachable or missing the extension.
Deploy-time fail-fast, never during the test suite.
