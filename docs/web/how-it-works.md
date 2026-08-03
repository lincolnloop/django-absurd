---
icon: lucide/cog
---

# How it works

django-absurd is a thin layer: Django's task API on top, Absurd's engine underneath. You
mostly write plain Django [tasks](tasks.md) — this page explains what's happening below,
and links to the source docs for each piece.

## The flow

You [**enqueue**](tasks.md) a task onto a [**queue**](#queues). A [**worker**](#workers)
claims it and creates a [**run**](#runs-retries-checkpoints). The task can be broken
into **steps** ([checkpoints](#runs-retries-checkpoints)) whose results are saved so
they don't re-execute on retry. A task can also
[**sleep** or **wait for an event**](#events-waits), suspending until it's time to
resume.

→ [Absurd: Concepts](https://earendil-works.github.io/absurd/concepts/) (durable
execution, tasks, steps, runs, events, retries).

## Queues

A named lane tasks flow through. Declare them in your
[configuration](configuration.md#declaring-queues); they're provisioned at `migrate` and
on worker start. Queues are **unpartitioned** by default. **Partitioned** storage is
declarable but **experimental — not tested yet**, and its partition lifecycle
(provisioning + detaching old partitions) is not automated; don't rely on it in
production.

→ [Absurd: Storage](https://earendil-works.github.io/absurd/storage/) (queue types,
partitioning, retention).

## Runs, retries & checkpoints

Each attempt at a task is a **run**. A failed task is retried up to its
[`max_attempts`](tasks.md#retries-spawn-options). Work wrapped in a **step** is
checkpointed — its result is persisted and skipped on the next run — so retries and
resumes don't redo completed work.

→ [Absurd: Concepts](https://earendil-works.github.io/absurd/concepts/).

## Events & waits

A task can suspend until a named **event** is emitted, or **sleep** until a time, then
resume where it left off (the worker wakes it — no external scheduler).

→ [Absurd: Concepts](https://earendil-works.github.io/absurd/concepts/).

## Workers

```bash
python manage.py absurd_worker --queue reports
```

One worker runs both sync and `async def` [tasks](tasks.md) (async on an event loop,
sync in a thread pool). On start it does a full sync — provisioning every declared queue
and rebuilding the admin views — then polls for work.

## Django Task lifecycle logging

Django logs a task's lifecycle on the `django.tasks` logger — `DEBUG` when it's
enqueued, `INFO` at `state=RUNNING` and `state=SUCCESSFUL`, `ERROR` with a traceback at
`state=FAILED`. `AbsurdBackend` emits the signals Django's logging listens for, so those
lines appear for Absurd tasks too.

Django's logs are not a complete record. Postgres is: a retried attempt's failure logs
nothing, and neither does an ending Absurd decided itself — a
[`max_delay`/`max_duration` cancellation](tasks.md#retries-spawn-options), an expired
claim, or a cancellation. The [stored result](tasks.md#read-the-result) and the
queue-state models below are the record.

## django-absurd's own logging

django-absurd reports what Absurd itself is doing on its own loggers — one per module,
all children of `django_absurd` — so you can route or level them through Django's
[`LOGGING`](https://docs.djangoproject.com/en/6.0/topics/logging/#configuring-logging)
setting like any other library's. The children that emit:

- `django_absurd.hooks` — each run's lifecycle.
- `django_absurd.worker` — [worker](#workers) start and stop.
- `django_absurd.scheduler` — the [beat scheduler](cron-jobs.md#run-the-beat).
- `django_absurd.queues` — [queue](#queues) provisioning.
- `django_absurd.cleanup` — [cleanup](cleanup.md) runs.
- `django_absurd.dispatch` — a failing task-signal receiver, logged with its traceback
  at `ERROR`.
- `django_absurd.tasks`, `django_absurd.pg_cron.apps`, and
  `django_absurd.pg_cron.signals` — warnings only.

Configure the parent to cover everything, or target one child — silencing just the beat
means setting `django_absurd.scheduler` to `WARNING`.

**`absurd_worker` and `absurd_beat` make their own lines visible.** Django's default
logging configuration covers the `django` logger only, and the root logger's default
level is `WARNING`, so without help these commands would run completely silent. On
startup, each makes two independent adjustments:

- it attaches one plain `StreamHandler` to `django_absurd`, but only when nothing on the
  logger's ancestor chain would already catch its records — a project that configured
  just the root logger keeps a single copy of each line;
- it raises the logger's level to `INFO`, but only when your `LOGGING` gives
  `django_absurd` no explicit level of its own — and this happens whether or not a
  handler was attached, because a root handler at the default `WARNING` level would
  otherwise filter every `INFO` line before it reached that handler.

The opt-out is the level: quiet this package by setting a **level** on `django_absurd`
in your `LOGGING`. Merely configuring the logger is not enough — handlers with no
`level` key leave it at `NOTSET`, and the command still raises it to `INFO`. A globally
quiet project therefore gets `INFO` from this package while one of these commands runs.
That is deliberate: a foreground command whose whole job is reporting task lifecycle
should not be silent.

Log records are plain text. The emoji in the commands' console output is written by the
commands themselves, never into a log record — a glyph the log stream cannot encode
would cost the whole log line.

## Admin & ORM introspection

When `django.contrib.admin` is installed, django-absurd registers **read-only** admin
pages for Tasks, Runs, Checkpoints, Events, Waits, and the Queues catalog — each
spanning all queues, filterable by queue. The same models are public for querying:

```python
from django_absurd.models import Task

Task.objects.filter(queue="reports", state="failed")
```

→ [Django: The admin site](https://docs.djangoproject.com/en/6.0/ref/contrib/admin/).

## Schema & migrations

Absurd's schema ships as a Django
[migration](https://docs.djangoproject.com/en/6.0/topics/migrations/) (offline — the SQL
comes from the pinned Absurd version, never fetched at migrate time). `migrate` installs
it and provisions declared queues.

→ [Absurd: Database setup](https://earendil-works.github.io/absurd/database/).
