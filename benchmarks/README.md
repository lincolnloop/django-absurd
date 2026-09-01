# `benchmarks/`: the django-absurd load harness

A repeatable measurement rig for three questions: how many tasks a worker topology
actually drains, what latency looks like under a fixed offered rate, and what the knobs
(`--concurrency`, `--batch-size`, `--poll-interval`) buy. It is internal tooling.
Nothing here ships in the `django_absurd` wheel, and it is not a Django app: no models,
no migrations, just a settings module, a task module, and two CLI drivers.

## Architecture

```
                    python -m benchmarks.stages [stage ...]
                                   |
                                   v
   +--------------------------- stages.py ----------------------------+
   | stage definitions, calibration between stages, per-stage         |
   | console output, results/stage_*.json writes                      |
   +---------------------------------|--------------------------------+
                                     v
   +------------------------- measurement.py -------------------------+
   | one measurement = one configuration: run N reps from a clean     |
   | queue, keep the median rep, flag anything untrustworthy          |
   +------------|-------------------------------------|---------------+
                v                                     v
   +----- producer.py ------+           +--------- runner.py --------+
   | the enqueue side:      |           | spawns and reaps real      |
   | preload a backlog, or  |           | `manage.py absurd_worker`  |
   | offer at a paced rate  |           | subprocesses               |
   +------------|-----------+           +--------------|-------------+
                |        enqueue / claim / complete    |
                v                                      v
   +------------------- Postgres (compose: db_bench) ----------------+
   | Absurd's own tables; t_bench.enqueue_at, r_bench.started_at and |
   | r_bench.completed_at carry every timestamp used                 |
   +---------------------------------|--------------------------------+
                                     v
                              analysis.py
              (SQL that turns those columns into metrics)
                                     |
                                     v
                          results/stage_*.json
                                     |
                                     v
                    report.py  ->  markdown tables
```

`host.py` sits beside all of this: it records the machine context per measurement
(cores, load, versions, git SHA) and brackets every measured phase with the suspension
guard. `settings.py`, `manage.py` and `tasks.py` are the minimal Django project the
workers run in.

## What it found

Measured on one 8-core laptop with Postgres in Docker on the same box. **Absolute rates
are a property of that machine.** A tasks/s figure quoted without its host context will
be read as django-absurd's number rather than this laptop's.

| finding                                                  | measured                                                                              |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `--batch-size 1` pays one claim round trip per run       | a concurrency-16 worker drops to 68.1 tasks/s, matching `--concurrency 1` at 66.6     |
| a no-op task is bound by round trips, not by concurrency | 16x the concurrency buys 2.35x the throughput                                         |
| worker processes scale, at falling efficiency            | 1 to 8 workers is 4.2x; per-worker efficiency 1.00 to 0.52                            |
| `poll_interval` sets the latency floor                   | median wait is half the interval, and each idle worker costs `1/poll` claims/s        |
| async and sync tasks perform the same                    | 0.99-1.00x at 50 ms of IO, across concurrency 4/16/32                                 |
| a checkpoint costs about a whole task                    | a 4-step `ctx.step` workflow costs 4.55x a flat one                                   |
| batching the enqueue side raises producer throughput     | `transaction.atomic()` over 500-task chunks reaches 2398 enqueues/s, a plain loop 199 |
| latency climbs steeply just above 75% utilisation        | p50 runs 51, 64, 91 ms at 25/50/75% of capacity, then 1244 ms at 90%                  |

The last row is the one to design against: **keep workers under about 75% of measured
capacity.** Between 75% and 90% the median rises 13.7x and p99 reaches 6.2 s. The 90%
result is flagged for a 57.7% spread across reps, and that instability is the finding
rather than a bad measurement.

The 0.52 efficiency figure earns its own caveat: 8 workers at concurrency 16 is 128
concurrent runs on an 8-core box that is also running Postgres, so the number cannot
separate claim contention from CPU starvation. A run with the database on its own host
would likely look better.

## Reproducing from a clean checkout

```
docker compose -f benchmarks/compose.yaml up -d db_bench
uv sync
uv run python benchmarks/manage.py migrate
uv run pytest tests/benchmarks
uv run python -m benchmarks.stages
uv run python -m benchmarks.report > /tmp/bench-report.md
```

Or run the whole benchmark inside a container instead of on the host:

```
docker compose -f benchmarks/compose.yaml run --rm bench
```

The `bench` service is behind a compose profile, so a plain `up -d` never starts a
75-minute run by accident. It installs the project with uv, migrates, and runs every
stage; results land in `benchmarks/results/` through the bind mount. Numbers measured
inside the container include the container's own overhead, so compare them only with
other container runs.

### Restarting Postgres between stages

How long the database process has been running changes what you measure. Reps at 90% of
capacity read 680-998 ms after an hour of continuous load, and 67-173 ms immediately
after a restart, and only in the restarted case did the workers reach the offered rate
at all. The effect returns within a single stage, so it is drift across a long run, not
a one-off warm-up.

To take that variable out of a comparison, run each stage against a fresh server:

```
docker compose -f benchmarks/compose.yaml restart db_bench
docker compose -f benchmarks/compose.yaml run --rm bench \
  python -m benchmarks.stages process_scaling
```

Two commands per stage, rather than a script that loops them: the driver runs inside a
container with no docker socket, so restarting the database is necessarily the caller's
job. Prerequisites still have to exist when you pick stages by hand — the driver orders
what you name, but it will not run a stage you did not ask for.

This is deliberately not the default. Nobody restarts Postgres between workloads in
production, so a cold run measures a best case rather than a representative one. Both
are legitimate, and every result records `postgres_uptime_s` so a reader can tell which
regime produced a number.

Step 4 is the test suite AND the smoke: `pytest tests/benchmarks` drives every stage end
to end through this driver — real `absurd_worker` subprocesses, real enqueues, real SQL
analysis — at a handful of tasks apiece. It runs against the root compose database like
the other three suites, so `db_bench` need not be up for it. Step 5 took 75 minutes on
the reference host; naming stages runs only those, and `--tasks`, `--duration` and
`--reps` shrink a stage to a dry run.

`db_bench` listens on `${PGPORT_BENCH:-5435}` and keeps a named volume, so it is a
different server from the root `compose.yaml`'s `db` and `db_pg_cron`. Those two do not
need to be running for anything in this directory.

## The stages

Times are from the 8-core reference run and scale with the host.

| stage                | question                                    | calibrates from   | workload                           | ~time  |
| -------------------- | ------------------------------------------- | ----------------- | ---------------------------------- | ------ |
| `worker_knobs`       | what each worker knob buys                  | —                 | 5,000 no-ops, saturation           | 20 min |
| `process_scaling`    | how throughput scales with worker processes | `worker_knobs`    | no-ops, saturation                 | 10 min |
| `poll_interval`      | what `--poll-interval` costs and buys       | `worker_knobs`    | 5 tasks/s offered for 60 s         | 10 min |
| `sync_vs_async`      | whether async tasks beat sync ones          | —                 | sleep (`--io-seconds`), saturation | 15 min |
| `checkpoint_cost`    | what a checkpoint costs                     | `worker_knobs`    | 2,000 tasks, saturation            | 3 min  |
| `producer_ceiling`   | how fast the producer can enqueue           | —                 | 5,000 enqueues, no workers         | 2 min  |
| `latency_under_load` | what latency looks like under load          | `process_scaling` | 60 s paced offer                   | 14 min |

Three stages depend on nothing, so the set is a partial order rather than a sequence —
which is why they carry names instead of letters. Naming several runs them in dependency
order whatever order you type.

**Worker counts scale with the host.** `build_worker_ladder` derives the process_scaling
ladder from `os.cpu_count()`: 1 and 2 anchor the low end where per-worker efficiency is
still readable, then quarter, half, three-quarter and full. Eight cores gives 1, 2, 4,
6, 8; thirty-two gives 1, 2, 8, 16, 24, 32.

**Stage G does not calibrate from the outright ceiling.** A rate measurement's producer
runs on the same machine as its workers. If latency_under_load aimed at the throughput
of a process_scaling result that used every core, the producer would have no CPU left to
actually offer tasks that fast. The measurement would then describe a load that was
never applied and be flagged for under-offering. So it calibrates from the fastest
process_scaling result at or below `RATE_WORKER_CAP` (half the cores), which leaves the
producer room to hit its target.

## The results files

`benchmarks/results/stage_<name>.json` files are written by
`python -m benchmarks.stages`, and by nothing else. The driver rewrites the stage's file
after every finished measurement (atomically, via a temp file), so a run killed at hour
two keeps everything it measured. The directory is git-ignored on purpose: the numbers
are a property of whatever machine produced them, and a committed set would read as
django-absurd's official figures. The test suite never touches these files; it works
against a throwaway test database and temporary directories.

## The measurement model

**Two experiments, and they answer different questions.** A _saturation_ measurement
preloads a backlog, starts the workers, and waits for the queue to drain: it measures
the ceiling, and its latency numbers are meaningless because every task but the first
waited in a queue that was full by construction. A _rate_ measurement starts the workers
first and then offers tasks at a fixed rate for a fixed duration: because arrivals are
paced, queue wait is a real number and end-to-end percentiles mean something. Latency
guidance therefore only ever comes from rate measurements.

**Every timing is a Postgres column.** The harness reads `t_bench.enqueue_at`,
`r_bench.started_at` and `r_bench.completed_at`, all written server-side by Absurd's own
SQL, so producer and workers are timed on one clock with no skew to correct for. The
driver contributes no timestamps: throughput is `0.8 * n / (p90 - p10)` over the
completion times, trimming the ramp and the tail, and fairness is
`count(*) group by claimed_by` over the same rows the throughput count uses.

Redelivery needs no instrumentation either, but it does need the right query. Absurd's
`fail_run` marks the failed run `'failed'` and inserts a **new** row for the retry, and
`complete_run` only ever accepts a run that is still `'running'`. A completed-only count
can therefore never see more than one run per task, and would report every redelivery as
zero. `extra_runs` is instead counted over **every** run row
(`total_runs - total_tasks`), with `max_attempt` recorded beside it to say what kind of
redelivery it was. Throughput keeps its own completed-only count, so failed and pending
rows can never inflate it.

**The suspension guard.** A measurement brackets its measured phase with both
`time.perf_counter()` and `time.time()`. If wall time outruns monotonic time by more
than two seconds the host napped or stalled mid-phase, the rep is thrown away, and the
measurement is flagged. A wall clock stepping _backwards_ is an NTP correction, not a
nap, and is tolerated.

**Flagging is the honesty mechanism, not a failure.** Every rep votes, not just the
median one: a rep that under-offered has a _lower_ latency, so it sorts away from the
median and would never be looked at. A measurement is flagged when:

- its spread across reps exceeds 15% (spread is measured over throughput in saturation
  mode and over end-to-end p50 in rate mode, matching whichever metric that mode ranks
  its reps by);
- any rep was suspended mid-phase;
- any rep saw `extra_runs > 0`, i.e. a redelivery;
- a saturation measurement finished with fewer completed tasks than it enqueued
  (`missing_tasks`): a terminally failed task still satisfies the drain predicate, so
  without this the sample shrinks silently;
- any rep's trimmed completion window was too short to divide by (`degenerate_window`),
  which would otherwise publish either a zero or an arbitrarily large rate;
- the rate producer could not sustain 98% of its target offer.

A measurement with no positive median reports its spread as `n/a` rather than `0.0`, so
one that measured nothing cannot read as the most stable in its stage. Flagged
measurements are still written to the results file; the report marks them and excludes
them from every derived ratio. Measure on a quiet machine on AC power; ambient load is
recorded per measurement precisely because it pollutes.

## Running the tests

```
docker compose up -d db
uv run pytest tests/benchmarks
```

The suite lives at `tests/benchmarks`, beside the other three, and runs against the root
compose database like they do — `db_bench` is for real runs and need not be up. It
enters through this driver's command line at a handful of tasks per stage, writes into a
temporary directory, and so touches neither a results directory you already have nor the
persistent benchmark database.

Nothing in this directory imports the suite, and the suite asserts which measurements
ran, how big each was and whether the harness trusted them — never a rate. A benchmark
number is not something a test can assert without becoming a flake.

## When the `absurd-sdk` pin moves

Copy `benchmarks/results/` aside, re-run `uv run python -m benchmarks.stages`, and diff
the two directories: the stage filenames are stable, so a throughput regression shows up
in a plain `diff -r`. `process_scaling`, `poll_interval` and `checkpoint_cost` calibrate
from `stage_worker_knobs.json` and `latency_under_load` from
`stage_process_scaling.json`, so a partial re-run must include the stage it depends on
(on a fresh checkout that means starting with worker_knobs), or it errors saying so.

## Layout

| file             | what it is                                                                            |
| ---------------- | ------------------------------------------------------------------------------------- |
| `compose.yaml`   | the `db_bench` Postgres (pinned config, own volume) and the `bench` runner            |
| `settings.py`    | Django settings; reads `PGDATABASE`/`PGHOST`/`PGPORT_BENCH`/`PGUSER`/`PGPASSWORD`     |
| `manage.py`      | for `migrate` and for the worker children                                             |
| `tasks.py`       | the five workloads: two no-ops, two sleeps, one 4-step workflow                       |
| `host.py`        | host context capture and the suspension guard                                         |
| `runner.py`      | spawns and reaps `absurd_worker` subprocesses                                         |
| `producer.py`    | the enqueue side: preload, paced offer, producer benchmark                            |
| `analysis.py`    | the SQL that turns Absurd's own columns into metrics                                  |
| `measurement.py` | one measurement: reps, drain detection, median, flags                                 |
| `stages.py`      | runs the stages (positional names, `--tasks`, `--duration`, `--io-seconds`, `--reps`) |
| `report.py`      | renders a results directory as markdown                                               |

Worker children inherit the database the parent is actually using (the runner serializes
`connections["default"].settings_dict` into their environment), which is what lets the
smoke run against a throwaway test database while a standalone benchmark runs against
the persistent one.
