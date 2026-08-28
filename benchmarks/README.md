# `benchmarks/` — the django-absurd load harness

A re-runnable measurement rig for three questions: how many tasks a worker topology
actually drains, what latency looks like under a fixed offered rate, and what the knobs
(`--concurrency`, `--batch-size`, `--poll-interval`) buy. It is internal tooling —
nothing here ships in the `django_absurd` wheel, and it is not a Django app: no models,
no migrations, just a settings module, a task module, and two CLI drivers.

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
`test_absurd_bench`, so it is safe to run while committed results exist. Step 5 takes
roughly two hours; `--stage A` (repeatable, `A`–`G`) runs one stage, and `--reps 1`
turns any stage into a fast dry run.

`db_bench` listens on `${PGPORT_BENCH:-5435}` and keeps a named volume, so it is a
different server from the root `compose.yaml`'s `db` and `db_pg_cron`. Those two do not
need to be running for anything in this directory.

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
