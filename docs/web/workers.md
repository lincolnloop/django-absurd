---
icon: lucide/cpu
---

# Workers

A worker claims [tasks](tasks.md) from a queue and runs them. Nothing runs without one.

## Run a worker

```bash
python manage.py absurd_worker --queue reports --concurrency 4
```

One worker runs both sync and `async def` tasks — async on an event loop, sync in a
thread pool. On start it provisions every declared queue and rebuilds the admin views,
then polls for work.

| Flag              | Default        | What it does                                                       |
| ----------------- | -------------- | ------------------------------------------------------------------ |
| `--queue`         | `default`      | Queue to consume.                                                  |
| `--concurrency`   | `1`            | Max tasks in flight; also the sync thread-pool size.               |
| `--claim-timeout` | `120`          | Seconds before a claimed task returns to the queue.                |
| `--poll-interval` | `0.25`         | Seconds between polls.                                             |
| `--batch-size`    | unset          | Max tasks claimed per poll; defaults to `--concurrency`.           |
| `--worker-id`     | `<host>:<pid>` | Identifier recorded on each claim.                                 |
| `--beat`          | off            | Also run the [beat scheduler](cron-jobs.md#application-side-beat). |

- **One worker per queue.** `--queue` takes a single name; run a process per queue.
- A run that makes no progress within `--claim-timeout` is re-claimed and replayed from
  its last [checkpoint](workflows.md#steps) — see [long steps](workflows.md#long-steps).
- Run **exactly one** `--beat` across your fleet; there is no leader election.

→
[Absurd: Concepts — Workers](https://earendil-works.github.io/absurd/concepts/#workers).

## Runs & retries

Each attempt at a task is a **run**. A failed task retries up to its
[`max_attempts`](tasks.md#retries-spawn-options), and work wrapped in a
[step](workflows.md#steps) is checkpointed — persisted and skipped on the next run — so
retries never redo completed work.

→
[Absurd: Concepts — Retries](https://earendil-works.github.io/absurd/concepts/#retries).

## Schema & migrations

```bash
python manage.py migrate
```

Absurd's schema ships as a Django
[migration](https://docs.djangoproject.com/en/6.0/topics/migrations/) and installs the
declared queues with it. The SQL comes from the pinned Absurd version and is never
fetched at migrate time.

→ [Absurd: database setup](https://earendil-works.github.io/absurd/database/).
