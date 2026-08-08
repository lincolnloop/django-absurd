---
icon: lucide/timer
---

# Cron Jobs

Run [tasks](tasks.md) on a recurring cadence. **Pick one scheduler** — application-side
[beat](#application-side-beat), or Postgres-side [pg_cron](#postgres-side-pg_cron). Both
read the same `SCHEDULE`. Installing the pg_cron app makes `absurd_beat` and
`absurd_worker --beat` raise `CommandError`.

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

Validated by `manage.py check` (`absurd.E007`); names are limited to `[A-Za-z0-9_-]`.

## Application-side (beat)

```bash
python manage.py absurd_beat          # or co-located: absurd_worker --beat
```

Beat enqueues each task when its slot comes due; a [worker](how-it-works.md#workers)
then runs it like any other.

- **Run exactly one.** No leader election — concurrent beats each fire every slot.
- **Never backfills.** A slot missed while down is skipped.
- Grammar is [croniter](https://pypi.org/project/croniter/): 5-field, or 6-field with a
  leading seconds column (`"*/30 * * * * *"`).
- Expressions use Django's
  [`TIME_ZONE`](https://docs.djangoproject.com/en/6.0/ref/settings/#time-zone).

Runnable demo:
[`examples/beat/`](https://github.com/lincolnloop/django-absurd/tree/main/examples/beat).

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

Postgres fires the schedule directly — no beat process. `migrate` reconciles `SCHEDULE`
into [pg_cron](https://github.com/citusdata/pg_cron) jobs and your existing
[workers](how-it-works.md#workers) run the tasks. A settings-only change needs no new
migration, so "migrate on deploy" covers it. The extension itself is one-time
[operator setup](#operator-setup).

- **Grammar is pg_cron's own**: 5-field cron, or `<n> seconds` (1–59) for sub-minute
  cadence. Beat's 6-field form is rejected. pg_cron validates it, so `manage.py check`
  does not.
- **Timezone is the `cron.timezone` GUC, default GMT** — not Django's `TIME_ZONE`. Set
  it to match if yours is non-UTC.
- `absurd.W003` if the app is ordered before `"django_absurd"`.

Runnable demo:
[`examples/pg_cron/`](https://github.com/lincolnloop/django-absurd/tree/main/examples/pg_cron).

??? note "How jobs reach your database"

    pg_cron is **cluster-wide**: only the database named by
    [`cron.database_name`](https://github.com/citusdata/pg_cron#configuring-pg_cron) may
    hold the extension, and yours probably isn't it. django-absurd finds that database
    itself and schedules each job
    [cross-database](https://github.com/citusdata/pg_cron#cross-database-scheduling) —
    nothing to configure either way.

### Reconcile without migrating

```bash
python manage.py absurd_sync_crons
```

For pipelines that skip `migrate` when no migration files changed. Reports synced/pruned
counts, non-zero exit on error.

- **Always connect as the same role.** pg_cron keys jobs on `(jobname, username)` and
  runs each as its scheduling role, so mixing roles duplicates jobs and breaks pruning.

### Author schedules in the admin

Settings-declared `ScheduledTask` rows are read-only. Admins author their own in two
steps: **add** (name, task, cron — the rest resolves from the task's
[decorators](tasks.md#retries-spawn-options), row created disabled), then **change**
(fill `args` / `kwargs`, tick **Enabled**). Saving or deleting an enabled row
(un)schedules its job immediately.

- `name` and the resolved options are frozen at create.
- `max_attempts` defaults to `5`; clearing it means retry forever.
- Writes bypassing `.save()` (data migrations, `bulk_create`, `update`, raw SQL) emit on
  the next reconcile, not immediately.
- `loaddata` bypasses the router — pass `--database=<alias>`. Another database raises
  `NotImplementedError`.

### Test databases

Every `cron.*` write is inert on a test database, detected automatically — otherwise a
leftover schedule would fire for real against test data.

| Option                      | Default | Effect                                                                                   |
| --------------------------- | ------- | ---------------------------------------------------------------------------------------- |
| `PG_CRON_ON_TEST_DB`        | `False` | The opt-in. Without it writes no-op and `absurd_sync_crons` refuses to run.              |
| `SYNC_SCHEDULES_ON_MIGRATE` | `True`  | `migrate`'s automatic reconcile against a real database.                                 |
| `SYNC_SCHEDULES_ON_TEST_DB` | `False` | Same, against a test database. Setting it without `PG_CRON_ON_TEST_DB` is `absurd.E011`. |

→ [Testing — getting a `SCHEDULE` into pg_cron](testing.md#schedule-in-a-test).

### Before you go to production

!!! warning "Uninstalling is not self-cleaning"

    Removing the app stops the reconcile but leaves jobs firing, and `migrate` never
    touches admin-authored ones. Run this **first**:

    ```bash
    python manage.py absurd_sync_crons --teardown   # --noinput in automation
    ```

    It unschedules every owned job and deletes its row, admin-authored included —
    otherwise the next `migrate` resurrects them. Hence the prompt.

- **The kill switch is `SCHEDULE`, not `cron.alter_job`.** Every reconcile re-arms
  settings-owned jobs, so edits to `cron.job` don't persist.
- Consider a
  [`cron.job_run_details`](https://github.com/citusdata/pg_cron#viewing-job-run-details)
  purge job — the only place fire-time failures show up, and it grows unbounded.

## Operator setup

One-time, on the **central** database named by `cron.database_name` — not necessarily
the Absurd one. See [pg_cron's own docs](https://github.com/citusdata/pg_cron).

- **pg_cron ≥ 1.4** — reconciles call `cron.schedule_in_database` and `cron.alter_job`.
- **`shared_preload_libraries = pg_cron`** — needs a server restart.
- **`CREATE EXTENSION pg_cron`**.
- Grants, unless the scheduling role owns the extension. `alter_job` is not optional —
  `schedule_in_database` only applies `active` on first create:

  ```sql
  GRANT USAGE ON SCHEMA cron TO <scheduling_role>;
  GRANT EXECUTE ON FUNCTION
      cron.schedule_in_database(text, text, text, text, text, boolean)
      TO <scheduling_role>;
  GRANT EXECUTE ON FUNCTION
      cron.alter_job(bigint, text, text, text, text, boolean)
      TO <scheduling_role>;
  ```

Managed Postgres (RDS, Cloud SQL, Azure) exposes these as parameter-group flags.
`manage.py check` reports `absurd.E012` if the central database is unreachable or
missing the extension.

### Docker

The stock `postgres` image ships no pg_cron. Copy django-absurd's own — its pg_cron
suite runs against exactly these:

- [`Dockerfile.pg_cron`](https://github.com/lincolnloop/django-absurd/blob/main/Dockerfile.pg_cron)
  — Debian base (Alpine has no pg_cron package), the PGDG package, and an initdb script
  that creates the extension on the central database.
- The `db_pg_cron` service in
  [`compose.yaml`](https://github.com/lincolnloop/django-absurd/blob/main/compose.yaml)
  — the `shared_preload_libraries` and `cron.database_name` server flags, which can't
  live in the image.
