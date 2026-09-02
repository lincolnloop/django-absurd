# `benchmarks/`: the django-absurd load harness

A repeatable measurement rig for three questions: how many tasks a worker topology
actually drains, what latency looks like under a fixed offered rate, and what the knobs
(`--concurrency`, `--batch-size`, `--poll-interval`) buy. It is internal tooling.
Nothing here ships in the `django_absurd` wheel, and it is not a Django app: no models,
no migrations, just a settings module, a task module, and two CLI drivers. It is not a
package either — no `__init__.py`, so this directory IS the import root and its modules
are top-level (`import stages`, `import host`). Every command below therefore runs from
inside `benchmarks/`, except the one that starts the database: its compose file is the
repo root's.

## Architecture

```
                        python -m stages [stage ...]
                                   |
                                   v
   +--------------------------- stages.py ----------------------------+
   | stage definitions, calibration between stages, per-stage         |
   | console output, results/stage_*.json writes                      |
   +---------------------------------|--------------------------------+
                                     v
   +------------------------- measurement.py -------------------------+
   | one measurement = one configuration: run N reps from a clean     |
   | queue, keep the median rep, mark anything untrustworthy         |
   +------------|-------------------------------------|---------------+
                v                                     v
   +----- producer.py ------+           +--------- runner.py --------+
   | the enqueue side:      |           | spawns and reaps real      |
   | preload a backlog, or  |           | `manage.py absurd_worker`  |
   | offer at a paced rate  |           | subprocesses               |
   +------------|-----------+           +--------------|-------------+
                |        enqueue / claim / complete    |
                v                                      v
   +------- Postgres (compose: db_bench, on a published port) --------+
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
(cores, versions, git SHA), samples the load average around every rep, and brackets
every measured phase with the suspension guard. The flags a run was given and the
machine's commit ceiling are recorded once per results file, beside those measurements —
see [The results files](#the-results-files). `settings.py`, `manage.py` and `tasks.py`
are the minimal Django project the workers run in.

## What it found

**Every row below predates this harness and nothing in the repo backs it.** They were
measured on one 8-core laptop with Postgres in Docker on the same box, by an earlier
driver whose stages, sizes and metrics are not the ones here — so no code in this
directory reproduces them. Results are git-ignored, so no evidence for a row survives
anywhere. Read them as the shape of what each stage measures, not as numbers to quote,
and re-measure on your own host before you rely on one.

| finding                                                  | measured                                                                          |
| -------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `--batch-size 1` pays one claim round trip per run       | a concurrency-16 worker drops to 68.1 tasks/s, matching `--concurrency 1` at 66.6 |
| a no-op task is bound by round trips, not by concurrency | 16x the concurrency buys 2.35x the throughput                                     |
| worker processes scale, at falling efficiency            | 1 to 8 workers is 4.2x; per-worker efficiency 1.00 to 0.52                        |
| `poll_interval` sets the latency floor                   | median wait is half the interval, and each idle worker costs `1/poll` claims/s    |
| async and sync tasks perform the same                    | 0.99-1.00x at 50 ms of IO, across concurrency 4/16/32                             |
| a checkpoint costs about a whole task                    | a 4-step `ctx.step` workflow costs 4.55x a flat one                               |
| batching the enqueue side raises producer throughput     | `transaction.atomic()` over 500-task chunks reaches 2398 enqueues/s               |
| latency climbs steeply just above 75% utilisation        | p50 runs 51, 64, 91 ms at 25/50/75% of capacity, then 1244 ms at 90%              |

The last row is the one to design against: **keep workers under about 75% of measured
capacity.** Between 75% and 90% the median rises 13.7x and p99 reaches 6.2 s. The 90%
result is flagged for a 57.7% spread across reps, and that instability is the finding
rather than a bad measurement.

The 0.52 efficiency figure earns its own caveat: 8 workers at concurrency 16 is 128
concurrent runs on an 8-core box that is also running Postgres, so the number cannot
separate claim contention from CPU starvation. A run with the database on its own host
would likely look better.

## Reproducing from a clean checkout

First the database, which is the one step whose home is the repo root:

```
docker compose up -d --wait db_bench
```

Then everything else, from inside `benchmarks/`:

```
uv run python manage.py migrate
uv run python -m stages
uv run python -m report > "results/report-$(date -u +%Y%m%dT%H%M%SZ).md"
```

`db_bench` is a service in the root `compose.yaml`, behind a `bench` profile: a bare
`docker compose up -d` starts the suites' databases and not a benchmark server nobody
asked for, while naming the service starts it as above. Compose searches parent
directories for its file, so that first command also works from inside `benchmarks/` —
it reaches the root file either way.

Only Postgres is containerised. `db_bench` publishes `${PGPORT_BENCH:-5460}` and holds
its own database, `absurd_bench`; `settings.py` reads the same variable, so one value
moves both sides and a run cannot quietly land on a suite's server (5432/5434) and
measure an untuned one. `DATABASE_URL` overrides the whole address.

`uv run` resolves `pyproject.toml` and `uv.lock` here, so the dependency set is the
pinned one and the environment lands in `benchmarks/.venv`. django-absurd comes from the
checkout above, editable, so the harness measures this branch. Compose names its project
after the directory holding the compose file, so `bench_pgdata` belongs to this
checkout: another clone or worktree builds its own rather than wandering into this one,
and a volume left behind by an older layout is not reached at all. To start the database
over — an empty one, or one whose contents predate a change to this service —
`docker compose down -v db_bench` removes that service and its volume without touching
`db` or `db_pg_cron`, and the next `up` initialises `absurd_bench` again. The results
files are what you keep, never the database.

With no stage named, `python -m stages` runs all seven, which took 75 minutes on the
reference host; naming stages runs only those, and `--tasks`, `--duration`, `--reps` and
`--max-workers` shrink a stage to a dry run. Results land in `benchmarks/results/`. The
test suite is separate and lives at the repo root — see
[Running the tests](#running-the-tests).

**One measurement regime, and it is a host one.** Driver, producer and workers all run
on the host; only Postgres is in a container, reached over loopback. Half of that regime
is still pinned hard — `db_bench` is an exact image tag with a fixed server config on a
port nothing else uses, and `uv.lock` plus `requires-python` fix the Python and every
dependency. The other half is now whichever machine you are on, which is why `host.py`
stamps cores, load average and versions onto every measurement and every results file
records the flags it was run at: numbers are comparable with numbers measured the same
way, and a run against Postgres somewhere else, or at another size, is not.

`db_bench` sits in the root `compose.yaml` beside `db` and `db_pg_cron` and is still a
different server: its own volume, its own pinned config, its own port, and a profile
that keeps it out of a bare `up`. The suites hammer `db` with real worker subprocesses,
so a benchmark sharing it would absorb whichever tests were running. Neither of those
two needs to be up for a benchmark run.

### Restarting Postgres between stages

How long the database process has been running changes what you measure. Reps at 90% of
capacity read 680-998 ms after an hour of continuous load, and 67-173 ms immediately
after a restart, and only in the restarted case did the workers reach the offered rate
at all. The effect returns within a single stage, so it is drift across a long run, not
a one-off warm-up.

To take that variable out of a comparison, run each stage against a fresh server:

```
docker compose restart db_bench
uv run python -m stages process_scaling
```

Both commands run on the host, in one shell and from this directory, so a loop over
stages is a `for` loop. Prerequisites still have to exist when you pick stages by hand —
the driver orders what you name, but it will not run a stage you did not ask for.

This is deliberately not the default. Nobody restarts Postgres between workloads in
production, so a cold run measures a best case rather than a representative one. Both
are legitimate, and every result records `postgres_uptime_s` so a reader can tell which
regime produced a number.

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

**`--max-workers` is the size flag for topology.** `--tasks`, `--duration` and
`--io-seconds` size the work; nothing sized the fleet, and the fleet tracked the host.
Thirty-two cores means 1 + 2 + 8 + 16 + 24 + 32 = 83 worker processes spawned across
process_scaling alone, and 128 cores means 323. `--max-workers N` lowers the ceiling the
ladder is derived from, so a bounded ladder is still a ladder rather than the same rung
repeated: `--max-workers 3` gives 1, 2, 3. The same bound caps the poll_interval idle
probes (four per interval otherwise) and the fleet latency_under_load calibrates from —
it narrows which process_scaling rung latency_under_load may pick, so the offered rate
stays the throughput that fleet actually measured. Unset, everything behaves exactly as
above.

Bound both stages together. `--max-workers` on latency_under_load alone reads back a
`stage_process_scaling.json` measured on a larger fleet, and the rungs it is allowed to
pick from are whatever that unbounded run recorded.

A size below what a stage can measure is refused before anything runs, rather than
crashing partway or writing a number describing work that never happened: fewer than one
worker, fewer than one task, or a rate window of no length. `--io-seconds 0` is the
exception and stays legal — no simulated IO is a real point on that experiment's axis.

**`latency_under_load` does not calibrate from the outright ceiling.** A rate
measurement's producer runs on the same machine as its workers. If latency_under_load
aimed at the throughput of a process_scaling result that used every core, the producer
would have no CPU left to actually offer tasks that fast. The measurement would then
describe a load that was never applied and be marked invalid for under-offering. So it
calibrates from the fastest process_scaling result at or below `RATE_WORKER_CAP` (half
the cores), which leaves the producer room to hit its target.

## The results files

`benchmarks/results/stage_<name>.json` files are written by `python -m stages`, and by
nothing else. A rendered `report-<UTC stamp>.md` lands beside them so a run and its
reading stay together, stamped because a second run would otherwise overwrite the first
reading while its own JSON sat right there. The driver rewrites the stage's file after
every finished measurement (atomically, via a temp file), so a run killed at hour two
keeps everything it measured. The directory is git-ignored on purpose: the numbers are a
property of whatever machine produced them, and a committed set would read as
django-absurd's official figures. The test suite never touches these files; it works
against a throwaway test database and temporary directories.

**Each file says which configuration produced it.** Beside `measurements` sits an
`options` block holding `--tasks`, `--duration`, `--io-seconds`, `--max-workers` and
`--reps` **resolved** — an unset flag records what the run actually used, not a null to
go look up. `--max-workers` is the one nothing else recovers: unset it tracks the host,
so an unbounded ten-core run and a fourteen-core run bounded to ten write the same
worker ladder. `--tasks` and `--duration` stay `null`, meaning the stage sized itself —
neither has one default to name (5,000 no-ops here, 2,000 workflows there; a 60 s offer,
a 30 s idle probe), and every measurement's `spec` carries the size it ran at. The whole
set prints on one line of the report header, and a flag two stage files disagree about
reads as `mixed (8, 60)`, the way a mixed git SHA does — running one stage again at
another size is a documented workflow. A file written before this block existed has
none, and the report will not render it; re-measure rather than hand-adding one. The
same goes for one written before a measurement carried `invalid`, `unstable`, `cv` and
its rep endpoints: it records a single `flagged` the report no longer reads.

**Each file also says what the machine could commit.** Beside `options` sit
`commit_ceiling_durable_per_s`, `commit_ceiling_nondurable_per_s` and
`commit_ceiling_durable_after_per_s`: commits per second this server sustained in one
session, measured once before the first measurement of the run and again after the last
one. The closing probe is not the opening one repeated — measured straight after a full
run the durable ceiling read 719/s against 1,903/s on the same idle machine, so a run's
own numbers mean different things at its start and at its end. The probe loops
server-side, a `do` block committing single-row inserts timed with `clock_timestamp()`,
because a client loop would measure its own round trips as well as the disk's fsync; it
writes to a real table it creates and drops rather than a temporary one, since a temp
table is not WAL-logged and a probe against one reports memory speed as the durable
ceiling (84,000/s against 953/s, measured side by side). Any of the three can be `null`:
the probe was refused, the run went ahead regardless, and the report says the
calibration is missing rather than dropping the line — an uncalibrated number that says
so beats no run at all.

**A stage calibrated from another names what it inherited**, in a `calibration` block
holding the stage and measurement that became the working point, its throughput, its CV
and whether it was marked. The choice prefers a rung that measured what was asked and
repeated, so when most rungs are marked it lands on the slowest survivor and every
number in the later stage was measured at it — which the report prints under that
stage's heading.

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

**Throughput is profiled within a rep, not only across reps.** A saturation rep drains a
full queue to empty, so its single `throughput_per_s` averages whatever the per-task
cost did as depth fell. Every saturation rep therefore also carries `profile_slices` —
throughput over successive slices of 200 completions, slice 0 the fullest queue and the
last one the emptiest — with `profile_median_per_s` and `profile_cv` beside it. The
slices are equal-COUNT rather than equal-time because a slower measurement puts fewer
completions into a fixed time slice, so its slices would read as noisier for purely
statistical reasons and would not compare across settings. The partial slice a drain
ends on is dropped, and a rep with fewer than three full slices records `null` rather
than a shape read off a handful of rows. Rate measurements carry no profile: their offer
rate is imposed rather than discovered, so slicing it would plot the producer's pacing
back at the reader.

**A throughput is a commit rate in disguise, and every saturation rep records the
exchange rate.** `commits_per_task` is the database's own `xact_commit` delta across the
measured phase over the runs that completed inside it, so it covers the claim, the
completion and the driver's drain polling, and excludes the preload, which is over
before the phase opens. Throughput times that is the commit rate the run asked of the
disk, and the report prints it against the ceiling under each saturation table: a
measurement near the ceiling is fsync-bound and its number is a property of the disk
rather than of Absurd, while one at a fraction of the ceiling is bound by its client and
is the kind that is about Absurd. Measured on one laptop with Postgres in Docker, 71% of
active backend time in a full run was WAL durability, the durable ceiling was 1,903
commits/s and a task cost 3.55 commits — 1903/3.55 is 536 tasks/s against 548 observed
at the top of the concurrency ladder, which is the ladder climbing from client-bound to
fsync-bound rather than Absurd scaling. Rate reps carry no `commits_per_task`: their
completed-run count comes off the trimmed middle of the offer window while the phase
spans the whole offer and drain, so the quotient would divide two different windows into
each other.

Reading a profile: within a rep, remaining depth falls while accumulated database state
only grows, so the two point opposite ways. Throughput RISING across the slices means
depth drives the per-task cost, because nothing cumulative can make a drain faster; flat
within a rep while reps disagree rules depth out and points at cumulative state or a
per-run latch; a sawtooth is contention, and neither.

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
measurement is marked invalid. A wall clock stepping _backwards_ is an NTP correction,
not a nap, and is tolerated.

**Marking is the honesty mechanism, and nothing is suppressed.** Every rep votes, not
just the median one: a rep that under-offered has a _lower_ latency, so it sorts away
from the median and would never be looked at. Two unrelated things can be wrong with a
measurement, so two booleans record them — they mean different things and they call for
different responses:

- **`invalid`** — a rep measured something OTHER than what was asked. Any rep was
  suspended mid-phase; any rep saw `extra_runs > 0`, i.e. a redelivery; a saturation
  measurement finished with fewer completed tasks than it enqueued (`missing_tasks`),
  since a terminally failed task still satisfies the drain predicate and the sample
  would shrink silently; a rep's trimmed completion window was too short to divide by
  (`degenerate_window`), which would otherwise publish either a zero or an arbitrarily
  large rate; or the rate producer could not sustain 98% of its target offer. A hard
  error — re-measure.
- **`unstable`** — the reps measured the right thing and disagreed about the answer.
  That is a finding about the system, and often the most interesting one in the run.

**Instability is thresholded on the CV; the range is what gets printed.** `cv` is the
sample standard deviation over the mean of whichever metric the reps were ranked on —
throughput in saturation mode, end-to-end p50 in rate mode, enqueues/s for the producer
— and the limit is 10%. `spread`, `(max - min) / median`, is a RANGE, and a range grows
with the rep count on unchanged data: 5.3% at 3 reps and 14.1% at 50 on a fixed process,
taking the flag rate from 0.2% to 30.3%. `--reps` is a live flag, so thresholding on it
meant that asking for more repeats made good data look worse. A CV is flat over that
same sweep (2.8% to 3.1%) and separates the real measurements cleanly: settings that
repeat sit at 1.0-3.4%, ones that genuinely lurch at 25-50%, and 10% is the gap between
them. The spread and the endpoints (`range_low`, `range_high`) are all still recorded
and the report prints the endpoints, because `209-501` is what a reader wants to see —
they have simply stopped being the trigger. A `spread_floor` guards the whole test in
the ranking key's own units: reps within 150 ms of each other are not called unstable
however far apart they read relatively.

Fewer than two valid reps is an unknown dispersion rather than a zero: `spread` and `cv`
record `null` and the row is marked `?`, so a measurement that measured nothing cannot
read as the most stable in its stage. `--reps 1` is a dry run for that reason.

**Every measurement appears in every table and in every number derived under one**,
marked `!` (invalid), `~` (unstable) or `?` (dispersion unmeasured), with a legend in
the report header. Nothing is dropped: a measurement struck from a ratio table is a
finding deleted, and the reproducible instability is exactly the finding worth keeping.
A derived line built on measurements that disagreed carries the disagreement instead of
hiding it — a ratio or a scaling efficiency divides the endpoints too and prints the
interval beside the point estimate, `1.94x (reps 1.49-2.40x)`. The one thing a mark
still decides is CALIBRATION: `pick_best_measurement` prefers a valid, stable rung when
it picks the working point later stages inherit, falling back to the whole set rather
than refusing. That is choosing a configuration to measure at, not editing a report.

**Load is sampled on each side of every rep.** `load_before` and `load_after` sit on the
rep itself, because the per-measurement `host` block is collected once all its reps are
over: the 1-minute average in it counts the load the harness had just made, and as
recorded there it could never answer whether the machine was otherwise busy. The host
block keeps its versions, core count, SHA and its own `load_avg_1m`.

Measure on a quiet machine on AC power; ambient load is recorded around every rep
precisely because it pollutes.

**It does not pollute every stage equally.** A rate stage offers a few tasks a second
and never approaches the machine's ceiling, so whatever else the box is doing barely
reaches it. A saturation stage drives the machine to its limit, which is exactly where
sharing cores with anything else stops being ignorable. On a busy workstation the paced
stages came back unmarked while nearly every saturation measurement did not — so a run
on a machine you are also using is worth reading for its rate stages and worth
distrusting for the rest.

## Running the tests

From the repo root, not from here:

```
docker compose up -d db
uv run pytest tests/benchmarks
```

The suite lives at `tests/benchmarks`, beside the other three, and runs from the repo
root against the `db` service like they do — `db_bench` is for real runs and need not be
up. It puts this directory on its `pythonpath` and imports the modules the way the
harness does, top-level, so one file is never imported under two names. It enters
through this driver's command line at a handful of tasks per stage, writes into a
temporary directory, and so touches neither a results directory you already have nor the
persistent benchmark database.

Nothing in this directory imports the suite, and the suite asserts which measurements
ran, how big each was and whether the harness trusted them — never a rate. A benchmark
number is not something a test can assert without becoming a flake.

## When the `absurd-sdk` pin moves

Copy `results/` aside, re-run `uv run python -m stages`, and diff the two directories:
the stage filenames are stable, so a throughput regression shows up in a plain
`diff -r`. `process_scaling`, `poll_interval` and `checkpoint_cost` calibrate from
`stage_worker_knobs.json` and `latency_under_load` from `stage_process_scaling.json`, so
a partial re-run must include the stage it depends on (on a fresh checkout that means
starting with worker_knobs), or it errors saying so.

Results from before the stages were named cannot be diffed against these, and the report
will not render them — the filenames no longer line up and the producer entries lack
fields the current table reads. Re-measure rather than converting them.

## Layout

| file             | what it is                                                                                             |
| ---------------- | ------------------------------------------------------------------------------------------------------ |
| `pyproject.toml` | the harness's own uv project: django-absurd by path, everything else pinned                            |
| `uv.lock`        | the pinned resolution `uv run` installs into `benchmarks/.venv`                                        |
| `settings.py`    | Django settings; reads `DATABASE_URL`, else `PGPORT_BENCH` against `absurd_bench`                      |
| `manage.py`      | for `migrate` and for the worker children                                                              |
| `tasks.py`       | the five workloads: two no-ops, two sleeps, one 4-step workflow                                        |
| `host.py`        | host context capture and the suspension guard                                                          |
| `runner.py`      | spawns and reaps `absurd_worker` subprocesses                                                          |
| `producer.py`    | the enqueue side: preload, paced offer, producer benchmark                                             |
| `analysis.py`    | the SQL that turns Absurd's own columns into metrics                                                   |
| `measurement.py` | one measurement: reps, drain detection, median, dispersion, marks                                      |
| `stages.py`      | runs the stages (positional names, `--tasks`, `--duration`, `--io-seconds`, `--max-workers`, `--reps`) |
| `report.py`      | renders a results directory as markdown                                                                |

Worker children inherit the database the parent is actually using: the runner renders
`connections["default"].settings_dict` back into a `DATABASE_URL` and hands it to a
child running on this directory's `settings`, whatever settings the parent itself was
started with. That is what lets the suite's workers reach its throwaway test database
while a benchmark run reaches the persistent one.
