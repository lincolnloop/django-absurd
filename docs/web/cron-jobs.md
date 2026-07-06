---
icon: lucide/timer
---

# Cron Jobs

Run [tasks](tasks.md) on a recurring **cron** cadence. django-absurd offers two ways to
drive schedules, both following
[Absurd's cron patterns](https://earendil-works.github.io/absurd/patterns/cron/):
**application-side** ([beat process](#application-side-beat)) and
**[database-side: pg_cron](#database-side-pg_cron)** (Postgres fires jobs directly).

## Application-side (beat)

A small **beat** process evaluates your cron expressions and enqueues each task when its
slot comes due; a [worker](how-it-works.md#workers) then runs it like any other task.

### Declare a schedule

Add a `SCHEDULE` map to the backend's `OPTIONS` (see [Configuration](configuration.md)).
Each entry is a name → spec:

```python title="settings.py"
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "SCHEDULE": {
                "nightly-report": {
                    "task": "myapp.tasks.send_report",   # dotted path to a @task
                    "cron": "0 2 * * *",                  # 2am daily, in your TIME_ZONE
                },
                "heartbeat": {
                    "task": "myapp.tasks.ping",
                    "cron": "*/5 * * * *",                # every 5 minutes
                    "queue": "monitoring",                # optional; must be a declared queue
                    "kwargs": {"source": "beat"},         # optional
                },
            },
        },
    },
}
```

| Key               | Required | Description                                                                                                                                                                                   |
| ----------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `task`            | yes      | Dotted import path to a [`@task`](tasks.md#define-a-task) function.                                                                                                                           |
| `cron`            | yes      | Cron expression ([croniter](https://pypi.org/project/croniter/)): 5-field `min hour dom mon dow`, or 6-field with a leading **seconds** column for sub-minute schedules (`"*/30 * * * * *"`). |
| `queue`           | no       | Queue to enqueue on; defaults to the backend's default. Must be a [declared queue](configuration.md#declaring-queues).                                                                        |
| `args` / `kwargs` | no       | Positional / keyword arguments passed to the task each firing.                                                                                                                                |

Cron is interpreted in Django's
[`TIME_ZONE`](https://docs.djangoproject.com/en/6.0/ref/settings/#time-zone), so
`0 2 * * *` means 2am **local** time. Entries are validated by `manage.py check`
(`absurd.E007`).

### Run the beat

Run the scheduler as its own process:

```bash
python manage.py absurd_beat
```

…or co-located with a [worker](how-it-works.md#workers) (one process, simple deploys):

```bash
python manage.py absurd_worker --beat
```

**Run exactly one beat.** Concurrent beats would each fire every slot; there's no leader
election.

**Fire-forward only.** Beat never backfills. If it's down across a slot, that slot is
skipped; on start it computes the next slot from _now_.

## Database-side: pg_cron

With `SCHEDULER="pg_cron"` Postgres fires the schedule directly — no beat process to
run. A reconcile step materialises each declared schedule into a
[`pg_cron`](https://github.com/citusdata/pg_cron) job whose command calls a wrapper
function; that wrapper reads the task configuration from a projection table and calls
`absurd.spawn_task`. Existing [workers](how-it-works.md#workers) then pick up and run
the tasks as usual.

### Prerequisites

`pg_cron` is an operator-installed extension — django-absurd does **not** ship a
`CREATE EXTENSION` migration. Before enabling the pg_cron backend you need:

1. **pg_cron ≥ 1.4** (the `cron.alter_job` function, used every reconcile, was added in
   1.4). Managed Postgres offerings (Amazon RDS, Google Cloud SQL, Azure Database, etc.)
   support pg_cron as a parameter-group / flag option.
2. `shared_preload_libraries = pg_cron` in `postgresql.conf` (requires a server
   restart).
3. `cron.database_name = <your_db>` pointing at the database Absurd runs on.
4. `CREATE EXTENSION pg_cron;` executed by a **superuser** in that database.

The standard way to deliver step 4 in a Django project is a one-off migration in your
own app:

```python title="yourapp/migrations/000x_create_pg_cron.py"
from django.contrib.postgres.operations import CreateExtension
from django.db import migrations

class Migration(migrations.Migration):
    operations = [
        CreateExtension("pg_cron"),
    ]
```

[`CreateExtension`](https://docs.djangoproject.com/en/stable/ref/contrib/postgres/operations/#django.contrib.postgres.operations.CreateExtension)
is Django's first-class operation for this (it issues `CREATE EXTENSION IF NOT EXISTS`
and a matching reverse) — prefer it over raw `RunSQL`.

The migration role must be a superuser (or granted `CREATE ON DATABASE`). This is the
same pattern that `CREATE EXTENSION "uuid-ossp"` uses — it is deliberately not shipped
inside django-absurd itself because the superuser requirement and
`shared_preload_libraries` restart make it an operator-side concern, not a library
concern.

### Enable the pg_cron backend

Add `"django_absurd.pg_cron"` to `INSTALLED_APPS` **after** `"django_absurd"` — the
opt-in app owns the projection table and wrapper function migrations (applied by
`migrate`) and reconciles the `SCHEDULE` on `post_migrate`. Running `manage.py check`
reports `absurd.E008` if `SCHEDULER="pg_cron"` is set but the app is absent, and
`absurd.W003` (Warning) if the app is present but ordered before `"django_absurd"`.

```python title="settings.py"
INSTALLED_APPS = [
    # ...
    "django_absurd",
    "django_absurd.pg_cron",   # must come after "django_absurd"
]
```

Then configure the scheduler:

```python title="settings.py"
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "SCHEDULER": "pg_cron",   # default is "beat"
            "SCHEDULE": {
                "nightly-report": {
                    "task": "myapp.tasks.send_report",
                    "cron": "0 2 * * *",  # 5-field only — see note on sub-minute below
                },
                "heartbeat": {
                    "task": "myapp.tasks.ping",
                    "cron": "*/5 * * * *",
                    "queue": "monitoring",          # optional; must be a declared queue
                    "kwargs": {"source": "pg_cron"}, # optional
                },
            },
        },
    },
}
```

The `SCHEDULE` schema is identical to the beat scheduler — `task`, `cron`, optional
`queue`, `args`, `kwargs`. See the [beat section above](#declare-a-schedule) for the
full field table.

**Sub-minute schedules are beat-only.** `pg_cron` fires at minute granularity. A 6-field
(leading-seconds) cron expression under `SCHEDULER="pg_cron"` is rejected by
`manage.py check` (`absurd.E007`). Use the beat for sub-minute cadences.

**pg_cron naming constraints.** `manage.py check` also reports `absurd.E007` for:

- schedule name containing characters outside `[A-Za-z0-9_-]`
- backend alias containing characters outside `[A-Za-z0-9_-]` (pg_cron job names share
  the same charset restriction)
- composed job name (`absurd:settings:<alias>:<name>`) exceeding 63 bytes (Postgres
  silently truncates longer names)

**Beat and pg_cron are mutually exclusive per backend.** Setting `SCHEDULER="pg_cron"`
and running `absurd_beat` (or `absurd_worker --beat`) against the same backend raises a
`CommandError` — use one or the other.

### Reconcile schedules

Run `migrate` on each deploy (the recommended path — nothing extra to do):

```bash
python manage.py migrate
```

`migrate` fires a `post_migrate` signal handler that reconciles the declared `SCHEDULE`
into `pg_cron` jobs automatically. A settings-only `SCHEDULE` change (no new migration
file) is picked up on the next `migrate` run, so "migrate on deploy" is sufficient.

To reconcile explicitly (e.g. in a pipeline that skips `migrate` when no migration files
changed):

```bash
python manage.py absurd_sync_crons
```

The command is loud: it reports upserted/pruned counts and raises `CommandError` on any
failure (missing extension, bad privilege, etc.).

**Projection table and wrapper function.** The `ScheduledTask` projection table
(`django_absurd_scheduledtask`) and the `public.django_absurd_run_scheduled` wrapper
function both live in the `public` schema (where Django app tables live). The table
stores explicit option columns — `args`, `kwargs`, `max_attempts`, `retry_strategy`,
`headers`, `cancellation`, `idempotency_key` — rather than opaque JSON blobs; the
wrapper reassembles `params`/`options` jsonb from those named columns server-side at
fire time. They are created and managed by the `django_absurd_pg_cron` app's own
migration, applied by `manage.py migrate`.

The `ScheduledTask` table is registered read-only in the admin. Settings is the source
of truth; use `SCHEDULE` in settings rather than editing rows directly (see
[Cron Jobs — kill switch warning](#two-things-to-know-before-going-to-production)).

### Timezone

`pg_cron` evaluates cron expressions in the timezone set by the
[`cron.timezone`](https://github.com/citusdata/pg_cron#configuration) GUC, which
defaults to **GMT**. This differs from the beat scheduler, which interprets expressions
in Django's
[`TIME_ZONE`](https://docs.djangoproject.com/en/6.0/ref/settings/#time-zone).

When Django's `TIME_ZONE` is `"UTC"` (the default), the two agree and no extra
configuration is needed. When `TIME_ZONE` is a non-UTC timezone (e.g.
`"America/New_York"`), set `cron.timezone` to match so that `0 2 * * *` means local 2am
under both schedulers:

```ini title="postgresql.conf"
cron.timezone = 'America/New_York'
```

### Single stable role

`pg_cron` ties each job to the role that scheduled it and runs the job as that role. All
reconcile calls — `migrate`, `absurd_sync_crons`, and future deploys — must use the
**same database role**. Using different roles causes duplicate jobs (pg_cron's upsert
key is `(jobname, username)`) and breaks pruning (each role sees only its own jobs).

### Two things to know before going to production

!!! warning "The kill switch is your `SCHEDULE`, not `cron.alter_job`"

    Every reconcile re-arms all settings-owned jobs (`active := true`). Operator edits
    to `cron.job` are not persistent. To stop a job permanently, remove its entry from
    `SCHEDULE` — the declaration is the source of truth.

!!! warning "Uninstalling is not self-cleaning"

    Removing django-absurd or switching back to the beat scheduler without running
    `migrate` (which calls `post_migrate` and tears down pg_cron jobs) leaves orphan jobs
    firing. Before uninstalling or switching, run:

    ```bash
    python manage.py absurd_sync_crons --teardown
    ```

    Also consider setting up a
    [`cron.job_run_details`](https://github.com/citusdata/pg_cron#viewing-job-run-details)
    purge job — it is the only surface where fire-time failures appear, and it accumulates
    rows indefinitely without pruning.
