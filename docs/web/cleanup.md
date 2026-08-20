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

- Importable too: `cleanup_queues()`, or `cleanup_queues(["reports", "emails"])`,
  returning per-queue count dicts.
- An unknown queue name raises the raw database error — cleanup is a maintenance
  operation, so nothing masks it.

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

Runs on cadence under **either** [scheduler](cron-jobs.md), no user code — beat runs it
in-process, pg_cron calls Absurd's own `absurd.cleanup_all_queues`.

- With `django_absurd.pg_cron` installed, django-absurd owns that job outright: it
  schedules it from `OPTIONS["CLEANUP"]` and removes it otherwise.
- `absurd.E010` for a malformed `CLEANUP`, including a cron expression the configured
  scheduler cannot run — `manage.py check` validates both grammars.

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

Removes every queue — tables, registry entry, and all tasks, runs, and events in them —
then prints the commands to re-provision. It does **not** uninstall Absurd: the schema,
migrations, and functions stay, so you never re-`migrate`.

With [`django_absurd.pg_cron`](cron-jobs.md) installed the flush also unschedules every
pg_cron job django-absurd owns — the schedule lanes and the `OPTIONS["CLEANUP"]` job —
and deletes every `ScheduledTask` row, so nothing fires at a queue that no longer
exists. `absurd_sync_crons` brings the settings-declared schedules back;
[admin-authored](cron-jobs.md#author-schedules-in-the-admin) ones are gone for good.

!!! warning "Destructive"

    This permanently deletes all task history across every queue, and every
    admin-authored schedule. Re-provision your declared queues afterward with `migrate`
    or `absurd_sync_queues`.

    On the [beat process](cron-jobs.md#application-side-beat) instead, schedules live in settings, so
    beat keeps enqueuing and **errors on each fire** until the queues exist again —
    re-provision promptly.

    Per-queue Absurd maintenance jobs from `absurdctl cron --enable <queue>` are dropped
    with their queue.
