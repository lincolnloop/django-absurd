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
thread pool. On start it checks that `--queue` is declared and present in the queue
catalog, then polls for work. It provisions nothing: a queue that isn't there — no
catalog row, or a row whose tables are gone — exits with a `CommandError` before the
worker prints anything, so nothing announces a start it can't honour. Run `migrate` or
[`manage.py absurd_sync_queues`](configuration.md#declaring-queues) first. A queue
dropped from under a running worker fails on its next claim, with the same message.

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
- **`--concurrency`** — default `1`. Runs in flight per worker; also the sync
  thread-pool size. Raise it when tasks wait — on the database, an HTTP call, a sleep.
  Pure Python compute won't go faster; add worker processes for that.
- **`--batch-size`** — defaults to `--concurrency`. A claim never fetches more than the
  worker has free, so this only bites at `--concurrency 1`, where a bigger batch means
  fewer claims. Setting it to 1 costs a round trip per task and buys nothing.
- **`--poll-interval`** — default `0.25`. Applies only when a worker is idle; a busy one
  reclaims immediately. Lower it for latency on quiet queues, raise it to stop idle
  workers polling so often.
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

## Before a worker starts

`migrate` has to have run: it installs Absurd's schema and provisions the declared
queues, and a worker refuses to start on a queue that isn't provisioned. See
[Deploying](deploying.md) for the release step, the privileges `migrate` needs, and
adopting a database that already runs Absurd.
