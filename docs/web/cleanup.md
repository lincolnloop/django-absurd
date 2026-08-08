---
icon: lucide/trash-2
---

# Cleanup / retention

Task rows accumulate in Postgres unless you prune them. Cleanup deletes **terminal**
rows — completed, failed, or cancelled. Running and pending tasks are never touched.

## Run on demand

```bash
python manage.py absurd_cleanup            # every queue
python manage.py absurd_cleanup reports    # only the named queue(s)
```

Prints per-queue counts:

```
default: 12 tasks, 0 events deleted
```

- The same function is importable: `cleanup_queues()` for all queues, or
  `cleanup_queues(["reports", "emails"])` for specific ones, returning a list of
  per-queue count dicts.
- An unknown queue name raises a database error rather than being masked by a guard —
  cleanup is a maintenance operation, so the raw error surfaces.

## Schedule recurring cleanup

```python title="settings.py"
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "CLEANUP": {"schedule": "0 3 * * *"},  # 3am daily
        },
    },
}
```

Runs cleanup automatically on cadence, no user code required, under **either**
[scheduler](cron-jobs.md) — beat runs it in-process; pg_cron calls Absurd's own native
`absurd.cleanup_all_queues` from a job on django-absurd's lane.

- With `django_absurd.pg_cron` installed, django-absurd is authoritative over that job:
  it schedules it from `OPTIONS["CLEANUP"]` and removes it otherwise, including at
  migrate teardown or a scheduler flip, even when `CLEANUP` was never set.
- `manage.py check` reports `absurd.E010` for a malformed `CLEANUP`. The cron grammar is
  checked at `check` time for beat, and by the database at sync for pg_cron.

!!! warning "Drive cleanup one way only"

    `OPTIONS["CLEANUP"]` **or** `absurdctl cron` — never both. Absurd's own maintenance
    scheduler (`absurd.enable_cron`, which `absurdctl cron --enable <queue>` drives) is a
    separate mechanism creating **per-queue** jobs that django-absurd neither uses nor
    manages. It cannot see or remove them, so they survive every teardown and fire
    alongside its own.

## Retention knobs

```python
"OPTIONS": {"QUEUES": {
    "reports": {"cleanup_ttl": "7 days", "cleanup_limit": 1000},
}}
```

Per-queue policy, set where you [declare the queue](configuration.md#declaring-queues):

| Option          | What it controls                                                                                         |
| --------------- | -------------------------------------------------------------------------------------------------------- |
| `cleanup_ttl`   | Minimum age a terminal task must reach before it is deleted.                                             |
| `cleanup_limit` | Max terminal rows deleted **per queue** per run — applied separately to task and event rows (batch cap). |

→ [Absurd: cleanup](https://earendil-works.github.io/absurd/cleanup/) ·
[Absurd: storage](https://earendil-works.github.io/absurd/storage/).

## Reset — drop all queues

```bash
python manage.py absurd_flush            # prompts, then drops on 'yes'
python manage.py absurd_flush --noinput  # drops without prompting
```

Removes every queue — its per-queue tables and registry entry — along with all tasks,
runs, and events in them. It does **not** uninstall Absurd: the schema, migrations, and
functions stay, so you never re-`migrate`, you only re-provision the queues.

!!! warning "Destructive"

    This permanently deletes all task history across every queue. Re-provision your
    declared queues afterward with `migrate`, `absurd_sync_queues`, or by starting a
    worker.

    Existing scheduled jobs (pg_cron and beat) survive the flush and **error on each
    fire** until the queues exist again — re-provision promptly. The one exception is the
    cleanup job from `OPTIONS["CLEANUP"]`, which survives and runs harmlessly, finding no
    eligible rows.

    Per-queue Absurd maintenance jobs created via `absurdctl cron --enable <queue>`
    (`absurd_partitions_<md5>` / `absurd_cleanup_<md5>` / `absurd_detach_plan_<md5>`) are
    removed along with their queue. The `OPTIONS["CLEANUP"]` job is not per-queue and is
    unaffected.
