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
