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

Eight of the nine stages took about 40 minutes together on the reference machine (14
cores, at `--max-workers 10 --reps 3`), `latency_under_load` about 15 of them.
`size_vs_depth` is the ninth and nothing has timed it there: it drains four tasks for
every one it measures, so budget another quarter of an hour. Name stages to run only
those; `--tasks`, `--duration`, `--reps` and `--max-workers` size them down to a dry
run, `--io-seconds` sets how long `sync_vs_async` pretends to do IO for, and
`--durable-seconds` sets how long `pooled_vs_split`'s durable arms hold a worker thread
(2 s by default — raise it to 30 to measure at an agent tool call's real duration, and
expect that stage to cost roughly fifteen times as much). Results land in
`benchmarks/results/`, which is git-ignored — the numbers belong to the machine that
produced them.

| stage                | what it answers                                                  |
| -------------------- | ---------------------------------------------------------------- |
| `worker_knobs`       | what `--concurrency`, `--batch-size` and async dispatch buy      |
| `process_scaling`    | how throughput scales with worker processes                      |
| `pooled_vs_split`    | one total concurrency, reached two ways, on short and long tasks |
| `size_vs_depth`      | whether a big table or a deep queue is what costs throughput     |
| `poll_interval`      | what `--poll-interval` costs and buys                            |
| `sync_vs_async`      | whether async task bodies beat sync ones                         |
| `checkpoint_cost`    | what a `ctx.step` checkpoint costs                               |
| `producer_ceiling`   | how fast the enqueue side can go                                 |
| `latency_under_load` | end-to-end latency at fractions of a sustainable offer rate      |

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

| variable                | default | change it when                                                                          |
| ----------------------- | ------- | --------------------------------------------------------------------------------------- |
| `BENCH_SHARED_BUFFERS`  | `1GB`   | the Docker VM has under ~4 GB free — try `256MB`                                        |
| `BENCH_MAX_CONNECTIONS` | `100`   | you run many worker processes — each holds 2 backends idle, `--concurrency + 2` working |
| `BENCH_TMPFS_SIZE`      | `4g`    | the VM is small — `512m` covers a single stage                                          |
| `BENCH_CPUS`            | unset   | you want the server pinned to N cores; unset = no limit                                 |
| `BENCH_MEMORY`          | unset   | you want the server's memory capped; unset = no limit                                   |

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

Measured on one 14-core laptop with the data directory in RAM, over four runs of one
commit; every range below is across those runs. Read the directions, not the rates.
[`CLAUDE.md`](CLAUDE.md) names the run behind each figure.

- **Scale with worker processes, keep `--concurrency` around 16, and batch the claims.**
  Neither axis had flattened at the top of the sweep: concurrency 1 -> 16 at one process
  bought 3.0-3.7x, all of it at one queue depth. More processes always bought more, but
  `process_scaling` preloads 2,000 tasks per worker, so its rungs drained 4,000 to
  20,000 tasks and no multiple can be read off that ladder.
- **Processes beat threads at the same total.** 4 x 1 beat 1 x 4 by 1.95-2.28x and 8 x 1
  beat 1 x 8 by 2.17-2.29x, both arms of each pair at the same depth. 40-47% of a task's
  wall time is outside the server (2.82-3.14 ms per task = 1.57-1.77 server + 1.16-1.48
  client); what serialises it — the GIL, or the one claim connection a worker process
  owns — is not established.
- **All of the per-task database cost is acquiring work, not finishing it.** Claiming a
  task costs 13-16x completing one (1.41-1.56 ms against 0.09-0.12 ms), and 18-19% of
  every claim is a scan for cancellations.
- **A queue that is deeper is slower.** Throughput rises as a backlog drains, a fitted
  median +15.7% within a rep over 150 reps, so a saturation number averages a curve and
  does not compare across `--tasks` values. Four times the `--tasks` also costs 40-58%
  of the rate outright — but that moves the table's size and the queue's depth together,
  because a rep preloads what it drains. `size_vs_depth` is the stage that holds depth
  still and moves only size; no figure here comes from a run that included it.
- **A long task holds a Postgres connection of its own the whole time it runs.** A
  worker process opens 2 backends while its slots are idle and one more for every slot
  running a task that touches the database, so it holds up to `--concurrency + 2` — 18
  at the concurrency 16 above, which is five worker processes against a default
  `max_connections` of 100. This is a count, not a rate, so it holds on any machine.

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

## Browsing a corpus in the admin

`seed.py` fills the `bench` queue's tables with millions of rows, so django-absurd's
admin has something to page through. It enqueues a handful of template tasks through the
real enqueue API, drains them with a real `absurd_worker`, and clones the drained rows
server-side. Every command runs from inside `benchmarks/`, against the suites' plain
`db` service — nothing here measures a rate, so the tuned `db_bench` would buy it
nothing.

```
docker compose up -d --wait db
docker compose exec db createdb -U postgres absurd_corpus

export DJANGO_SETTINGS_MODULE=tests.benchmarks.settings
export PGDATABASE=absurd_corpus
uv run python manage.py migrate
uv run python -m seed --rows 1000000
uv run python manage.py createsuperuser
uv run python manage.py runserver --insecure
```

Then open <http://localhost:8000/admin/>. `--rows` is what the queue holds afterwards,
not what the run adds: the tables are emptied first, so seeding again replaces the
corpus. One million tasks and the 1.2 million runs behind them took 20 seconds and 1.1
GB on the reference machine.

`PGPORT` is read here exactly as the test suites read it, so export it too if a system
Postgres already owns 5432. `--insecure` is what serves the admin's CSS with `DEBUG`
off; `DEBUG` stays off because the settings module above is one the whole benchmarks
suite imports, and pytest-django would undo a `DEBUG = True` written into it anyway.

**The corpus is synthetic, and no number taken on it is a property of django-absurd.**
Every task is a copy of one of six templates, so the ages are uniform, the payloads are
identical, and `claimed_by` is spread over eight worker names that never claimed
anything. It answers questions about VOLUME — whether a page loads, which plan the
changelist gets, what an index is worth — and nothing else.

Seeding refuses outright when the queue tables are not the shape it clones — a column it
writes that has gone, or a column the table has grown that it does not write. Cloning
writes those tables directly, so it encodes their columns, and an upstream change has to
fail the seed rather than fill a table it half-understands: a column nothing copies is
real on the drained templates and left at its default on every row taken from them.

## Files

`stages.py`, `measurement.py`, `producer.py`, `runner.py`, `analysis.py` and `report.py`
are the pipeline above, and `seed.py` is the corpus seeder above that. Beside them,
`settings.py` (Django settings: `DATABASE_URL`, else `PGPORT_BENCH` against
`absurd_bench`), `manage.py` (for `migrate` and the worker children), `tasks.py` (the
seven workloads: two no-ops, two sleeps, one 4-step workflow, one long body that reads
and writes rows, and one that always fails), `workload/` (the one-model Django app that
long body works on), `host.py` (host context capture and the suspension guard), and
`pyproject.toml` plus `uv.lock` (the harness's own pinned uv project, django-absurd by
path). [`CLAUDE.md`](CLAUDE.md) holds the reasoning: the measurement model, the
results-file schema, and every number's evidence.

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
