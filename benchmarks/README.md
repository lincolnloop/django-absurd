# `benchmarks/`: the django-absurd load harness

Internal tooling that measures how much work a fleet of `absurd_worker` processes gets
through, what latency looks like under a steady offered rate, and what the worker flags
buy. Nothing here ships in the `django_absurd` wheel.

The pipeline is `stages.py` (what to measure) -> `measurement.py` (one configuration,
repeated) -> `producer.py` (enqueues) and `runner.py` (spawns real workers) -> Postgres
-> `analysis.py` (SQL that turns Absurd's own timestamp columns into metrics) ->
`results/stage_*.json` -> `report.py` (markdown). This directory is the import root, not
a package, so every command below runs from inside `benchmarks/`.

## Run it

```
docker compose up -d --wait db_bench
uv run python manage.py migrate
uv run python -m stages
uv run python -m report > "results/report-$(date -u +%Y%m%dT%H%M%SZ).md"
```

**Run `migrate` after every start or restart of `db_bench`.** Its data directory is in
RAM, so a restart hands you back an empty server, and a run against one dies partway
through its first measurement with `schema "absurd" does not exist`. It takes a second
and it is idempotent, so just run it every time.

All eight stages took about 40 minutes on the reference machine (14 cores, at
`--max-workers 10 --reps 3`), `latency_under_load` about 15 of them. Name stages to run
only those; `--tasks`, `--duration`, `--reps` and `--max-workers` size them down to a
dry run, and `--io-seconds` sets how long `sync_vs_async` pretends to do IO for. Results
land in `benchmarks/results/`, which is git-ignored — the numbers belong to the machine
that produced them.

| stage                | what it answers                                             |
| -------------------- | ----------------------------------------------------------- |
| `worker_knobs`       | what `--concurrency`, `--batch-size` and async dispatch buy |
| `process_scaling`    | how throughput scales with worker processes                 |
| `pooled_vs_split`    | one total concurrency, reached two ways                     |
| `poll_interval`      | what `--poll-interval` costs and buys                       |
| `sync_vs_async`      | whether async task bodies beat sync ones                    |
| `checkpoint_cost`    | what a `ctx.step` checkpoint costs                          |
| `producer_ceiling`   | how fast the enqueue side can go                            |
| `latency_under_load` | end-to-end latency at fractions of a sustainable offer rate |

Stages run in dependency order whatever order you type them in, but nothing runs a
prerequisite you did not name: `process_scaling`, `poll_interval` and `checkpoint_cost`
read back `stage_worker_knobs.json`, and `latency_under_load` reads
`stage_process_scaling.json`. Missing one is an error that says so.

Starting over is `docker compose restart db_bench` and then `migrate` again. Nothing
about the database survives, so nothing about it can go stale.

## The server

Only Postgres is containerised; driver, producer and workers all run on the host.
`db_bench` lives in the repo root's `compose.yaml` behind a `bench` profile, so a bare
`docker compose up -d` starts the test suites' databases and not a benchmark server
nobody asked for. It publishes `${PGPORT_BENCH:-5460}` and holds its own database,
`absurd_bench`, and `settings.py` reads the same variable, so a run cannot quietly land
on a suite's server and measure an untuned one; `DATABASE_URL` overrides the whole
address. `uv run` uses this directory's `pyproject.toml` and `uv.lock`, with
django-absurd editable from the checkout above, so a run measures this branch.

The defaults need about 5 GB free in the Docker VM (1 GB of shared buffers plus a 4 GB
tmpfs). Set any of these in the shell that starts `db_bench`, then restart it and re-run
`migrate`:

| variable                | default | change it when                                             |
| ----------------------- | ------- | ---------------------------------------------------------- |
| `BENCH_SHARED_BUFFERS`  | `1GB`   | the Docker VM has under ~4 GB free — try `256MB`           |
| `BENCH_MAX_CONNECTIONS` | `100`   | you run over ~40 worker processes (2 server backends each) |
| `BENCH_TMPFS_SIZE`      | `4g`    | the VM is small — `512m` covers a single stage             |
| `BENCH_CPUS`            | unset   | you want the server pinned to N cores; unset = no limit    |
| `BENCH_MEMORY`          | unset   | you want the server's memory capped; unset = no limit      |

Too small a tmpfs does not fail at startup: the run dies partway through on a Postgres
write error, so raise it if a long run aborts. Every value above is recorded in the
results file, because runs taken at different values are not comparable.
[`CLAUDE.md`](CLAUDE.md) has the sizing evidence and why nothing else here is a
variable.

## Reading the report

Read the header first. It names the machine, the flags the run used, and the server's
`cluster_name` — `bench-tmpfs` means the data directory was RAM, and the header then
says outright that rates off that server are only for comparing configurations. Beside
it is the commit ceiling: what a single connection to this server could commit per
second, measured before the first stage and again after the last.

Then one table per stage, one row per configuration, with the rate and the spread across
repeats. Every measurement appears even when something was wrong with it, marked in
place:

- `!` **invalid** — a rep measured something other than what was asked (a redelivery, a
  task that never finished, a window too short to divide by, an offer the producer could
  not sustain, a queue still growing when the offer stopped). Re-measure; do not read
  the row.
- `~` **unstable** — the reps measured the right thing and disagreed. That is a finding
  about the system, not a broken measurement.
- `?` — fewer than two valid reps, so the spread was never measured at all.

Under each saturation table is a commit-budget line saying what limited that row:
`client-bound` is our Python, `connection-bound` is Postgres, `unresolved` means the
calibration could not tell.

## What it found

Measured on one 14-core laptop with the server's data directory in RAM. Read the ratios,
not the rates.

- **Scale with worker processes, keep `--concurrency` around 16, and batch the claims.**
  The best cell measured was 10 processes x 16 concurrency at 4,441 tasks/s. Neither
  axis had flattened out there: concurrency 1 -> 16 at one process bought 3.4x (360.6 ->
  1,231.1 tasks/s), and processes 1 -> 10 at concurrency 16 bought 3.7x (1,211.5 ->
  4,441.7).
- **Processes beat threads at the same total.** 4 processes x 1 beat 1 x 4 concurrency
  by 2.08x, and 8 x 1 beat 1 x 8 by 2.24x. The reason is that 42% of a task's wall time
  is client-side Python (2.82 ms per task = 1.64 ms server + 1.18 ms client), which is
  what a GIL serialises.
- **All of the per-task database cost is acquiring work, not finishing it.** Claiming a
  task costs about 15x completing one (1.47 ms against 0.099 ms), and 18% of every claim
  is a scan for cancellations.
- **A queue that is deeper is slower.** Throughput rises as a backlog drains — within a
  measurement, by a median 13.3%, in 37 of 46 repeats. So a saturation number averages a
  curve and cannot be compared across different `--tasks` values.

## Caveats that change what you can do with a number

**Absolute rates are not publishable, at all** — not across runs, and not as a property
of django-absurd. `db_bench` keeps its data directory in RAM, so every rate here was
measured against a server with no disk under it, and no production Postgres runs that
way. A row read against another row in the same file is a comparison; the same row
quoted on its own is a number about RAM. Publishable figures need real storage under a
real filesystem, which nothing in this directory can provide.

**Every ceiling this work proposed turned out to be the measurement environment rather
than Absurd** — disk fsync first, then a single connection's commit rate, then CPU. The
harness has not found Absurd's limit; it has only ever found its own. Read every figure
above with that in mind.

**Repeats are good enough to rank things, not to confirm a small change.** Three runs of
the same commit, 25 shared measurements: median CV 4.7%, mean 5.1%, worst 12.5%. The
third run also sat systematically below the other two on several measurements, so part
of that is a between-run bias rather than scatter. A difference smaller than about 12%
is not evidence of anything.

**Measure on a quiet machine on AC power.** The macOS indexer alone was worth 1-1.4
cores sustained and moved measurements 6-10%. It ruins the saturation stages, which
drive the box to its limit, and barely reaches the paced ones — so a run on a machine
you are also using is worth reading for its rate stages and worth distrusting for the
rest.

**`latency_under_load` measures the offer rate it then uses.** Its rungs are fractions
of whatever rate its own ramp found the fleet could absorb, so read the `Offer rate:`
line under its table first — two runs' rows only compare if their ramps agreed.

## Files

`stages.py`, `measurement.py`, `producer.py`, `runner.py`, `analysis.py` and `report.py`
are the pipeline above. Beside them, `settings.py` (Django settings: `DATABASE_URL`,
else `PGPORT_BENCH` against `absurd_bench`), `manage.py` (for `migrate` and the worker
children), `tasks.py` (the five workloads: two no-ops, two sleeps, one 4-step workflow),
`host.py` (host context capture and the suspension guard), and `pyproject.toml` plus
`uv.lock` (the harness's own pinned uv project, django-absurd by path).
[`CLAUDE.md`](CLAUDE.md) holds the reasoning: the measurement model, the results-file
schema, and every number's evidence.

## Running the tests

From the repo root, not from here:

```
docker compose up -d db
uv run pytest tests/benchmarks
```

The suite runs against the plain `db` service like the other three suites do, so
`db_bench` need not be up. It drives this directory's command line at a handful of tasks
per stage and writes into a temporary directory, touching neither your results directory
nor the benchmark database. It asserts which measurements ran and whether the harness
trusted them, never a rate — a benchmark number cannot be asserted without becoming a
flake.
