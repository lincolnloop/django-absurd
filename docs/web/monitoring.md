---
icon: lucide/scroll-text
---

# Monitoring

```python title="settings.py"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        # everything from this package...
        "django_absurd": {"handlers": ["console"], "level": "INFO"},
        # ...or quiet one part of it
        "django_absurd.scheduler": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
```

Two loggers, configured like any other in Django's
[`LOGGING`](https://docs.djangoproject.com/en/6.0/topics/logging/#configuring-logging):

- **`django.tasks`** — Django's own task lifecycle. `AbsurdBackend` emits its signals,
  so this stays portable across backends.
- **`django_absurd`** — what Absurd did: attempts, durations, [worker](workers.md) and
  [beat](cron-jobs.md#application-side-beat) lifecycle, steps, replays, sleeps, event
  waits.
- One child per module, each levellable on its own — `django_absurd.scheduler` is the
  beat, `django_absurd.context` the durable primitives.

`absurd_worker` and `absurd_beat` attach a `StreamHandler` at `INFO` so a fresh project
is not silent.

- Naming `django_absurd` or one of its children in `LOGGING` stops them.
- Naming only `root` adds no handler but still raises the level, so a `WARNING` root
  does not swallow them.
- **Neither logger is the complete record.** Postgres is: the
  [stored result](tasks.md#read-the-result) and the queue-state models below.

## Query queue state

```python
from django_absurd.models import Task

Task.objects.filter(queue="reports", state="failed")
```

Tasks, Runs, Checkpoints, Events, Waits, and the Queues catalog are public models,
spanning every queue.

- **Filter by `queue=` whenever you can.** The views carry no cross-queue index, so
  `queue=` prunes to a single per-queue table, while an unfiltered query — ordering by
  `enqueue_at`, or filtering only on `state` — scans every queue's table.
- They are read-only: `save()` / `delete()` raise `QueueReadOnlyError`.

## Browse in the admin

With `django.contrib.admin` installed, django-absurd registers **read-only** admin pages
for the same models, filterable by queue. Turn them off with
[`ENABLE_ADMIN`](configuration.md#backend-options).

- **Non-default [`DATABASE`](configuration.md#backend-options):** these models read from
  the Absurd database, but Django's own `LogEntry`, session, and `ContentType` tables
  must still exist in `"default"` — run `migrate` there too.

→ [Django: the admin site](https://docs.djangoproject.com/en/6.0/ref/contrib/admin/).
