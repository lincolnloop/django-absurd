---
icon: lucide/trash-2
---

# Cleanup / retention

Absurd stores task rows in Postgres — they accumulate unless you prune them. Each queue
exposes two retention knobs (see
[Configuration — Declaring queues](configuration.md#declaring-queues)):

| Option          | What it controls                                                                                         |
| --------------- | -------------------------------------------------------------------------------------------------------- |
| `cleanup_ttl`   | Minimum age a terminal task must reach before it is deleted.                                             |
| `cleanup_limit` | Max terminal rows deleted **per queue** per run — applied separately to task and event rows (batch cap). |

**Terminal** means completed, failed, or cancelled — running and pending tasks are never
touched. See [Absurd's storage docs](https://earendil-works.github.io/absurd/storage/)
for the full retention model.

## Run on demand

```bash
python manage.py absurd_cleanup            # every queue
python manage.py absurd_cleanup reports    # only the named queue(s)
```

Deletes eligible rows across the configured Absurd backend and prints per-queue counts:

```
default: 12 tasks, 0 events deleted
```

The same function is importable — `cleanup_queues()` for all queues, or
`cleanup_queues(["reports", "emails"])` for specific ones — returning a list of
per-queue count dicts.

## Schedule recurring cleanup

There is no built-in scheduled task — write a one-line `@task` wrapper in your app and
register it in [`SCHEDULE`](cron-jobs.md):

```python title="myapp/tasks.py"
from django.tasks import task
from django_absurd.cleanup import cleanup_queues

@task
def cleanup():
    return cleanup_queues()
```

```python title="settings.py"
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "SCHEDULE": {
                "absurd-cleanup": {
                    "task": "myapp.tasks.cleanup",
                    "cron": "0 3 * * *",   # 3am daily
                },
            },
        },
    },
}
```

The wrapper runs on its `@task` queue (or the `queue` key in the schedule entry). Its
return value — a list of per-queue dicts — is stored as the task result and retrievable
via `get_result` (see [Tasks — Read the result](tasks.md#read-the-result)).

## Reset — drop all queues

`absurd_flush` drops **every** queue and its data (tasks, runs, events). The Absurd
schema and functions are kept — only the queues go. It prompts for confirmation; pass
`--noinput` (alias `--no-input`) to skip the prompt in automation:

```bash
python manage.py absurd_flush            # prompts, then drops on 'yes'
python manage.py absurd_flush --noinput  # drops without prompting
```

!!! warning "Destructive"

    This deletes all task history across every queue. Re-provision your declared queues
    afterward with `migrate`, `absurd_sync_queues`, or by starting a worker.
