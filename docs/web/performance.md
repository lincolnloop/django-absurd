---
icon: lucide/gauge
---

# Performance

Guidance for sizing a deployment and choosing [worker flags](workers.md#run-a-worker),
drawn from a measured sweep rather than intuition.

**Read the figures below as ratios.** They come from one 8-core laptop running Postgres
in a container on the same machine, so the absolute rates are a property of that box.
The relationships — what doubling a knob buys, where a curve bends — are what transfer.
For a sense of scale: a single worker drained a trivial task at roughly 150/s there, and
eight workers at roughly 650/s. Treat those as an order of magnitude, never a
specification.

## What a worker does per task

A worker polls, claims a batch of runs in one query, executes them, and writes a
completion row for each. So each task costs at least two database round trips, and the
claim is shared across everything it claimed.

- `--concurrency` is how many tasks run at once: `async def` tasks on an event loop,
  sync tasks in a thread pool of the same size.
- `--batch-size` is how many tasks one claim query fetches. Unset, it equals
  `--concurrency`.
- `--poll-interval` is how long an idle worker waits before asking again.

A slot is refilled as soon as it frees, so one slow task never stalls the others it was
claimed alongside.

## Don't set `--batch-size` to 1

A worker with 16 slots and `--batch-size 1` ran at the same rate as a worker with
**one** slot — 68 tasks/s against 67. Claiming a single task per query throws away every
extra slot you paid for, because the claim round trip dominates a short task.

The default is already right. Doubling it to 32 changed nothing measurable (2.36x versus
2.35x). Leave it unset unless you have a specific reason, and never lower it hoping to
reduce latency — `--poll-interval` is the knob for that.

## Concurrency pays off, then flattens

Going from 1 to 16 slots on one worker bought **2.35x** the throughput, not 16x. For a
short task almost all the elapsed time is database round trips, and concurrency can only
overlap those.

Tasks that wait on something external go further, but still not indefinitely. At 50 ms
of simulated IO per task, a worker reached 60% of its theoretical ceiling at 4 slots,
34% at 16, and 22% at 32. Past roughly 16 slots the claim and completion writes become
the constraint rather than the waiting.

Start at `--concurrency 4` for IO-bound work, `1`–`2` for CPU-bound work, and raise it
only if you can see slots sitting idle.

## Scale out with processes

| Workers | Throughput | Efficiency |
| ------- | ---------- | ---------- |
| 1       | 1.00x      | 1.00       |
| 2       | 1.39x      | 0.70       |
| 4       | 2.39x      | 0.60       |
| 6       | 3.22x      | 0.54       |
| 8       | 4.18x      | 0.52       |

Throughput keeps climbing as you add worker processes, but each one returns less: by
eight workers you get about half a worker's worth per worker.

Expect this curve to look better than the table when your database is on its own host.
These numbers were taken with eight workers, their slots, and Postgres all competing for
the same eight cores, so they cannot separate claim contention from plain CPU
starvation.

## `--poll-interval` sets your latency floor

An idle worker only notices work on its next poll, so median wait on an otherwise empty
queue is about **half** the interval:

| `--poll-interval` | Median wait | Claims/s per idle worker |
| ----------------- | ----------- | ------------------------ |
| `0.05`            | 39 ms       | 19.4                     |
| `0.25` (default)  | 139 ms      | 4.2                      |
| `1.0`             | 513 ms      | 1.2                      |

The cost is one query per worker per interval, forever. At `0.05` a fleet of eight
workers issues about 160 queries a second into an idle database. Lower it when latency
on a quiet queue matters; raise it when it doesn't and you would rather not pay the
polling tax.

## Sync or async, whichever reads better

At the same IO wait, async and sync tasks performed identically — within 1% across
concurrency 4, 16 and 32. The thread pool that runs sync tasks is not a bottleneck at
these sizes.

Write `async def` when the task's own work is naturally async. Don't convert a working
sync task expecting a speedup.

## What a checkpoint costs

A task with four [steps](workflows.md#steps) cost **4.55x** a flat task doing the same
trivial work, so each `ctx.step` costs roughly as much as running a whole task. Each one
is a durable write; that is what buys you resumption.

Checkpoint at boundaries worth not repeating — an external charge, a slow import —
rather than at every line. Four checkpoints on a task that already takes a second are
free in relative terms; four on a task that takes a millisecond are the whole cost.

## The enqueue side is often the bottleneck

| How you enqueue                   | Rate    | Per call |
| --------------------------------- | ------- | -------- |
| One at a time, one connection     | 1.00x   | 4.8 ms   |
| One at a time, 8 threads          | 4.32x   | 8.6 ms   |
| Batched in `transaction.atomic()` | **12x** | 0.4 ms   |

Each `enqueue()` is its own round trip, so a single-threaded producer topped out well
below what eight workers could consume — it could not keep more than about three workers
busy.

**Wrapping bulk enqueues in `transaction.atomic()` is the largest single improvement
anywhere in these measurements.** Amortising the commit across a few hundred tasks took
per-call cost from 4.8 ms to 0.4 ms:

```python
from django.db import transaction

with transaction.atomic():
    for order in batch:
        process_order.enqueue(order.id)
```

Enqueuing one task inside a web request is fine — it is a few milliseconds. Enqueuing
thousands in a loop without a transaction is not.

## Leave headroom

End-to-end latency stays flat until the fleet is busy, then rises sharply:

| Load (share of capacity) | Median | p99    |
| ------------------------ | ------ | ------ |
| 25%                      | 51 ms  | 94 ms  |
| 50%                      | 64 ms  | 146 ms |
| 75%                      | 91 ms  | 165 ms |
| 90%                      | 1.24 s | 6.2 s  |

**Capacity** here is the rate at which the same fleet drained an already-full queue —
375.5 tasks/s for the four workers these rows were measured on. Each row then offers
tasks at a fixed share of that rate for 60 seconds and measures how long each one took
from `enqueue()` to completion. A saturation run cannot answer this: when the queue
starts full, every task but the first waits behind the whole backlog, so its latency is
just drain time.

Between 75% and 90% the median rose **13.7x**. That is ordinary queueing behaviour
rather than anything specific to Absurd — as utilisation approaches capacity, waiting
time grows without bound — but it is worth designing against.

**Size your fleet so steady-state load sits under about 75% of measured capacity.** The
90% measurement also varied by 57% between repeats: near saturation the system is not
merely slower, it is unpredictable.

To measure this for your own workload and hardware:

```bash
docker compose -f benchmarks/compose.yaml up -d db_bench
uv run python benchmarks/manage.py migrate
uv run python -m benchmarks.sweep --stage b --stage g
```

Stage B measures the ceiling and stage G offers against it, so B has to run first — on
its own, `--stage g` reads the stored `benchmarks/results/stage_b.json` and errors if it
is absent. Budget about 25 minutes for both, on an otherwise idle machine.

## Reproducing this

Every figure above comes from
[`benchmarks/`](https://github.com/lincolnloop/django-absurd/tree/main/benchmarks) in
the repository. From a checkout of it:

```bash
docker compose -f benchmarks/compose.yaml up -d db_bench
uv run python benchmarks/manage.py migrate
uv run python -m benchmarks.sweep --all
uv run python -m benchmarks.report
```

The full sweep took 75 minutes on the reference host and wants an otherwise idle
machine; `--stage a` (repeatable, `a`–`g`) runs one stage and `--reps 1` turns any of
them into a quick dry run. Results land in `benchmarks/results/` as JSON, and the report
renders them as the tables above.

Every timing is read from Absurd's own `enqueue_at`, `started_at` and `completed_at`
columns rather than from the harness, so the producer and the workers are measured on
one clock. A cell whose repeats disagree by more than 15%, or that the host slept
through, is flagged and excluded from the ratios rather than published.

Reference host: 8 cores, Postgres 18.6 in Docker on the same machine, Python 3.14,
Django 6.1, absurd-sdk 0.5.0.
