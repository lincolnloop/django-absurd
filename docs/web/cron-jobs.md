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
                    "kwargs": {"source": "beat"},         # kwargs passed to the task; optional
                },
            },
        },
    },
}
```

| Key               | Required | Description                                                                                                                                                                                                                                                                        |
| ----------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `task`            | yes      | Dotted import path to a [`@task`](tasks.md#define-a-task) function.                                                                                                                                                                                                                |
| `cron`            | yes      | Cron expression. **Beat**: [croniter](https://pypi.org/project/croniter/) — 5-field `min hour dom mon dow`, or 6-field with a leading **seconds** column (`"*/30 * * * * *"`). **pg_cron**: a 5-field cron or `"<n> seconds"` (see [Schedule constraints](#schedule-constraints)). |
| `queue`           | no       | Queue to enqueue on; defaults to the backend's default. Must be a [declared queue](configuration.md#declaring-queues).                                                                                                                                                             |
| `args` / `kwargs` | no       | Positional / keyword arguments passed to the task each firing. `args` must be a JSON array, `kwargs` a JSON object.                                                                                                                                                                |

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

Installing the `django_absurd.pg_cron` app makes Postgres fire the schedule directly —
no beat process to run. django-absurd materialises each declared schedule into a
[pg_cron](https://github.com/citusdata/pg_cron) job; your existing
[workers](how-it-works.md#workers) pick up and run the tasks as usual.

This section covers running django-absurd's pg_cron backend. Installing and enabling the
pg_cron extension on your database is pg_cron's own concern — see
[References](#references) at the end for that.

### Get running

**1. Add the opt-in app to `INSTALLED_APPS`, after `"django_absurd"`:**

```python title="settings.py"
INSTALLED_APPS = [
    # ...
    "django_absurd",
    "django_absurd.pg_cron",   # must come after "django_absurd" — scheduling becomes
                                # pg_cron the moment this app is installed
]
```

This app owns the projection table + wrapper-function migrations and reconciles your
`SCHEDULE` on `post_migrate`. Its first migration runs
`CREATE EXTENSION IF NOT EXISTS pg_cron`: **if the extension isn't installed yet, we
create it**; if it's already there (managed Postgres, or a superuser installed it) that
step is a no-op and needs no special rights.

**2. Declare your `SCHEDULE`:**

```python title="settings.py"
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "SCHEDULE": {
                "nightly-report": {
                    "task": "myapp.tasks.send_report",
                    "cron": "0 2 * * *",
                },
                "heartbeat": {
                    "task": "myapp.tasks.ping",
                    "cron": "*/5 * * * *",
                    "queue": "monitoring",           # optional; must be a declared queue
                    "kwargs": {"source": "pg_cron"}, # kwargs passed to the task; optional
                },
            },
        },
    },
}
```

The `SCHEDULE` schema is identical to the beat scheduler — `task`, `cron`, optional
`queue`, `args`, `kwargs`. See the [beat field table](#declare-a-schedule).

**3. Migrate:**

```bash
python manage.py migrate
```

That's it. `migrate` applies the app's migrations and fires a `post_migrate` handler
that reconciles your `SCHEDULE` into pg_cron jobs. A settings-only `SCHEDULE` change (no
new migration file) is picked up on the next `migrate`, so "migrate on deploy" is all
you need.

Run `manage.py check` to catch misconfiguration early: `absurd.W003` if the app is
ordered before `"django_absurd"`.

Prefer to see it end-to-end first? The runnable
[`examples/pg_cron/`](https://github.com/lincolnloop/django-absurd/tree/main/examples/pg_cron)
demo (`docker compose up`) wires all of the above together (a companion
[`examples/beat/`](https://github.com/lincolnloop/django-absurd/tree/main/examples/beat)
demos the beat scheduler).

### Schedule constraints

**Cron grammar is pg_cron's own.** Once `django_absurd.pg_cron` is installed, an
expression is either a 5-field cron **or** the interval form `<n> seconds` (1-59) — so
sub-minute cadence works via `"30 seconds"`. This differs from beat's 6-field
leading-seconds croniter syntax, which `pg_cron` does not accept. `pg_cron` (the
database) validates the grammar — at sync for settings schedules, at save time for admin
ones — so `manage.py check` does **not** grammar-check pg_cron entries.

**Naming.** `manage.py check` also reports `absurd.E007` for:

- schedule name containing characters outside `[A-Za-z0-9_-]`
- composed job name (`_dj:s:<name>`) exceeding 63 bytes (Postgres silently truncates
  longer names)

**Beat and pg_cron are mutually exclusive.** Running `absurd_beat` (or
`absurd_worker --beat`) while `django_absurd.pg_cron` is installed raises a
`CommandError` — install the app, or run beat, not both.

### Reconcile explicitly

`migrate` reconciles automatically (above). To reconcile without a migrate — e.g. a
pipeline that skips `migrate` when no migration files changed:

```bash
python manage.py absurd_sync_crons
```

The command is loud: it reports synced/pruned counts, and fails with a non-zero exit on
error — a malformed `SCHEDULE` entry (missing `task`/`cron`) raises `CommandError`,
while a missing extension or insufficient privilege surfaces as the underlying database
error.

**Uninstalling pg_cron.** If you remove `"django_absurd.pg_cron"` from `INSTALLED_APPS`,
its `post_migrate` reconcile no longer runs, so nothing tears down existing jobs
automatically. Run `manage.py absurd_sync_crons --teardown --noinput` **before**
removing the app — not after — so it can still see and remove them.

### Authoring schedules in the admin

`ScheduledTask` rows appear in Django admin. Rows declared in settings
(`ScheduledTask.Source.SETTINGS`) are **read-only** — `SCHEDULE` is their source of
truth. Admins can additionally author `ScheduledTask.Source.ADMIN` schedules via a
**two-step flow**:

**Step 1 — Add form.** Fill only three fields: **Name**, **Task** (dotted import path to
a [`@task`](tasks.md#define-a-task)), and **Cron** expression. On save, the remaining
[spawn options](tasks.md#retries-spawn-options) — queue, `max_attempts`, retry strategy,
cancellation policy, `headers`, `idempotency_key` — are resolved from the task's `@task`
/ `@absurd_default_params` decorators and stored automatically. **Queue is required** —
blank is rejected; it always resolves to a concrete declared queue. The row is created
**disabled** (not yet firing). Resolution is frozen at create: later decorator edits do
not change existing rows.

**Step 2 — Change form.** Review the resolved values, fill `args` / `kwargs` if the task
needs them, and check **Enabled** to go live. Once enabled, saving or deleting the row
**immediately** (un)schedules its `pg_cron` job.

`name` is fixed once created (it forms the job's identity); the cron expression is
validated by `pg_cron` itself on save, so `"30 seconds"` is accepted and an invalid
expression comes back with `pg_cron`'s own message. **`max_attempts`** defaults to `5`
(Absurd's default retry ceiling) and must be `≥ 1`; clearing it stores `NULL`, which
Absurd treats as **retry forever** — a deliberate opt-in, so a mistyped schedule can't
loop unbounded by accident. The row is the source of truth: any write that persists it
(admin, ORM, or `loaddata`) keeps `pg_cron` in step (`cron.schedule` is an idempotent
upsert). A write forced onto a **different** database (`loaddata --database=…`,
`.using(…)`) raises `NotImplementedError` — schedules live only on the absurd DB. (When
Absurd is on a **non-default** database, `loaddata` bypasses the router and targets
`default`, so pass `--database=<alias>` to load schedules onto the absurd DB.) Writes
that bypass `.save()` — a **data migration** (the historical model isn't the signal's
sender), `bulk_create`, `QuerySet.update`, raw SQL — don't emit directly, but `migrate`
(and `absurd_sync_crons`) reconciles admin rows, so their jobs materialize then. A
settings schedule and an admin schedule **may** share the same name — they are distinct,
source-namespaced jobs (`_dj:s:…` vs `_dj:a:…`, the source abbreviated to keep the job
name short).

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

### Before you go to production

!!! warning "The kill switch is your `SCHEDULE`, not `cron.alter_job`"

    Every reconcile re-arms all settings-owned jobs (`active := true`). Operator edits
    to `cron.job` are not persistent. To stop a job permanently, remove its entry from
    `SCHEDULE` — the declaration is the source of truth.

!!! warning "Uninstalling is not self-cleaning"

    Removing django-absurd or switching back to the beat scheduler without running
    `migrate` (whose `post_migrate` tears down **settings** pg_cron jobs) leaves orphan
    jobs firing — and migrate never touches admin-authored jobs. Before uninstalling or
    switching, run:

    ```bash
    python manage.py absurd_sync_crons --teardown
    ```

    `--teardown` unschedules **all** owned jobs for the backend, including
    admin-authored ones, and deletes their rows (settings **and** admin). The admin rows
    are deleted deliberately — otherwise the next `migrate` would re-emit a job for each
    surviving admin row and resurrect what teardown just killed. Because it destroys
    admin-authored schedules it prompts for confirmation — pass `--no-input` in
    automation.

    Also consider setting up a
    [`cron.job_run_details`](https://github.com/citusdata/pg_cron#viewing-job-run-details)
    purge job — it is the only surface where fire-time failures appear, and it accumulates
    rows indefinitely without pruning.

## References

Setting up the pg_cron extension itself is out of scope for django-absurd — it's the
same for any pg_cron user. Start from pg_cron's own docs:

- **[pg_cron](https://github.com/citusdata/pg_cron)** — the extension.
  [Installing](https://github.com/citusdata/pg_cron#installing-pg_cron) ·
  [Configuring](https://github.com/citusdata/pg_cron#configuring-pg_cron) ·
  [Viewing job run details](https://github.com/citusdata/pg_cron#viewing-job-run-details)

Operator prerequisites django-absurd assumes are already in place before you `migrate`:

- **pg_cron ≥ 1.4** — django-absurd calls `cron.alter_job` (added in 1.4) on every
  reconcile.
- **`shared_preload_libraries = pg_cron`** — set in `postgresql.conf`; requires a server
  restart. A migration cannot set this.
- **`cron.database_name = <your_db>`** — the database Absurd runs on (pg_cron only lets
  the extension be created in that one database).

Managed Postgres (Amazon RDS, Google Cloud SQL, Azure Database, …) exposes these as
parameter-group / flag options and typically pre-installs the extension — in which case
our `CREATE EXTENSION IF NOT EXISTS` step is a clean no-op.

!!! note

    Because the app's first migration creates the extension, reversing it runs
    `DROP EXTENSION IF EXISTS pg_cron` — stock Django `CreateExtension` behavior, same as
    any extension migration.
