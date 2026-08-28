# `benchmarks/` — the django-absurd load harness

A re-runnable measurement rig for three questions: how many tasks a worker topology
actually drains, what latency looks like under a fixed offered rate, and what the knobs
(`--concurrency`, `--batch-size`, `--poll-interval`) buy. It is internal tooling —
nothing here ships in the `django_absurd` wheel, and it is not a Django app: no models,
no migrations, just a settings module, a task module, and two CLI drivers.

## What it found

Measured on one 8-core laptop with Postgres in Docker on the same box
(`benchmarks/results/`, rendered by `python -m benchmarks.report`). **Absolute rates are
a property of that machine; only the ratios travel.** A tasks/s figure quoted without
its host context will be read as django-absurd's number rather than this laptop's.

| finding                                                    | measured                                                                                 |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `--batch-size 1` throws away every extra slot              | a 16-slot worker drops to 68.1 tasks/s, matching `--concurrency 1` at 66.6               |
| concurrency has sharply diminishing returns on short tasks | 16x the slots buys 2.35x the throughput                                                  |
| worker processes scale, at falling efficiency              | 1 to 8 workers is 4.2x; per-worker efficiency 1.00 to 0.52                               |
| `poll_interval` sets the latency floor                     | median wait is half the interval, and each idle worker costs `1/poll` claims/s           |
| async and sync tasks perform the same                      | 0.99-1.00x at 50 ms of IO, across concurrency 4/16/32                                    |
| a checkpoint costs about a whole task                      | a 4-step `ctx.step` workflow costs 4.55x a flat one                                      |
| batching the enqueue side is the biggest single lever      | `transaction.atomic()` over 500-task chunks is 12x a plain loop (199 to 2398 enqueues/s) |
| latency has a knee just above 75% utilisation              | p50 runs 51, 64, 91 ms at 25/50/75% of capacity, then 1244 ms at 90%                     |

The last row is the one to design against: **keep workers under about 75% of measured
capacity.** Between 75% and 90% the median rises 13.7x and p99 reaches 6.2 s. The 90%
cell is flagged for a 57.7% spread across reps, and that instability is the finding
rather than a bad measurement.

The 0.52 efficiency figure earns its own caveat: 8 workers x 16 slots is 128 slots on an
8-core box that is also running Postgres, so the number cannot separate claim contention
from CPU starvation. A run with the database on its own host would likely look better.

## Reproducing from a clean checkout

```
docker compose -f benchmarks/compose.yaml up -d db_bench
uv sync
uv run python benchmarks/manage.py migrate
uv run pytest benchmarks
uv run python -m benchmarks.sweep --all
uv run python -m benchmarks.report > /tmp/bench-report.md
```

Step 4 is the smoke: it runs the whole harness end to end (real `absurd_worker`
subprocesses, real enqueues, real SQL analysis) against a pytest-django test database,
`test_absurd_bench`, so it is safe to run while committed results exist. Step 5 took 75
minutes on the reference host; `--stage A` (repeatable, case-insensitive, `A`–`G`) runs
one stage, and `--reps 1` turns any stage into a fast dry run.

`db_bench` listens on `${PGPORT_BENCH:-5435}` and keeps a named volume, so it is a
different server from the root `compose.yaml`'s `db` and `db_pg_cron`. Those two do not
need to be running for anything in this directory.

## The stages

Times are from the 8-core reference run and scale with the host.

| stage | question                                    | varies                                                | workload                   | ~time  |
| ----- | ------------------------------------------- | ----------------------------------------------------- | -------------------------- | ------ |
| A     | what one worker's knobs buy                 | `--concurrency` 1-16, then `--batch-size`, then async | 5,000 no-ops, saturation   | 20 min |
| B     | how throughput scales with worker processes | worker count, at A's winning config                   | no-ops, saturation         | 10 min |
| C     | what `--poll-interval` costs and buys       | 0.05 / 0.25 / 1.0 s, plus idle probes                 | 5 tasks/s offered for 60 s | 10 min |
| D     | whether async tasks beat sync ones          | sync vs async x concurrency 4/16/32                   | 50 ms sleep, saturation    | 15 min |
| E     | what a checkpoint costs                     | 4-step workflow vs flat task                          | 2,000 tasks, saturation    | 3 min  |
| F     | how fast the producer can enqueue           | one connection / 8 threads / `atomic()` chunks        | 5,000 enqueues, no workers | 2 min  |
| G     | what latency looks like under load          | 25/50/75/90% of B's ceiling                           | 60 s paced offer           | 14 min |

**Worker counts scale with the host.** `build_worker_ladder` derives stage B's ladder
from `os.cpu_count()`: 1 and 2 anchor the low end where per-worker efficiency is still
readable, then quarter, half, three-quarter and full. Eight cores gives 1, 2, 4, 6, 8;
thirty-two gives 1, 2, 8, 16, 24, 32.

Stage G calibrates from the fastest stage B cell at or below `RATE_WORKER_CAP` (half the
cores) rather than from the outright ceiling, because a rate cell's producer runs on the
same machine as its workers. Calibrated off a full-core ceiling it asks for an offer the
producer has no cores left to deliver, and the upper cells flag having measured a load
that was never applied.

The concurrency ladders are deliberately **not** scaled: concurrency is slots for
overlapping IO waits rather than CPU parallelism, so an IO-bound task wants many slots
whatever the core count, and scaling them would make the async-vs-sync ratio
incomparable across machines.

## The measurement model

**Two experiments, and they answer different questions.** A _saturation_ cell preloads a
backlog, starts the workers, and waits for the queue to drain: it measures the ceiling,
and its latency numbers are meaningless because every task but the first waited in a
queue that was full by construction. A _rate_ cell starts the workers first and then
offers tasks at a fixed rate for a fixed duration: because arrivals are paced, queue
wait is a real number and end-to-end percentiles mean something. Latency guidance
therefore only ever comes from rate cells.

**Every timing is a Postgres column.** The harness reads `t_bench.enqueue_at`,
`r_bench.started_at` and `r_bench.completed_at` — all written server-side by Absurd's
own SQL — so producer and workers are timed on one clock with no skew to correct for.
The driver contributes no timestamps: throughput is `0.8 · n / (p90 − p10)` over the
completion times, trimming the ramp and the tail, and fairness is
`count(*) group by claimed_by` over the same rows the throughput count uses.

Redelivery needs no instrumentation either, but it does need the right query. Absurd's
`fail_run` marks the failed run `'failed'` and inserts a **new** row for the retry, and
`complete_run` only ever accepts a run that is still `'running'` — so a completed-only
count can never see more than one run per task and would report every redelivery as
zero. `extra_runs` is therefore counted over **every** run row
(`total_runs - total_tasks`), with `max_attempt` recorded beside it to say what kind of
redelivery it was. Throughput keeps its own completed-only count, so failed and pending
rows can never inflate it.

**The suspension guard.** A cell brackets its measured phase with both
`time.perf_counter()` and `time.time()`. If wall time outruns monotonic time by more
than two seconds the host napped or stalled mid-phase, the rep is thrown away, and the
cell is flagged. A wall clock stepping _backwards_ is an NTP correction, not a nap, and
is tolerated.

**Flagging is the honesty mechanism, not a failure.** Every rep votes, not just the
median one — a rep that under-offered has a _lower_ latency, so it sorts away from the
median and would never be looked at. A cell is flagged when:

- its spread across reps exceeds 15% (spread is measured over throughput for a
  saturation cell and over end-to-end p50 for a rate cell, matching whichever metric
  that mode ranks its reps by);
- any rep was suspended mid-phase;
- any rep saw `extra_runs > 0`, i.e. a redelivery;
- a saturation cell finished with fewer completed tasks than it enqueued
  (`missing_tasks`) — a terminally failed task still satisfies the drain predicate, so
  without this the sample shrinks silently;
- any rep's trimmed completion window was too short to divide by (`degenerate_window`),
  which would otherwise publish either a zero or an arbitrarily large rate;
- the rate producer could not sustain 98% of its target offer.

A cell with no positive median reports its spread as `n/a` rather than `0.0`, so a cell
that measured nothing cannot read as the most stable one in its stage. Flagged cells are
still written to the results file; the report marks them and excludes them from every
derived ratio. Measure on a quiet machine on AC power — ambient load is recorded per
cell precisely because it pollutes.

## When the `absurd-sdk` pin moves

Re-run `uv run python -m benchmarks.sweep --all` and diff `benchmarks/results/`. The
stage filenames are stable (`stage_a.json` … `stage_g.json`) so a throughput regression
shows up as a plain `git diff`. Stages B, C and E calibrate themselves from
`stage_a.json` and stage G from `stage_b.json`, so a partial re-run must include the
stage it depends on, or it errors saying so.

## Layout

| file           | what it is                                                                        |
| -------------- | --------------------------------------------------------------------------------- |
| `compose.yaml` | the `db_bench` Postgres, pinned config, own volume                                |
| `settings.py`  | Django settings; reads `PGDATABASE`/`PGHOST`/`PGPORT_BENCH`/`PGUSER`/`PGPASSWORD` |
| `manage.py`    | for `migrate` and for the worker children                                         |
| `tasks.py`     | the five workloads: two no-ops, two 50 ms sleeps, one 4-step workflow             |
| `host.py`      | host context capture and the suspension guard                                     |
| `runner.py`    | spawns and reaps `absurd_worker` subprocesses                                     |
| `producer.py`  | the enqueue side: preload, paced offer, producer benchmark                        |
| `analysis.py`  | the SQL that turns Absurd's own columns into metrics                              |
| `cells.py`     | one cell: reps, drain detection, median, flags                                    |
| `sweep.py`     | the staged driver (`--stage`, `--all`)                                            |
| `report.py`    | renders committed results as markdown                                             |

Worker children inherit the database the parent is actually using — the runner
serializes `connections["default"].settings_dict` into their environment — which is what
lets the smoke run against a throwaway test database while a standalone sweep runs
against the persistent one.
