---
icon: lucide/timer
---

# Cron Jobs

Run [tasks](tasks.md) on a recurring **cron** cadence. django-absurd offers two ways to
drive schedules, both following
[Absurd's cron patterns](https://earendil-works.github.io/absurd/patterns/cron/):
**application-side** (a beat process — available now) and **database-side**
([`pg_cron`](#database-side-pg_cron) — coming soon).

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

!!! info "Coming soon"

    A database-side scheduler built on
    [`pg_cron`](https://earendil-works.github.io/absurd/patterns/cron/) — Postgres fires
    the schedule directly, no beat process to run. Planned as an opt-in alternative to
    the application-side beat above. Not yet available.
