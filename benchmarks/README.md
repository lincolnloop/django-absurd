# `benchmarks/`: the django-absurd load harness

Internal tooling that measures how much work a fleet of `absurd_worker` processes gets
through, what latency looks like under a steady offered rate, and what the worker flags
buy. Nothing here ships in the `django_absurd` wheel.

> **Internal tooling, and mostly written autonomously.** An AI agent was let loose on
> this directory and built the majority of it, much of it never reviewed line by line.
> Weigh its numbers accordingly. This applies to `benchmarks/` alone — not to the
> `django_absurd` package, and not to Absurd itself.

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

The eleven stages take about seventy-five minutes together on the reference machine (14
cores, at `--max-workers 14 --reps 3`). Seven of them were timed at 50 minutes in one
run, of which `latency_under_load` was 15 and `size_vs_depth` 11 — that one drains four
tasks for every one it measures. Name stages to run only those; `--tasks`, `--duration`,
`--reps` and `--max-workers` size them down to a dry run, `--io-seconds` sets how long
`sync_vs_async` pretends to do IO for, and `--durable-seconds` sets how long a durable
body holds a worker thread — in `pooled_vs_split`'s durable arms and in
`durable_checkpoints`, whose long-body arms run at 15x it (default 2 s; 30 s is an agent
tool call's duration and ~15x `pooled_vs_split`'s cost). `durable_checkpoints` is about
ten minutes of the run at that default, nearly all of it its three long-body arms.
Results land in `benchmarks/results/`, which is git-ignored — the numbers belong to the
machine that produced them.

| stage                 | what it answers                                                       |
| --------------------- | --------------------------------------------------------------------- |
| `worker_knobs`        | what `--concurrency`, `--batch-size` and async dispatch buy           |
| `process_scaling`     | how throughput scales with worker processes                           |
| `pooled_vs_split`     | one total concurrency, reached two ways, on short and long tasks      |
| `size_vs_depth`       | whether a big table or a deep queue is what costs throughput          |
| `poll_interval`       | what `--poll-interval` costs and buys                                 |
| `sync_vs_async`       | whether async task bodies beat sync ones                              |
| `checkpoint_cost`     | what a `ctx.step` checkpoint costs                                    |
| `durable_checkpoints` | what a checkpoint costs at depth, inside a body that runs for seconds |
| `cleanup_vs_size`     | what one cleanup call costs, and whether the table sets it            |
| `producer_ceiling`    | how fast the enqueue side can go                                      |
| `latency_under_load`  | end-to-end latency at fractions of a sustainable offer rate           |

Stages run in dependency order whatever order you type them in, but nothing runs a
prerequisite you did not name: `process_scaling`, `poll_interval`, `checkpoint_cost` and
`durable_checkpoints` read back `stage_worker_knobs.json`, and `latency_under_load`
reads `stage_process_scaling.json`. Missing one is an error that says so.

Starting over is `docker compose restart db_bench` and then `migrate` again. Nothing
about the database survives, so nothing about it can go stale.

**`cleanup_vs_size` is the one stage that can fill the server.** It seeds real rows
rather than draining them, and a million tasks is 1.09 GB of tables against a 4 GB tmpfs
— which is why its arms are sized at 250,000 and a million. Seeding more needs
`BENCH_TMPFS_SIZE` raised, and the RAM to back it; without that the seed dies mid-clone
with `could not extend file ... No space left on device`, and the server stays full
until you restart it.

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

| variable                | default | change it when                                                                    |
| ----------------------- | ------- | --------------------------------------------------------------------------------- |
| `BENCH_SHARED_BUFFERS`  | `1GB`   | the Docker VM has under ~4 GB free — try `256MB`                                  |
| `BENCH_MAX_CONNECTIONS` | `100`   | you run many worker processes — `--concurrency + 2` backends each while they work |
| `BENCH_TMPFS_SIZE`      | `4g`    | the VM is small — `512m` covers a single stage                                    |
| `BENCH_CPUS`            | unset   | you want the server pinned to N cores; unset = no limit                           |
| `BENCH_MEMORY`          | unset   | you want the server's memory capped; unset = no limit                             |

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

Then one table per stage, one row per configuration, with the rate, the `backlog` it
preloaded and the spread across repeats. Two rows with different backlogs are two
different experiments — a deeper queue is slower. Every measurement appears even when
something was wrong with it, marked in place:

- `!` **invalid** — a rep measured something other than what was asked (a redelivery, a
  task that never finished, a window too short to divide by, an offer the producer could
  not sustain, a queue still growing when the offer stopped). Re-measure; do not read
  the row.
- `~` **unstable** — the reps measured the right thing and disagreed. That is a finding
  about the system, not a broken measurement.
- `?` — fewer than two valid reps, so the spread was never measured at all.

Five stages run at a configuration an earlier stage picked — `process_scaling`,
`poll_interval`, `checkpoint_cost` and `durable_checkpoints` inherit `worker_knobs`'
winning row, and `latency_under_load` inherits `process_scaling`'s. Each prints a
`Calibrated from` line naming the row it took and ending in that row's standing. **Read
it before the numbers under it, and discard the whole run unless it says
`valid and stable`.** Anything else — `unstable`, `invalid`, `dispersion unmeasured` —
means those stages measured a configuration that did not repeat, and nothing else in the
report says so: the run still exits cleanly and its tables still agree with each other.

Under each saturation table is a commit-budget line saying what limited that row:
`client-bound` is our Python, `connection-bound` is Postgres, `unresolved` means the
calibration could not tell.

`pooled_vs_split` adds a backend table with two columns: `idle` is what its fleet opened
with nothing to do, `working` is what it held with a long task running in every slot.
Size a server's `max_connections` off the second one.

`size_vs_depth` adds a table of what its queue tables held when each drain started —
live rows, dead rows and megabytes. Every one of its arms drained the same amount of
pending work, so the ratios under that table are what a bigger table cost on its own,
and the vacuumed arm says how much of that was dead rows rather than live ones.

## What it found

Measured on one 14-core laptop with the data directory in RAM; every range below is over
the runs that measured it, and a single figure came from one run. Read the directions,
not the rates. [`CLAUDE.md`](CLAUDE.md) names the run behind each figure, and no run has
measured every stage.

- **Scale with worker processes, keep `--concurrency` around 16, and batch the claims.**
  Concurrency 1 -> 16 at one process bought 3.0-4.0x and had not flattened, all of it at
  one queue depth. More processes bought more up to 10; the 14-process rung read below
  it at a deeper backlog, and `process_scaling` preloads 2,000 tasks per worker — 4,000
  to 28,000 tasks across the ladder — so it cannot separate a knee from depth.
- **Processes beat threads at the same total.** 8 x 1 beat 1 x 8 by 2.31x, both arms of
  the pair at the same depth; 4 x 1 beat 1 x 4 by 2.35x, where the split arm is the one
  measurement of the four the harness marked unstable, so quote the 8-way number. On the
  long, database-touching workload the two shapes tie by construction, so that pair says
  nothing about either. 40-47% of a task's wall time is outside the server (2.82-3.55 ms
  per task = 1.57-2.09 server + 1.16-1.48 client); what serialises it — the GIL, or the
  one claim connection a worker process owns — is not established.
- **All of the per-task database cost is acquiring work, not finishing it.** Claiming a
  task costs 13-20x completing one (1.41-1.92 ms against 0.09-0.12 ms), and 14-19% of
  every claim is a scan for cancellations.
- **A `ctx.step` checkpoint costs about 0.6 ms and two commits.** A four-step workflow
  drained at 4.25x the cost of an empty task (305 against 1,297 tasks/s, one run). That
  multiple is what a task with nothing in its body pays; against an agent tool call that
  runs for seconds a step is under a thousandth of the work it checkpoints, so choose
  step boundaries for the restarts you want rather than for throughput.
- **A bigger table is slower, and that is most of why a deeper queue is.** Throughput
  rises as a backlog drains, a fitted median +15.7% within a rep over 150 reps, so a
  saturation number averages a curve and does not compare across `--tasks` values. Four
  times the `--tasks` costs 40-58% of the rate outright, and `size_vs_depth` — which
  holds the pending depth still and moves only the rows behind it — puts 1.87x of that
  on table size alone. Vacuuming the extra rows' dead versions bought 1.2%, so what
  costs the throughput is the live rows: retention is a throughput feature here, not
  housekeeping.
- **A long task holds a Postgres connection of its own the whole time it runs**, so a
  worker process holds up to `--concurrency + 2` backends — 18 at the concurrency above,
  which is five processes against a default `max_connections` of 100. A count, not a
  rate, so it holds on any machine.

## Caveats that change what you can do with a number

**Absolute rates are not publishable, at all** — not across runs, and not as a property
of django-absurd. `db_bench` keeps its data directory in RAM, and no production Postgres
runs that way. A row read against another row in the same file is a comparison; the same
row quoted on its own is a number about RAM. Publishable figures need real storage,
which nothing in this directory can provide.

**Every ceiling this work proposed turned out to be the measurement environment rather
than Absurd** — disk fsync first, then a single connection's commit rate, then CPU. The
harness has not found Absurd's limit; it has only ever found its own.

**Repeats are good enough to rank things, not to confirm a small change.** Three runs of
the same commit, 25 shared measurements: median CV 4.7%, mean 5.1%, worst 12.5%, one run
systematically below the other two. Under about 12% is not evidence of anything, and a
whole run reading low is usually the working point it inherited, not the machine.

**Most of what is above was measured on tasks that finish in microseconds.** Only
`pooled_vs_split` also runs the long, database-touching workload django-absurd is
primarily for; every other stage uses an empty task body, and its advice is advice about
that regime. `--durable-seconds` is how you take the long arms to a realistic duration,
and [`CLAUDE.md`](CLAUDE.md) says which findings carry over.

**Measure on a quiet machine on AC power.** The macOS indexer alone was worth 1-1.4
cores sustained. It spoils the saturation stages, which drive the box to its limit, and
barely reaches the paced ones.

**`latency_under_load` measures the offer rate it then uses, and stops at the lower of
two limits.** Its rungs are fractions of whatever rate its own ramp got through cleanly,
and a probe fails when the fleet falls behind OR when the producer — on the same box —
never delivers the offer. Read the `Offer rate:` line and the ramp's `producer kept up`
column first; two runs' rows only compare if their ramps agreed.

## Filling the admin with millions of rows

`seed.py` fills the `bench` queue's tables so django-absurd's admin has something to
page through. It enqueues a handful of template tasks through the real enqueue API,
drains them with a real `absurd_worker`, and clones the drained rows server-side. Every
command runs on the harness's own settings.

```
benchmarks/serve_admin.sh            # a million tasks
benchmarks/serve_admin.sh 50000      # fewer, for a quicker loop
```

Start the `db` service first, from the repo root (`docker compose up -d db`), as the
suites do. The script then makes itself a database on it, migrates, seeds, and serves
<http://localhost:8000/admin/>. Log in as `admin`/`admin`. Re-running is fine: the
argument is what the queue holds afterwards, not what the run adds, since the tables are
emptied first and the six templates every clone is copied from are the floor. One
million tasks and the 1.2 million runs behind them took 23 seconds and 1.1 GB on the
reference machine.

`PGPORT` picks the server and `SAMPLE_DATABASE` the database on it. It is a database of
its own because the harness's default is `db_bench`'s, which a real run empties. The
script also sets `DEBUG=1`, which is what serves the admin's own CSS. `python -m stages`
refuses to run while it is on: the children inherit the whole environment, so a shell
that exported it once would measure every rate through the debug cursor.

**The data is synthetic, and no number taken on it is a property of django-absurd.**
Every task is a copy of one of six templates, so the ages are uniform, the payloads are
identical, and `claimed_by` is spread over eight worker names that never claimed
anything. It answers questions about VOLUME — whether a page loads, which plan the
changelist gets, what an index is worth — and nothing else.

Seeding refuses, before it empties anything, when the queue tables' columns are not the
ones it clones — in either direction. Cloning writes those tables directly, so an
upstream change has to fail the seed rather than fill a table it half-understands.

## Files

`stages.py`, `measurement.py`, `producer.py`, `runner.py`, `analysis.py` and `report.py`
are the pipeline above, `seed.py` fills the tables for the admin and `serve_admin.sh`
serves it on them. Beside them, `settings.py` (Django settings: `DATABASE_URL`, else
`PGPORT_BENCH` against `absurd_bench`, plus `DEBUG` and the admin stack), `urls.py`
(which mounts the admin), `manage.py` (for `migrate` and the worker children),
`tasks.py` (the seven workloads: two no-ops, two sleeps, one 4-step workflow, one long
body that reads and writes rows, and one that always fails), `workload/` (the one-model
Django app that long body works on), `host.py` (host context capture and the suspension
guard), and `pyproject.toml` plus `uv.lock` (the harness's own pinned uv project,
django-absurd by path). [`CLAUDE.md`](CLAUDE.md) holds the reasoning: the measurement
model, the results-file schema, and every number's evidence.

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
