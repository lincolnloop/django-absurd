---
icon: lucide/gauge
---

# Performance

Guidance for sizing a deployment and choosing [worker flags](workers.md#run-a-worker),
drawn from a measured benchmark rather than intuition.

**Read the benchmark numbers with a grain of salt.** They come from one eight-core
laptop running Postgres, the workers, and the benchmark itself on the same machine. The
absolute rates are a property of that box. What transfers is the ratios: what doubling a
knob buys, and where a curve bends. For a sense of scale, a single worker drained a
trivial task at roughly 140 to 190/s there, and eight workers at roughly 700 to 810/s.
Treat those as an order of magnitude, never a specification.

Those ranges are wide on purpose. The same measurement moved by 15% to 47% depending on
how long the Postgres process had been running, which is worth reading about before
comparing any number here against your own: see
[Database uptime changes what you measure](#database-uptime-changes-what-you-measure).

## Tasks, runs, claims, checkpoints

Absurd's vocabulary, and when each concept touches Postgres:

- A **task** is one unit of enqueued work. `enqueue()` calls Absurd's `spawn_task`,
  which inserts the task row and its first **run** row in a single round trip. Inside
  `transaction.atomic()`, workers see the rows at commit, not before.
- A **run** is one attempt at a task. A retry inserts a new run row for the same task;
  the failed row stays behind as history.
- A **claim** is one `claim_task` query from a worker. It picks up to `--batch-size` due
  runs, marks each `running`, stamps who claimed it and when it started, and gives each
  a lease that expires `--claim-timeout` seconds later.
- A **checkpoint** is a saved step result. Each `ctx.step` that executes writes its
  result to Postgres immediately, one round trip per step.

The life of a claimed batch, in order:

1. The worker sends one claim query and receives up to `--batch-size` runs.
2. Each run starts executing as soon as the worker has free execution capacity (see the
   knobs below).
3. Each run finishes on its own: one `complete_run` (or `fail_run`) write at the moment
   that run ends. Runs claimed together do not wait for each other, and they do not
   report their results together.
4. If a worker dies mid-run, the lease expires. A later claim query, from any worker,
   notices the expired lease, fails that run, and inserts a fresh run for the retry. The
   new run loads the task's committed checkpoints in one query and skips every step that
   already ran.

Runs in one claim therefore share exactly two things: the fetch query, and the lease
clock, which starts at claim time for the whole batch. They share no state, and one
run's failure has no effect on its batch-mates.

The floor for a trivial task is two round trips: its share of one claim, plus its own
completion write.

## The three worker knobs

- `--concurrency` is the worker's **execution capacity**: how many claimed runs it
  executes at once. One worker process runs one event loop, whatever the concurrency.
  `async def` tasks run on that loop, up to `--concurrency` at a time; sync tasks run in
  a thread pool of the same size. Raising concurrency never adds event loops or
  processes.

  Coming from Celery: there the meaning of `--concurrency` depends on the pool, and the
  default prefork pool makes it child processes. django-absurd has one execution model,
  and `--concurrency` is always in-process, closest to Celery's threads or gevent pools.
  To add processes, start more `absurd_worker` commands; that is what the scaling table
  below measures.

- `--batch-size` is the **claim capacity**: the most runs one claim query returns.
  Unset, it equals `--concurrency`. Above `--concurrency 1`, a claim also never fetches
  more runs than the worker has free execution capacity. `--concurrency 1` is the
  exception: that worker claims its whole `--batch-size` in one query and executes the
  runs one after another.

- `--poll-interval` is how long an idle worker sleeps before asking again. It paces
  nothing under load: a busy worker claims again the moment a run finishes. The interval
  only applies when the previous claim came back empty.

Execution capacity refills as soon as a run finishes, so one slow run never stalls the
others it was claimed alongside.

## Don't set `--batch-size` to 1

A worker with `--concurrency 16` and `--batch-size 1` ran at the same rate as a worker
with `--concurrency 1`: 68 tasks/s against 67. The execution capacity is not discarded:
the worker re-claims the moment capacity frees, so runs still overlap. What changes is
the price. Filling sixteen units of capacity now takes sixteen claim round trips instead
of one, and on a short task the claim round trip is the dominant cost, so throughput
collapses to the rate of a single claim query. The measured penalty is round-trip cost,
not idle capacity.

The default is already right. Doubling it to 32 changed nothing measurable (2.36x versus
2.35x). Leave it unset unless you have a specific reason, and never lower it hoping to
reduce latency. `--poll-interval` is the knob for that.

## Concurrency pays off, then flattens

Going from `--concurrency 1` to `16` on one worker bought **2.35x** the throughput, not
16x. For a short task almost all the elapsed time is database round trips, and
concurrency can only overlap those.

Tasks that wait on something external go further, but still not indefinitely. At 50 ms
of simulated IO per task, a worker reached 60% of its theoretical ceiling at concurrency
4, 34% at 16, and 22% at 32. Past roughly 16 concurrent runs the claim and completion
writes become the constraint rather than the waiting.

Concurrency is capacity for overlapping IO waits, not CPU parallelism, so it does not
scale with core count. Start at `--concurrency 4` for IO-bound work and `1` to `2` for
CPU-bound work, and raise it only if you can see spare capacity going unused.

## Scale out with processes

| Workers | Throughput | Efficiency |
| ------- | ---------- | ---------- |
| 1       | 1.00x      | 1.00       |
| 2       | 1.39x      | 0.70       |
| 4       | 2.39x      | 0.60       |
| 6       | 3.22x      | 0.54       |
| 8       | 4.18x      | 0.52       |

Throughput keeps rising as workers are added, but with diminishing returns on each new
worker: by eight, each one contributes about half of what the first did. No hard ceiling
appeared within the eight processes this host could hold.

Expect this curve to look better when your database is on its own host. These numbers
were taken with eight workers, their concurrent runs, and Postgres all competing for the
same eight cores, so they cannot separate claim contention from plain CPU starvation.

## `--poll-interval` sets your latency floor

The interval is the idle cadence: how often a worker with nothing to do asks again. A
task that arrives on an idle queue waits, on average, half of it:

| `--poll-interval` | Median wait | Claims/s per idle worker |
| ----------------- | ----------- | ------------------------ |
| `0.05`            | 39 ms       | 19.4                     |
| `0.25` (default)  | 139 ms      | 4.2                      |
| `1.0`             | 513 ms      | 1.2                      |

The cost is one query per idle worker per interval, forever. At `0.05` a fleet of eight
workers issues about 160 queries a second into an empty database. Lower it when latency
on a quiet queue matters; raise it when it doesn't and you would rather not pay the
polling tax. It has no effect on a saturated worker, which never sleeps.

## Sync or async, whichever reads better

At the same IO wait, async and sync tasks performed identically, within 1% across
concurrency 4, 16 and 32. The thread pool that runs sync tasks is not a bottleneck at
these sizes.

Write `async def` when the task's own work is naturally async. Don't convert a working
sync task expecting a speedup.

## What a checkpoint costs

A task with four [steps](workflows.md#steps) cost **4.55x** a flat task doing the same
trivial work, so each `ctx.step` costs roughly as much as running a whole task. Each one
is a durable write; that is what buys you resumption.

Checkpoint at boundaries worth not repeating, like an external charge or a slow import,
rather than at every line. Four checkpoints on a task that already takes a second are
free in relative terms; four on a task that takes a millisecond are the whole cost.

## The enqueue side is often the bottleneck

The producer is whatever code calls `enqueue()`. Usually that is your existing
application, a web process for example, not something extra you write.

| How you enqueue                   | Rate    | Per call |
| --------------------------------- | ------- | -------- |
| One at a time, one connection     | 1.00x   | 4.8 ms   |
| One at a time, 8 threads          | 4.32x   | 8.6 ms   |
| Batched in `transaction.atomic()` | **12x** | 0.4 ms   |

Each `enqueue()` is its own round trip, so a single-threaded producer topped out well
below what eight workers could consume. It could not keep more than about three workers
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

The trade is atomicity. If anything makes the transaction roll back (an exception inside
the block, a lost connection, a crash before commit), every enqueue in it is gone
together, and workers see none of them until the commit lands. Batch in chunks of a few
hundred and retry a failed chunk, rather than putting one giant batch in one
transaction.

Enqueuing one task inside a web request is fine; it is a few milliseconds. Enqueuing
thousands in a loop without a transaction is not.

## Leave headroom, and measure your own cliff

End-to-end latency stays flat while the fleet has headroom, then rises. How sharply it
rises turned out to depend less on Absurd than on how long the Postgres process had been
running. The same four stage G rows, measured twice:

| Load | Postgres up 75 min | Postgres up 3 min |
| ---- | ------------------ | ----------------- |
| 25%  | 51 ms              | 49 ms             |
| 50%  | 64 ms              | 46 ms             |
| 75%  | 91 ms              | 63 ms             |
| 90%  | **1.24 s**         | **140 ms**        |

Median end-to-end latency, `enqueue()` to completion. The left column offered up to 338
tasks/s against a measured capacity of 375.5; the right offered up to 445 against a
capacity of 494.5, because a freshly started server also measured a 32% higher ceiling.

On the long-running server the median rose **13.7x** between 75% and 90%, and repeats of
the 90% row disagreed by 57%. On the fresh one the same step cost **2.2x**, the workers
kept up with the full offered rate, and repeats held within 114 ms of each other.

**Capacity** is the rate at which the same fleet drained an already-full queue. Each row
then offers tasks at a fixed percentage of that capacity for 60 seconds. A saturation
run cannot answer this question: when the queue starts full, every task but the first
waits behind the whole backlog, so its latency is just drain time.

Two things to take from this. Latency does rise as you approach capacity, which is
ordinary queueing behaviour and worth designing against whatever your hardware does. And
the size of the cliff is a property of your database's state, not a constant you can
copy from this page. Measure it where you run it.

### Database uptime changes what you measure

Every worker-side result improved by 15% to 47% when each stage ran against a freshly
restarted Postgres instead of one that had been under load for the preceding hour. Pure
enqueue throughput, with no workers involved at all, improved by 20% to 30%, so this is
not specific to claiming. The effect returns within a single stage.

We did not isolate the cause. Buffer eviction pressure on a fully populated
`shared_buffers` is the likeliest candidate, but it is inference, not a measurement.

Every result file records `postgres_uptime_s`, so a number can always be traced back to
the regime that produced it, and `benchmarks/run_stages_cold.sh` restarts the server
between stages when you want that variable held still.

## Running the benchmark

The numbers above describe one laptop. To understand your own limits, run the benchmark
on your own hardware, against your own Postgres. The secret is to measure, measure, and
measure again. From a checkout of
[the repository](https://github.com/lincolnloop/django-absurd/tree/main/benchmarks):

```bash
docker compose -f benchmarks/compose.yaml up -d db_bench
uv run python benchmarks/manage.py migrate
uv run python -m benchmarks.stages --all
uv run python -m benchmarks.report
```

The full run took 75 minutes on the reference host and wants an otherwise idle machine.
Two flags shorten it:

- `--stage a` (repeatable, `a` to `g`) runs one stage instead of all seven. Stages
  calibrate from their predecessors' result files, so for the latency table above run
  `--stage a --stage b --stage g`.
- `--reps N` overrides how many times each measurement repeats. The default is 3; the
  median repeat is kept and the spread across repeats decides whether the result is
  trustworthy. `--reps 1` turns any stage into a quick dry run whose numbers are
  indicative only.

To hold database uptime still across the whole sweep, `benchmarks/run_stages_cold.sh`
restarts Postgres before each stage and runs them in order. It measures a best case
rather than a representative one, since nothing restarts a production database between
workloads, so prefer it for comparing runs against each other rather than for sizing.

Results land in `benchmarks/results/` as JSON, and the report renders them as the tables
above.

Every timing is read from Absurd's own `enqueue_at`, `started_at` and `completed_at`
columns rather than from the harness, so the producer and the workers are measured on
one clock. A measurement whose repeats disagree by more than 15%, or that the host slept
through, is flagged and excluded from the ratios rather than published. Latency
measurements carry a 150 ms floor under that check, because relative spread divides by
the median and so grows as a measurement gets faster: repeats of 88, 139 and 202 ms read
as 82% apart while being tight enough to quote.

Reference host: 8 cores, Postgres 18.6 in Docker on the same machine, Python 3.14,
Django 6.1, absurd-sdk 0.5.0.
