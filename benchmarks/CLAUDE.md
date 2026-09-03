# `benchmarks/` — the reasoning behind the rig

[`README.md`](README.md) is the actions: start the server, run the stages, read the
report. This file is everything behind them — what the harness measured and how much of
it to believe, the measurement model, the results-file schema, and why the server is
configured the way it is. Rationale belongs here; the README stays short and stays about
what a human does.

## How to read every number in this file

Every figure names the run it came from, and one quoted from several runs is a range
across them. All of them are tmpfs runs with the macOS indexer suppressed, on one
14-core laptop at `--max-workers 10 --reps 3`: four complete runs — `warmup`, `a`, `b`,
`c2` — plus `c`, which recorded `worker_knobs` alone. The offer-rate figures come from
two later `latency_under_load` runs (`after1`, `after2`) and the depth figures from one
`worker_knobs` at `--tasks 20000`, same server, same session.

**Runs with different calibration points did not measure the same configuration.** `a`'s
`worker_knobs` winner was `batch_32`, so every stage downstream of it ran
`--concurrency 16 --batch-size 32` where `warmup`, `b` and `c2` ran concurrency 16 with
the default batch; and its `latency_under_load` calibrated from `workers_5` rather than
`workers_7`, so its rate rungs were offered to five workers where the other three runs'
went to seven. An `a` row beside a `b` row is two experiments, not one measurement
repeated. Read the `Calibrated from` line first, always.

Load is sampled on each side of every rep. Median of every `load_before`/`load_after`
sample: `warmup` 2.68, `c2` 3.39, `b` 3.40, `a` 5.11. It does not account for a whole
run reading low — `c2` and `b` sat at 3.39 and 3.40 and still disagreed by up to 18% on
individual rungs. Measure on a quiet machine regardless: the macOS indexer alone was
measured at 1-1.4 cores sustained.

**Every ceiling proposed during this work turned out to be the measurement environment
rather than Absurd: disk fsync, then one connection's commit rate, then CPU. The harness
has not found Absurd's limit — only its own.** That governs how any figure here should
be read, and it is the most important thing the work produced.

Disk-era figures — anything measured with `db_bench`'s data directory on a Docker named
volume — are superseded. A few are kept below because their SHAPE is still the argument;
they are labelled, and their levels mean nothing now.

## Repeatability: rank things, do not confirm small changes

Runs `warmup`, `b` and `c2` — one commit, one calibration point — over the 25 saturation
measurements they share:

    median CV      4.7%      (14 of 25 under 5%)
    mean CV        5.1%
    worst CV      12.5%

`b` against `warmup` over the same 25: median ratio 0.98, CV of ratios 3.7%, ratios
spanning 0.88-1.08.

**4.7% is not a precision guarantee for a single measurement.** `c2` sits systematically
BELOW the other two on several of them (`workers_1` 1,066 against 1,211/1,306;
`async_dispatch` 888 against 1,137/1,071; 8-20% low on `concurrency_2`, `concurrency_4`
and `batch_32`) at the same per-rep load as `b`, so there is a between-run bias on top
of scatter. Treat a difference under ~12% as noise.

Two conditions produced these figures and both matter: the data directory on tmpfs, and
the macOS indexer suppressed.

## Overhead, itemised

Per `noop_sync` task (empty body), worker side, across the 204 `noop_sync` saturation
reps of `warmup`, `a`, `b` and `c2`:

    commits per task     1.72 - 3.01   part shape, part ramp; the split is open
    row updates          ~4            ~2 on r_bench, ~2 on t_bench
    backends per worker  2             SDK connection + Django ORM, for a body that
                                       touches no ORM itself

**That 2 holds only for task bodies that do not touch the ORM**, which is every workload
in `tasks.py`. Django's connections are thread-local and `open_worker_runtime` runs sync
bodies on a `ThreadPoolExecutor(max_workers=options.concurrency)`, so a body doing ORM
work opens a third backend per busy thread and holds it for the body's whole duration —
up to `C + 2` per process, 126 at the 7 x 16 this file recommends, against a
`BENCH_MAX_CONNECTIONS` of 100 and a stock `max_connections` of 100. Nothing here
measured that: `measure_shape_connections` counts backends across starting a fleet and
runs no task, so it observes worker STARTUP connections and cannot see a body-opened
one.

That last row is a property of an EMPTY body, not of a worker: a body that touches the
ORM opens one more backend per busy slot and holds it while it runs
([the durable workload](#the-durable-workload-and-the-backends-it-holds)).

From `pg_stat_statements` with `track=all`, at `worker_knobs` `concurrency_1` — the only
shape where the client remainder is exact
([the measurement model](#the-measurement-model)). Each figure is a range over the
median rep of the five saved runs:

    wall 2.82 - 3.14 ms/task = server 1.57 - 1.77 ms + client 1.16 - 1.48 ms

    1.00 calls/task  1.41 - 1.56 ms  claim_task            (top level)
    1.00 calls/task  0.84 - 0.92 ms    candidate CTE       (nested)
    1.00 calls/task  0.27 - 0.30 ms    cancellation scan   (nested)
    1.00 calls/task  0.09 - 0.12 ms  complete_run          (top level)

- WAL durability was 71% of a full run's active backend time on the volume (disk-era),
  which is why every rate here is read against a commit ceiling rather than on its own.
- Claiming costs 13-16x completing. All per-task database cost is acquiring work.
- The cancellation scan is 18-19% of the claim, on every claim.
- **40-47% of a task's wall time is outside the server, and what serialises it is not
  established.** `client_ms_per_task` is wall minus server, so it holds our Python, the
  SDK's and the round-trip waits — and a round trip releases the GIL. Two mechanisms fit
  every figure here: the GIL, or the single claim connection a worker process owns. Both
  predict processes beating in-process concurrency.

## Throughput: both axes were still buying at the top of the sweep

    shape    tasks/s              backlog   commits/task
    10x16    4,008.5 - 4,441.7    20,000    1.72 - 1.88
     7x16    4,052.1 - 4,231.4    14,000    1.96 - 2.07
     5x16    3,623.1 - 3,727.7    10,000    2.13
     2x16    2,074.6 - 2,292.8     4,000    2.13
     1x16    1,066.1 - 1,306.3     4,000    2.13
     8x1     1,924.4 - 2,028.3     5,000    2.35 - 2.69

`process_scaling` rungs over `warmup`, `b` and `c2`; `8x1` is `pooled_vs_split`'s
`split_8`. **`--tasks` belongs beside every rate in that table.**
`build_process_scaling_measurements` sizes the preload as `max(4000, 2000 * count)`, so
the ladder's rungs drain 4,000 to 20,000 tasks, and four times the backlog costs 40-58%
of the throughput on its own
([Queue depth costs throughput](#queue-depth-costs-throughput)). No rate in that table
compares with the rate above it.

    concurrency at 1 process, all 5,000 tasks:  1->16 gives 333-361 -> 1,096-1,231
                                                tasks/s, 3.0-3.7x over four runs
    diagonal at equal total, 5,000 both arms:   4x1 beats 1x4 by 1.95-2.28x
                                                8x1 beats 1x8 by 2.17-2.29x

Those two comparisons hold one depth each, so both are measurements: in-process
concurrency does not saturate around 3x, and processes beat threads at the same total.
The process axis has no multiple — its raw 1->10 quotients read 3.3-3.8x across
`warmup`, `b` and `c2`, each dividing a 20,000-task rung by a 4,000-task one, and the
deeper rung is the slower-reading one, so 3.3x is a LOWER BOUND. The report's
`T(N)/(N x T(1))` block prints each rung's backlog and marks itself `CONFOUNDED` for the
same reason.

Advice: scale with PROCESSES, concurrency around 16, batch the claims.

**Future work: run the ladder at one depth.** A fixed preload across every rung, sized
for the fastest one, is what makes the process axis a measurement.

## Queue depth costs throughput

Throughput RISES as a backlog drains. Within-rep drift — the least-squares trend over a
rep's `profile_slices`, read as total fractional change across the drain — over the 150
saturation reps of `warmup` and `b`:

    median +15.7%, positive in 120 of 150

The magnitude moves with the definition and the rep set — the same trend over just those
runs' 48 `worker_knobs` reps reads +19.3% in 36 of 48, last-slice-over-first +17.7% over
the 150 — so quote one only with both beside it. The SIGN is what nothing cumulative
could produce: accumulated state cannot make a drain faster.

Consequence: a saturation measurement averages a curve, so its single number depends on
the starting depth and does not compare across `--tasks` values. `profile_slices` makes
the curve visible; the report's `backlog` column makes the depth visible beside the
rate.

**How much depth costs, measured directly.** The whole `worker_knobs` concurrency ladder
at `--tasks 20000` against the four baseline runs at 5,000, same machine, same session:

    rung             5,000 (4 runs)      20,000    ratio
    concurrency_1    333-361             147.0     0.42
    concurrency_2    320-376             154.5     0.44
    concurrency_4    514-619             267.4     0.47
    concurrency_8    809-913             415.7     0.47
    concurrency_16   1,096-1,231         716.4     0.60

Four times the backlog costs 40-58% of the throughput on every rung. The within-rep
drift grows with depth with it: the same fitted trend reads a median +19% over the
ladder's 60 reps at 5,000 and +46% over its 15 reps at 20,000, while slice 0 reads
0.60-1.09 of its rep's median slice at 5,000 against 0.46-0.93 at 20,000.

That is why `--tasks` is a comparability key and not a size knob, and it is the measured
answer to raising `SATURATION_TASKS`:
[the window is long enough already](#saturation_tasks-stays-at-5000). It is also why
`process_scaling`, which sizes its own preload from the worker count, reports no scaling
multiple:
[both axes were still buying](#throughput-both-axes-were-still-buying-at-the-top-of-the-sweep).

## A drain rate is not an arrival rate

A saturated fleet has work waiting at every claim; a paced one has to keep up in real
time, claim polls that find nothing included. So the two rates are different quantities
and the drain one is the larger, and `latency_under_load` measures its own arrival rate
rather than taking fractions of a drain rate. A ramp offers for 20 s at a time, climbing
from a tenth of the drain ceiling by half again a step, and stops at the first offer
that did not come off cleanly; the highest one that did is the working point, and the
rungs are fractions of THAT. The whole ramp is recorded probe by probe under
`sustainable_rate`, because a working point nothing can see is a number a reader cannot
argue with.

**The ramp stops at the lower of two limits and does not say which.** The producer runs
on the same box as the workers, so a probe fails either because the fleet fell behind or
because the enqueue loop never made the offer. On this server, both at once: `after1`
and `after2` absorbed 2,142.1/s and refused 3,213.2/s, and the refused probe tripped
both guards in both runs.

    run      target/s   producer achieved/s   deadlines missed   fleet completed/s   backlog growth
    after1    3,213.2               2,894.8   61,225 / 64,264              2,741.2   4,472 / 30,300
    after2    3,213.2               3,016.4   60,961 / 64,264              2,954.3   2,614 / 31,511

`offered_ok` false — the producer delivered 90-94% of its target — AND `backlog_grew`
true under what it did deliver. So 2,142/s is the highest offer this rig got through
cleanly, and it bounds the two sides jointly. The report prints `achieved/s` and
`producer kept up` beside every probe, and its offer-rate line names both limits.

**The fleet's own knee is measured by over-offering it, which the ramp cannot do.**
Three runs at 7 workers x concurrency 16 — `warmup`, `b`, `c2`, whose `workers_7` drain
rates measured 4,052-4,231 tasks/s — offered fractions of that drain rate for 60 s a
rung:

    rung          offered/s      p50 (medians)   throughput/s (medians)   cv
    rate_25pct    1,013-1,058    15-18 ms        1,013-1,057              3-65%
    rate_50pct    2,026-2,116    30-34 ms        1,982-2,084              2-167%
    rate_75pct    3,039-3,174    39-68 SECONDS   619-718                  10-18%

Medians throughout, so one column cannot be read against another's endpoints. At
2,026-2,116/s the fleet kept up in seven of nine reps and went bimodal in two — 29-34 ms
beside 1.30 s and 2.61 s in the same measurement, which is the 167% CV. At 3,039-3,174/s
it collapsed to 619-718/s while the producer delivered its full offer in seven of nine:
`warmup`'s first rep offered 188,203 tasks over 60 s, delivered 3,136/s of them, and its
trimmed window completed 619/s. So the knee is at or a little above half the drain rate
and below three-quarters of it, and a p50 measured above the knee is a function of the
window length rather than of the system.

Two hazards live in those rows. A rung whose drain outruns `RATE_TIMEOUT_S` raises
`MeasurementTimeoutError`, which reaches no handler and takes the invocation with it —
the stages that finished keep their files and their closing ceiling
([the results files](#the-results-files)), and everything after that rung is unmeasured.
And 60 s diverges where 20 s does not — the fleet completed 2,741-2,954/s inside a 20 s
probe and collapsed at a comparable offer held for 60 — so a rate the ramp absorbed is
not a rate a rung will; [the guard](#the-guard-a-backlog-that-grew-measured-nothing)
catches that on the rung.

**What the rungs actually exercise.** Both post-change runs:

    rung          offered/s   p50 (after1 / after2)   cv (after1 / after2)   marks
    rate_25pct        535.5   9.7 / 10.5 ms           3.6% / 3.5%            none
    rate_50pct      1,071.1   15.3 / 16.1 ms          0.6% / 3.4%            none
    rate_75pct      1,606.6   23.1 / 24.0 ms          3.7% / 0.9%            none
    rate_90pct      1,927.9   29.9 / 28.8 ms          1.8% / 2.9%            none

A monotonic latency curve against offered load, which is what the stage is for, at 14.2
minutes of measured phase per run, ramp included. **The top rung asks the fleet for
1,927.9/s, so a change that only shows above about 2,100/s — where these runs put the
knee — is invisible to this stage.**

Back to back, the two runs absorbed and refused the same probes and their rungs agree to
0.96-1.08 — inside the rig's own 12% floor. Both read the same
`stage_process_scaling.json` back, though, so what repeats is which offer got through
and not the drain ceiling the ramp climbs from: a run whose `process_scaling` lands 12%
elsewhere probes 12% elsewhere too, and its rung rates move with it. That is what makes
`sustainable_rate` the first thing to compare between two of these reports.

**The knee is bracketed, never resolved.** Probes are 1.5x apart, so the ramp puts the
working point between 2,142 and 3,213 tasks/s and nothing here refines it; it is
deliberately the low end of that bracket.

**A ramp whose first probe diverges does not abort the stage.** It measures at that
lowest rate, records `sustained: false`, and the marks on the rungs become the finding.
An aborted stage would leave nothing to read at all.

### The guard: a backlog that grew measured nothing

Every paced rep records the backlog at the midpoint of its offer window and at the end
of it — enqueued minus completed, read off Postgres's own columns after the drain, so
the growth is exact rather than sampled while the offer ran. A rep whose backlog grew by
more than 5% of the tasks offered between those two points is `invalid`: it measured the
climb towards a rate rather than the rate.

A share and not a count, because one threshold has to cover a five-task smoke offer and
a 120,000-task one. **The denominator is `offered_after_midpoint`, the tasks offered
between the two readings — half the window, not the whole offer.** In steady state the
backlog is one Little's-law-worth of in-flight work at each end, so the growth cancels
to a fraction of a percent — under 0.2% at 160 concurrent tasks over a 60-second offer —
while a fleet absorbing 95% of its offer reads 5%. The refused 3,213/s probe read 4,472
tasks of growth against the 30,300 offered after its midpoint, 14.8%, and 2,614 against
31,511, 8.3%, in the second run.

5% rather than tighter, on evidence: `after1`'s `rate_90pct` ended two of its three reps
with backlogs of 1,149 and 2,345 tasks against 46 and 52 at the midpoint — 1.9% and 4.0%
of the ~57,800 offered after the midpoint — and all three reps still agreed on the p50
within 1.8%: the rep with no spike at all read 30.6 ms against 29.5 and 29.9. A 2% limit
would throw that measurement away for a transient that does not reach the number.

The blind spot is the other side of the same threshold: a fleet absorbing 96% of its
offer diverges slowly and this guard will not say so. That is part of why the top rung
is 90% of a measured knee rather than 100%.

## `SATURATION_TASKS` stays at 5,000

5,000 was sized when `concurrency_1` drained 67 tasks/s, which made a 75-second window.
On RAM the same rung drains 333-361/s, so the window is now 13-16 s and the fastest rung
using the constant is 4 s — short enough to ask whether what it measures is mostly the
fleet getting going. Tested by running the whole ladder at `--tasks 20000` and comparing
it with the four baseline runs. Everything below is the concurrency ladder's five rungs,
3 reps each — 60 reps at 5,000 against 15 at 20,000:

    what moved            5,000 (4 runs)     20,000
    medians               (the table above)  0.42-0.60x
    cross-rep cv          0.7-7.3%           1.9-4.5%
    within-rep profile_cv 2.6-16.9%          8.7-29.0%
    slowest rep           15.7 s             148.1 s

So a longer window does not tighten anything: the cross-rep CV at 20,000 lands inside
the bracket the same rungs already spanned at 5,000, and the within-rep profile gets
NOISIER — its whole band moves up — because the drain now crosses more of the depth
curve. What it does change is the experiment — the medians move by more than half, which
is depth and not precision — at four times the tasks and up to ten times the wall clock.
**Rejected.**

A short drain is not a fleet getting going, either — at one process. `start_workers`
waits for that child's readiness line before `measure_phase` opens, and the p10-p90 trim
then drops the first tenth of the completions. Over the concurrency ladder's 60 reps
slice 0 sits at 0.60-1.09 of its rep's median slice, and the SHORT rungs show no deficit
to explain — `concurrency_16` and `batch_32` (4.0-5.1 s) read 0.78-1.10 while
`concurrency_1` (13.1-15.7 s) reads 0.60-0.93. Dropping slice 0 moves a rep's fitted
drift by a median 0.9 points across those 60 reps and at most 7.4. A short drain here is
a shallower queue, not a ramp.

### Where a ramp IS in the window: the multi-process arms

**Above one process the fleet starts inside the measured drain.** `start_workers` spawns
children one at a time, blocking on each one's readiness line, and the preload is
already in the queue — so worker 0 claims for the whole stagger before the last child
exists. A child's interpreter and Django startup times at 0.20 s, putting roughly 1.5 s
of staggered start inside an eight-process arm and 2 s inside a ten-process one.

`split_8` (8x1, 5,000 tasks, 1.5-2.0 s) reads slice 0 at 0.21-0.24 of its median and
climbs from 479-523 to 2,286-2,714 tasks/s across its own drain, while the pooled arm of
the same pair (1x8, 5.5-6.1 s) sits at 0.95-1.05. The contamination tracks the PROCESS
COUNT and not the window length, and the pooled arm is what rules out a warm-up
explanation: one interpreter warms its claim path as much as eight do, and it shows no
deficit. `split_8` publishes 1,920-2,055 tasks/s against its own median slices of
2,060-2,253, so it understates by 7-15% and `pooled_vs_split`'s split/pooled ratio is
understated with it — the direction of that ratio is not in doubt either way, and it
would only grow. (All four runs, 12 reps.)

**`commits_per_task` and `calls_per_task` take the same defect the other way up.** Both
divide a phase-window delta — `xact_commit`, and `pg_stat_statements` — by `n_runs`,
which `analyze_saturation` counts over the WHOLE drain (`window = true`). Tasks finished
before the phase opened are in the denominator and their commits are not, so both counts
read low above one process, by the share of the drain that ran during the stagger. What
that costs in practice is not established: the measured `commits_per_task` column falls
from 2.13 at one, two and five processes to 1.88 at ten, which is the right direction,
but a bias proportional to the stagger would have moved the two- and five-process rungs
too and it did not. Treat the spread across process counts as unresolved rather than as
shape.

That is a real defect and a bigger constant is the wrong fix for it: it would dilute the
stagger only by measuring a deeper queue for both arms of a pair, at four times the
cost, while degrading every single-process rung it touches. Two things would: spawning
the children concurrently rather than one at a time, and a windowed analysis — a
saturation rep capturing the database clock the way a rate rep already does, and
counting both completions and runs from there. The second moves every saturation figure
in this file. Neither is done here.

## Storage: two regimes on a volume, and none on RAM

Ten arms each, interleaved so the media alternate, one configuration throughout:

    disk (volume)   618 466 464 679 462 476 590 579 776 1,698   median   584 c/s  cv 54.7%
    tmpfs (RAM)     46,356 ... 187,643                          median 56,659 c/s

The volume was the single largest source of irreproducibility here: its durable commit
rate sits in one of two regimes about 3.5x apart and nothing the harness controls
selects between them. RAM is not better storage; it is storage taken out of the
measurement.

**A run's own probe is the figure to use, and the interleaved arms above are not it.**
Those arms are one connection alternating media without the per-session warm-up a
results file's probe does, and their tmpfs median is 56,659 c/s. The five saved runs'
opening probes — warmed, on the session that then times — recorded 203,804-240,964
durable commits/s (cv 3.7-14.6%) and 512,453-617,208 non-durable; `a`'s closing probe
read 307,377. Every commit-budget verdict in a report is read against the in-run
figures; the interleaved arms establish the ratio between the media and nothing else.

Either way the ceiling stops constraining: at 203,804 c/s and the 1.72-3.01 commits per
task measured here the database could sustain 68,000-118,000 tasks/s against the
4,000-4,400 this rig reaches, so neither the ceiling nor its spread reaches the numbers
below it. `synchronous_commit=on` and `fsync=on` are unchanged: the durability request
is the same one, only the medium moved.

A stage took ~14 minutes on the volume and ~4-5 minutes on RAM.

Wall-clock cost is not why the volume went, though. The reason is that no single ceiling
probe characterises a run on it: one run opened at 615 c/s and closed at 2,100, a
ceiling that moved 3.4x DURING the run.

## The durable workload, and the backends it holds

Every workload here but one finishes in microseconds and touches no ORM, so every
finding above is a finding about the NANO-TASK regime. django-absurd's primary use case
is the other one: a durable agent tool call runs for seconds to minutes and reads and
writes application rows. `tasks.run_durable_work` is that regime — it inserts a
`workload.WorkItem`, spends `--durable-seconds` updating and re-reading it, and clears
it. Its own app and its own table, because a body writing into Absurd's tables would
move the very columns every metric here is defined on; cleared on the way out, because a
table that only grows makes every later rep of a measurement slower for a reason no
column records.

**A sync body holds a Postgres backend for as long as it runs.**
`django_absurd/worker.py`'s `open_worker_runtime` installs a
`ThreadPoolExecutor(max_workers=concurrency)` as the loop's default executor, and every
sync task body lands on it through `asyncio.to_thread`. Django's connections are
thread-local, so the first ORM statement in a body opens a THIRD backend on that thread,
held until the body returns and `close_old_connections` closes it again. Measured by
`pooled_vs_split`'s connection probe, on the suites' plain `db` server rather than in a
benchmark run — these are counts, so no server's tuning changes them:

    shape   idle backends   every slot working
    1x4     2               6
    4x1     8               12

One more per busy slot, exactly. So a worker process holds up to **`--concurrency` + 2**
backends and a fleet holds `processes x (concurrency + 2)`: 18 per process at the
concurrency 16 the nano-task ladder recommends, which is five processes against a stock
`max_connections` of 100. A nano-task fleet never reaches that count — an empty body
opens nothing — so an idle-fleet reading is the whole story there and is the wrong
number to size a server against anywhere else.

**The probe samples while work is running, which is the only time that count exists.**
It starts the fleet, reads the idle delta, offers two rounds of durable work per slot,
then takes the peak of `pg_stat_activity` every 50 ms for
`2 x poll_interval + 2 x --durable-seconds`. Two rounds so every slot still has work in
hand while the sample runs even where one process claimed a whole round to itself. It
runs the durable workload whatever the arm it describes is measured on: a nano-task body
never holds a thread long enough for a sampler to see, so that column would be the idle
one repeated. And it is per SHAPE, not per measurement — connection cost is a property
of the topology, and the arms of a pair share theirs whatever they run.

Its offer goes through the harness's OWN connection rather than `preload_tasks`, whose
pool threads each open a backend: a closed backend lingers in `pg_stat_activity` long
enough for the next sample to count it, and nothing in the view separates a producer's
from a worker's. Measured with the ORM taken out of the durable body, where the true
answer is the idle count repeated: the threaded preloader read `1x4 2 -> 3` and
`4x1 8 -> 9` on the first probe of each shape and the honest `2 -> 2` and `8 -> 8` on
the second.

**`pooled_vs_split` is the only stage with a durable arm.** Both totals are measured on
both workloads — `pooled_4`/`split_4` against `pooled_durable_4`/`split_durable_4` — and
the report ranks the shapes once per workload, because a body that holds a thread and
one that does not are two different questions about the same topology. Everything else
here — the concurrency ladder, the process ladder, the poll intervals, the checkpoint
multiplier, the rate rungs — is still nano-task only, so read each of them as a finding
about that regime and nothing else. The durable arms are sized per SLOT
(`DURABLE_ROUNDS_PER_SLOT` rounds each) rather than at a fixed task count: a fixed count
runs for minutes at one shape and seconds at the other.

**`--durable-seconds` defaults to 2, the FLOOR of the regime rather than the middle of
it.** A durable rep costs its rounds times this value, so the flag is that stage's whole
wall clock. `--durable-seconds 30` is the same experiment at an agent tool call's real
duration and roughly fifteen times the bill. Below `SMALLEST_MEASURABLE_DURATION_S` it
is refused: a body that holds nothing is the nano-task arm again, recorded under a
durable name.

## Incidental, worth raising upstream

- `r_bench` bloats and `t_bench` does not: 10,000 dead tuples against 5,000 live, versus
  77, on identical update volume. The tasks table's updates are HOT-pruned; the runs
  table's are not.
- The cancellation scan runs on every claim and costs 18% of it.

## The measurement model

**Two experiments, answering different questions.** A _saturation_ measurement preloads
a backlog, starts the workers and waits for the queue to drain: it measures the ceiling,
and its latency numbers are meaningless because every task but the first waited in a
queue that was full by construction. A _rate_ measurement starts the workers first and
offers tasks at a fixed rate for a fixed duration, so queue wait is real and end-to-end
percentiles mean something. Latency guidance only ever comes from rate measurements.

**Every timing is a Postgres column.** `t_bench.enqueue_at`, `r_bench.started_at`,
`r_bench.completed_at`, all written server-side by Absurd's own SQL, so producer and
workers are timed on one clock with no skew to correct. The driver contributes no
timestamps: throughput is `0.8 * n / (p90 - p10)` over the completion times, trimming
ramp and tail, and fairness is `count(*) group by claimed_by` over the same rows.

**Throughput is profiled within a rep, not only across reps.** Each saturation rep
carries `profile_slices` — throughput over successive slices of 200 completions, slice 0
the fullest queue — with `profile_median_per_s` and `profile_cv`. Equal-COUNT slices,
not equal-time: a slower measurement puts fewer completions into a fixed time slice, so
its slices would read as noisier for purely statistical reasons and would not compare
across settings. The partial slice a drain ends on is dropped; under three full slices
records `null`. Rate reps carry no profile — their offer rate is imposed, so slicing it
would plot the producer's own pacing back at the reader.

Reading a profile: within a rep, remaining depth falls while accumulated database state
only grows, so the two point opposite ways. Rising across the slices means depth drives
the per-task cost. Flat within a rep while reps disagree rules depth out and points at
cumulative state or a per-run latch. A sawtooth is contention, and neither.

**A throughput is a commit rate in disguise.** `commits_per_task` is the database's own
`xact_commit` delta across the measured phase over the runs that completed inside it, so
it covers claim, completion and the driver's drain polling and excludes the preload.
Throughput times that is the commit rate the run asked of the database, and the report
prints it under each saturation table — divided by the worker count first, because **the
ceiling is one connection's**. A worker process with idle slots opens exactly two
backends whatever its `--concurrency` (the SDK's connection and Django's), and its CLAIM
traffic funnels through the SDK's one however many threads are behind it — a body that
touches the ORM opens a third of its own, which does no claiming and is counted
separately ([the durable workload](#the-durable-workload-and-the-backends-it-holds)).
The box has several times that capacity spread across more of them; concurrent backends
share an fsync through group commit, so that scaling is sublinear (measured once on the
volume in its fast regime: 1,953 c/s at one connection, 4,748 at four, 13,238 at sixteen
— read the shape, not the levels, and note that a single-connection ceiling is a per-run
measurement rather than a constant of the machine). Comparing a fleet's total against
one connection's ceiling would call an eight-process measurement saturated while each of
its connections idled.

So a row is **connection-bound** when its per-connection commit rate is near the ceiling
— that number belongs to Postgres and moves on another machine — and **client-bound**
when it is a fraction of it, which is the kind that is about Absurd. "Near" is 70%, a
judgement rather than a measured boundary — for scale, the volume-era concurrency ladder
ran from 28% of the ceiling at concurrency 1 to 102% at concurrency 16, sixteen threads
on the one connection being what saturated it. The verdict is read off the band the
durable probes put around that rate rather than off any median: a row whose band
straddles the line prints `unresolved` instead of being assigned to whichever side one
draw of the calibration favoured. On RAM every row reads `client-bound`, which is the
finding — what runs out on this rig is cores. Rate reps carry no `commits_per_task`:
their completed-run count comes off the trimmed middle of the offer window while the
phase spans the whole offer and drain, so the quotient would divide two different
windows into each other.

**Every saturation rep also says what those commits carried, statement by statement.**
The measured phase is bracketed with `pg_stat_statements` snapshots and diffed, so
`statement_stats` names each statement, its `calls_per_task`, its
`total_exec_ms_per_task` and the rows it touched. `track=all`, not `top`, because
Absurd's whole claim path is PL/pgSQL and `top` would record the call that entered it
while hiding every statement inside. A statement run inside another is
`"toplevel": false` and does not sum into `server_exec_ms_per_task` — a function's own
row already counts what it ran. What is left of the wall time is `client_ms_per_task`:
our Python, the SDK's, and the round trips, which is the number separating "Absurd's SQL
is expensive" from "our Python is expensive". Server time sums over every backend the
phase used, so above one worker it counts concurrent work against a single wall clock
and the remainder stops being one; at 1x1 it is exact, which is where the instrument is
worth reading. The driver's own drain poll is in the list like any other statement, so
its share is attributed rather than assumed; the reader's own statements are excluded by
name.

The counters are never reset: `pg_stat_statements_reset()` cannot be undone and would
take out whatever else reads the same server, so a phase is two cumulative snapshots
subtracted and a statement whose counters did not move is somebody else's.
`python -m stages` issues `create extension if not exists pg_stat_statements` before
measuring, because the extension is per-database and a RAM data directory loses it on
every restart exactly as it loses the schema. A role that may not create extensions, or
a server that never preloaded the library (which is every suite server here), leaves
`statement_stats` null and changes nothing else: the instrument is worth a run's
itemisation, not the run.

The view is read through a function of the harness's own (`STATEMENT_STATS_READER`)
rather than off `pg_stat_statements` directly, because one code path has three servers
to satisfy: one that preloaded the library, one where nothing created the extension
(`undefined_table`), and one where the extension exists but the server started without
it, which is every suite server here (`object_not_in_prerequisite_state`). The last two
mean the same thing to a caller — no statement stats — and neither may abort a run, so
both are caught server-side in one handler. Three mechanics of that function are
invisible from its call site:

- It is DROPPED before it is created, because `create or replace` refuses a changed
  return type and a database that has held an older reader outlives one edit here easily
  (every `--reuse-db` test database does). The failure would otherwise be a run
  recording no statement stats for a reason nothing in it names.
- It returns one JSON document as `text`, not `jsonb`: Django's psycopg3 backend
  registers an identity loader for jsonb so its own JSONField can decode with its own
  decoder, so a jsonb column reaches Python as the undecoded string anyway. A null
  DOCUMENT rather than SQL NULL, so the caller always parses one JSON string.
- It excludes its own statements by TWO `like` patterns — this query, which names the
  view, plus the drop and create, which name the function. A comment marker would be
  tidier and does not survive: plpgsql hands SPI an expression whose text has had its
  comments taken out.

**Redelivery needs the right query, not instrumentation.** Absurd's `fail_run` marks the
failed run `'failed'` and inserts a NEW row for the retry, and `complete_run` only ever
accepts a run still `'running'`. A completed-only count can therefore never see more
than one run per task and would report every redelivery as zero. `extra_runs` is counted
over every run row (`total_runs - total_tasks`), with `max_attempt` beside it to say
what kind of redelivery it was. Throughput keeps its own completed-only count, so failed
and pending rows can never inflate it.

**The suspension guard.** A measurement brackets its phase with both
`time.perf_counter()` and `time.time()`. If wall time outruns monotonic time by more
than two seconds the host napped mid-phase, the rep is thrown away and the measurement
is marked invalid. A wall clock stepping BACKWARDS is an NTP correction, not a nap, and
is tolerated.

**A drain waits on the queue AND on the fleet.** A queue polled by itself cannot tell a
slow drain from an absent one, so a rung whose workers died would sit out its whole
`timeout_s` — 900 s in saturation — and then report the timeout as the failure, with the
crash that caused it printed underneath as an afterthought. Any child exit ends the
wait, a clean one included: a worker that returned 0 has stopped claiming as thoroughly
as one that died. `stop_workers` then raises the children's own crash over that refusal
on the way out, so what a reader sees first is the failure and not its symptom.

**A fleet starts all at once, because the queue is already full when it does.**
`start_workers` launches every child before waiting for any child's readiness line. A
saturation rep preloads its backlog BEFORE starting the fleet, so the first child begins
draining while the rest are still starting — and throughput is taken over the completion
timestamps, trimmed p10-p90, which puts those early completions inside the measured
window at a fraction of the fleet that was asked for. Waiting for each child in turn
stretched that stretch by one child's whole start-up per extra process and read every
multi-process rung low, the more so the more processes it had. The waits are still
written one after another; the children are already running by then, so each one that
reported readiness during an earlier wait returns at once.

**The summary rep is the unluckier middle, and which one that is depends on the
metric.** `pick_median_rep` sorts the valid reps by the ranking key and, at an even rep
count, takes the WORSE of the two middles — the lower throughput, the lower enqueue
rate, the HIGHER end-to-end p50 — because a measurement must not be summarized by its
luckiest rep, and the metric is what says which one that is.

**Marking is the honesty mechanism, and nothing is suppressed.** Every rep votes, not
just the median one: a rep that under-offered has a LOWER latency, so it sorts away from
the median and would never be looked at. Two unrelated things can be wrong, so two
booleans record them:

- **`invalid`** — a rep measured something OTHER than what was asked: suspended
  mid-phase; `extra_runs > 0`, i.e. a redelivery; a saturation measurement that finished
  with fewer completed tasks than it enqueued (`missing_tasks`), since a terminally
  failed task still satisfies the drain predicate and the sample would shrink silently;
  a trimmed completion window too short to divide by (`degenerate_window`); a rate
  producer that could not sustain 98% of its target offer; or a paced rep whose queue
  was still growing when the offer stopped (`backlog_grew`, see
  [the guard](#the-guard-a-backlog-that-grew-measured-nothing)). A hard error —
  re-measure.
- **`unstable`** — the reps measured the right thing and disagreed. A finding about the
  system, and often the most interesting one in the run.

**Instability is thresholded on the CV; the range is what gets printed.** `cv` is the
sample standard deviation over the mean of whichever metric the reps were ranked on —
throughput in saturation mode, end-to-end p50 in rate mode, enqueues/s for the producer
— and the limit is 10%. `spread`, `(max - min) / median`, is a RANGE, and a range grows
with the rep count on unchanged data: 5.3% at 3 reps and 14.1% at 50 on a fixed process,
taking the flag rate from 0.2% to 30.3%. `--reps` is a live flag, so thresholding on it
meant that asking for more repeats made good data look worse. A CV is flat over that
same sweep (2.8% to 3.1%) and separates the real measurements cleanly: settings that
repeat sit at 1.0-3.4%, ones that genuinely lurch at 25-50%, and 10% is the gap between
them. `spread` and the endpoints (`range_low`, `range_high`) are still recorded and the
report prints the endpoints, because `209-501` is what a reader wants to see — they have
simply stopped being the trigger. A `spread_floor` guards the test in the ranking key's
own units: reps closer together than the floor are not called unstable however far apart
they read relatively. Saturation mode sets none — a throughput's relative and absolute
dispersion say the same thing — and the rate stages set `RATE_SPREAD_FLOOR_S`, 10 ms.
**A floor has to sit BELOW the measurements it guards, or it disables the mark instead
of steadying it:** `latency_under_load`'s rungs read p50s of 9-30 ms and `poll_0.05`
39-42 ms, so no combination of their reps clears a floor set anywhere near a tenth of a
second. At 10 ms a rung that lurches still marks, and a 10% CV on reps milliseconds
apart does not.

Fewer than two valid reps is an unknown dispersion rather than a zero: `spread` and `cv`
record `null` and the row is marked `?`, so a measurement that measured nothing cannot
read as the most stable in its stage. `--reps 1` is a dry run for that reason.

**Every measurement appears in every table and in every number derived under one.**
Nothing is dropped: a measurement struck from a ratio table is a finding deleted, and
reproducible instability is exactly the finding worth keeping. A derived line built on
measurements that disagreed carries the disagreement — a ratio divides the endpoints too
and prints the interval beside the point estimate, `1.94x (reps 1.49-2.40x)`. The one
thing a mark still decides is CALIBRATION: `pick_best_measurement` prefers a valid,
stable rung when picking the working point later stages inherit, falling back to the
whole set rather than refusing. That is choosing a configuration to measure at, not
editing a report.

**Load is sampled on each side of every rep.** `load_before` and `load_after` sit on the
rep itself, because the per-measurement `host` block is collected once all its reps are
over: its 1-minute average counts the load the harness had just made and could never
answer whether the machine was otherwise busy. A quiet machine buys clean reps and clean
rankings within the run; it does not buy an absolute rate a later run will agree with.

**`latency_under_load` deliberately does not calibrate from the outright ceiling.** A
rate measurement's producer runs on the same machine as its workers, so aiming at the
throughput of a `process_scaling` result that used every core would leave the producer
no CPU to offer tasks that fast: the measurement would describe a load never applied and
be marked invalid for under-offering. It calibrates from the fastest result at or below
`RATE_WORKER_CAP` (half the cores). That is the FLEET, and it is separate from the offer
rate: the drain rate of the rung it picks is only where the ramp starts climbing —
[a drain rate is not an arrival rate](#a-drain-rate-is-not-an-arrival-rate).

## Stage mechanics worth knowing

**Worker counts scale with the host.** `build_worker_ladder` derives the
`process_scaling` ladder from `os.cpu_count()`: 1 and 2 anchor the low end where
per-worker efficiency is still readable, then quarter, half, three-quarter and full.
Eight cores gives 1, 2, 4, 6, 8; thirty-two gives 1, 2, 8, 16, 24, 32.

**`--max-workers` is the size flag for topology.** `--tasks`, `--duration`,
`--io-seconds` and `--durable-seconds` size the work; nothing sized the fleet, and the
fleet tracked the host. Thirty-two cores means 83 worker processes spawned across
`process_scaling` alone, and 128 cores means 323. `--max-workers N` lowers the ceiling
the ladder is derived from, so a bounded ladder is still a ladder rather than one rung
repeated: `--max-workers 3` gives 1, 2, 3. The same bound caps the `poll_interval` idle
probes, drops whole any `pooled_vs_split` pair whose split arm it cannot spawn, and
narrows which `process_scaling` rung `latency_under_load` may calibrate from, so the
offered rate stays a rate that fleet actually measured. Bound both stages together:
`--max-workers` on `latency_under_load` alone reads back a `stage_process_scaling.json`
measured on a larger fleet.

A size below what a stage can measure is refused before anything runs, rather than
crashing partway or writing a number describing work that never happened: fewer than one
worker, fewer than one task, a rate window of no length, or a durable body of no
duration, which is the nano-task arm again under a durable name. `--io-seconds 0` stays
legal — no simulated IO is a real point on that experiment's axis.

**`pooled_vs_split` measures the diagonal the other two miss.** `worker_knobs` sweeps
concurrency at one process and `process_scaling` sweeps processes at one concurrency, so
the two ways of reaching the SAME total were never put against each other: `pooled_4` is
1x4, `split_4` is 4x1, and the same for 8. The shapes are fixed twice over — not
calibrated from an earlier stage, since configuring one arm from a winner would make the
pair unequal in a second way, and not derived from the host, because a shape that means
something different on every machine cannot be compared across machines. Each total runs
on both workloads, so the stage answers the diagonal twice
([the durable workload](#the-durable-workload-and-the-backends-it-holds)). Three
mechanics matter:

- **A pair is skipped whole, never half-run.** Running `1x8` against a bounded `4x1`
  would be an unequal comparison presented as an equal one, so neither arm runs and both
  the console and the results file name the pair and the bound that refused it. A bound
  under every total leaves the stage with nothing to compare, which is a file saying so
  rather than an error.
- **The arms alternate across reps.** A pair's arms run back to back and the whole
  schedule reverses on odd reps, so no arm and no pair always goes first. Cumulative
  database state only grows across a stage; a fixed order would hand one arm of every
  pair the emptier tables, which is exactly how an earlier control in this repo came to
  be invalidated. The order they ran in is recorded and printed.
- **Connection count is measured, because it is a confound.** Idle, `1x4` reaches
  four-way concurrency on two backends and `4x1` reaches it on eight, so the shapes
  differ in connection count as well as in claim path, and the report says so above the
  ratio rather than leaving it to be read as the claim path alone. The delta across
  starting the fleet is only half of it: the probe then runs durable work and takes the
  peak, so the report also carries what a body that holds a thread costs — 6 and 12 for
  those same two shapes. The day a pooled worker opens a claim connection per slot the
  same line will say the comparison is clean.

## The results files

`results/stage_<name>.json` is written by `python -m stages` and by nothing else,
rewritten atomically after every finished measurement, so a run killed at hour two keeps
everything it measured. The directory is git-ignored on purpose: a committed set would
read as django-absurd's official figures. A rendered `report-<UTC stamp>.md` lands
beside the JSON so a run and its reading stay together, stamped because a second run
would otherwise overwrite the first reading while its own JSON sat right there.

**Each file says which configuration produced it.** Beside `measurements` sits an
`options` block holding `--tasks`, `--duration`, `--io-seconds`, `--durable-seconds`,
`--max-workers` and `--reps` RESOLVED — an unset flag records what the run actually
used, not a null to go look up. `--max-workers` is the one nothing else recovers: unset
it tracks the host, so an unbounded ten-core run and a fourteen-core run bounded to ten
write the same ladder. `--tasks` and `--duration` stay `null`, meaning the stage sized
itself — neither has one default to name, and every measurement's `spec` carries the
size it ran at. The whole set prints on one header line, and a flag two stage files
disagree about reads as `mixed (8, 60)`, the way a mixed git SHA does. A file written
before this block existed has none and the report will not render it; re-measure rather
than hand-adding one. The same goes for one written before a measurement carried
`invalid`, `unstable`, `cv` and its rep endpoints: it records a single `flagged` the
report no longer reads. Results from before the stages were named cannot be diffed
against these either.

**Each file also says what one connection to this server could commit.** Beside
`options` sit `commit_ceiling_durable`, `commit_ceiling_nondurable` and
`commit_ceiling_durable_after`, one block each: `median_per_s`, `cv`, `range_low`,
`range_high` — the same dispersion vocabulary a measurement uses for its reps, because
the durable rate is a distribution and not a constant. With the `cluster_name` beside it
in the host block, **this block is what says which server the run got** — five figures
here is RAM, three is a disk.

Each probe warms before it times. A session's commit rate can climb over its first
thousand or so commits — consecutive 400-commit rounds on one volume-era connection read
519, 544, 1,276, 1,899, 1,912, 1,914/s — so the probe runs rounds until it has committed
`PROBE_WARM_UP_COMMITS`, throws those away, and records the median and spread of the
five that follow. The climb is per connection, so the warm-up has to happen on the
session that then measures. Warming removes that bias; it does not deliver a particular
number. The same six-round probe on an idle machine has also read 566, 508, 628, 500,
587, 510/s — flat from first round to last. So the ceiling is not a constant of the
machine to be looked up once, which is why every results file carries its own.

A WARMED probe is still a draw from a wide distribution, which is why one is recorded as
a median with its dispersion and never as a scalar. Eight volume-era trials spread
600-2,262/s around a 2,016 median, a 28% CV, while the same trials with
`synchronous_commit = off` held to 5%; two warmed opening probes, one per run, read
1,984/s and 615/s. Nothing here selects between those two regimes. The two probes are
also sized differently on purpose — `DURABLE_PROBE_COMMITS` 300 against
`NONDURABLE_PROBE_COMMITS` 5,000 — because the non-durable rate reached 512,000-617,000
commits/s across the five saved runs, where 300 commits would be timing under a
millisecond. (On tmpfs the durable count is barely out of that regime either:
[Storage](#storage-two-regimes-on-a-volume-and-none-on-ram).) A round read straight
after a run once came back at 719/s: that was a cold session, not a depressed server,
and it is why all three probes warm.

The opening probe runs before the first measurement and the closing one after the last.
The closing probe is not the opening one repeated: a run leaves behind the bloat and the
WAL churn it made, and only a probe taken afterwards says whether the ceiling its
throughputs were read against still held.

The probe loops server-side, a `do` block committing single-row inserts timed with
`clock_timestamp()`, because a client loop would measure its own round trips as well as
the server's fsync. It writes to a real table it creates and drops rather than a
temporary one, since a temp table is not WAL-logged and a probe against one skips the
WAL the run itself pays for (84,000/s against 953/s on a disk, measured side by side).

**A block that holds no rate says which of two things happened**, in the `valid`/`error`
vocabulary a rep already uses. `"the server refused the probe: ..."` is an answer about
the server — it was asked, and the message it gave is the reader's first clue.
`"the run ended before this probe was taken"` is not about the server at all: the run
was interrupted mid-wait, killed, or died at a stage, and nothing here is evidence about
what that machine could commit. Reading the second as the first is how an interrupted
run comes to look like a server that cannot commit, so the report prints the reason
beside every missing rate and calibrates against neither. A run whose probe was refused
goes ahead regardless — an uncalibrated number that says it is uncalibrated beats no
number.

Every file carries the closing block from its FIRST write, holding the never-taken
reason until a probe replaces it, so a run killed outright still says what it never did.
A stage that raises is not that case: its session is intact where an interrupted wait's
is not, so the closing probe is still taken and written into the files the finished
stages left — 75 minutes of measurement must not lose its calibration to the stage that
died. The probe issues nothing from a `finally`, either: a statement sent after an
interrupted wait raises over the interruption, and what reaches the caller is then a
database error where a timeout happened, on a session stuck mid-command for everything
that follows.

**A calibration that disagrees with itself softens every bound-verdict under it**, and
it can disagree two ways. Within one probe: `range_low` and `range_high` come off the
same rounds the `cv` does, so a badly-repeating probe is already wide. Between the two
probes: the opening one can land in a different regime from the closing one, which the
volume did constantly. So the commit-budget band spans the UNION of both durable probes'
endpoints, `min(range_low)` to `max(range_high)`, and every row names which probes it
came from — a reader comparing two reports has to tell a band that widened from mid-run
drift apart from one that widened because the medium is worse.

That mechanism was built for the volume and every number in it was measured there: the
run that opened at 615 c/s has a union of 465-2,139 c/s, and 21 of its 25 commit-budget
rows print `unresolved` — right, because a run that crossed regimes halfway through
calibrates nothing measured under it, itself included. Its opening probe alone would
have called 15 of those rows `connection-bound`. Refusing costs a run that did not drift
nothing: a run whose union was 1,915-2,037 c/s against an opening 1,948-2,006 read
`client-bound` on all 25 rows either way. No share of a median prints beside the band
either: two probes are two calibrations rather than two draws of one, so when they
disagree there is no rate a share could honestly be taken against. A run with no closing
probe — killed before the last stage, or the probe refused — bands against the opening
one alone and says so.

**Each saturation rep carries its own itemised cost** in `statement_stats`, capped at
the fifteen costliest statements by server time because the tail of that list is
microsecond bookkeeping; the totals beside it are summed over all of them, capped or
not. `null` means nothing counted statements on that server, which is not the same as
nothing having run.

**A stage that compares two topologies records how it compared them.**
`stage_pooled_vs_split.json` carries `shape_connections` — one row per distinct shape,
`{shape, processes, concurrency, connections_idle, connections_busy}`, the last two
being the delta across starting that fleet and the peak while a durable body holds every
slot — plus `run_order` (the arms as they actually ran, which is what says no arm always
went first) and `skipped_pairs` (the pairs the worker bound refused, and the bound).
Empty `measurements` with non-empty `skipped_pairs` is a stage asked for more processes
than it was allowed to spawn, not a crashed run.

**A stage that measured its own working point records the measurement, not just the
number.** `stage_latency_under_load.json` carries `sustainable_rate`: the drain ceiling
it climbed from, the offer window each probe used, the rate it settled on and whether
that rate was absorbed, the first rate refused (`bracket_high_per_s`, `null` when the
ramp ran out of ceiling with everything absorbed), and every probe with its whole rep.
The rungs are fractions of `rate_per_s`, so this block is what a rung's rate means.

**A stage calibrated from another names what it inherited**, in a `calibration` block
holding the stage and measurement that became the working point, its throughput, its CV
and whether it was marked. The choice prefers a rung that measured what was asked and
repeated, so when most rungs are marked it lands on the slowest survivor and every
number in the later stage was measured at it — which the report prints under that
stage's heading.

## Which findings survive the durable regime

Everything here measures nano-tasks at saturation. django-absurd is mostly used for
durable agent tool calls — seconds to minutes per task, checkpointed, often suspended —
so most of the above answers a question that workload does not ask. A claim costing
1.41-1.56 ms is the whole story at 4,000 tasks/s and rounding error at 4 tasks/s.

Carries over:

- **What a `ctx.step` costs.** A checkpoint per tool call IS the durable shape. The
  `checkpoint_cost` stage measures it and **no finding for it is written up in this
  file** — the most durable-relevant number the harness produces is the one nobody
  recorded. Fix that before quoting anything else here at a durable workload.
- **The connection budget, `C + 2` backends per process once a body touches the ORM**
  ([Overhead](#overhead-itemised)). Not a throughput fact, and it binds precisely when
  holds are long: a durable body holds its thread, and its connection, for minutes.
- **`r_bench` bloats and `t_bench` does not** (10,000 dead tuples against 5,000 live).
  Retries and suspensions make run rows, so a durable workload compounds this.
- **Table size costing throughput.** A durable workload accumulates history by design,
  which turns retention into a throughput concern rather than housekeeping.

Does not carry over: peak tasks/s, the process x concurrency sweep, `--batch-size`, the
producer ceiling. Those answer how fast a flood of trivial tasks drains.

Not measured at all: a task that sleeps for minutes, checkpoints repeatedly, and
suspends. No stage exercises that shape, so nothing here bounds it.

## Comparing two runs: refactors, version bumps, bisection

This is what the rig is FOR — deciding whether a change cost throughput, not producing a
headline number. Copy `results/` aside, re-run `uv run python -m stages`, and compare
the two directories. The stage filenames are stable, so `diff -r` lines the runs up —
but part of what it lines up is run-to-run spread rather than the change. Read in this
order:

1. **the host block's `cluster_name`, then the `commit_ceiling_durable` blocks.** Both
   runs must say `bench-tmpfs`; a three-figure ceiling means one landed on a server with
   a disk under it, and no rate below them compares at all. A ceiling that MOVED between
   one file's opening and closing probes says the same about that file. Re-run rather
   than reading on.
2. **the structural counts** — `commits_per_task` per measurement, each statement's
   `calls_per_task` in `statement_stats`, `shape_connections`. Counts rather than
   timings, so a claim path that grew a statement shows here at a magnitude no
   throughput number could resolve, and nowhere else. `shape_connections` is exact. The
   two per-task counts are exact at ONE worker process and read low above it
   ([the multi-process arms](#where-a-ramp-is-in-the-window-the-multi-process-arms)), so
   compare them at the SAME process count — where the bias is common to both runs — and
   do not read a difference between rungs of `process_scaling` as shape.
3. **the `options` and `calibration` blocks.** Same flags, or the runs measured
   different experiments — a deeper queue is slower, so a saturation rate does not
   compare across `--tasks`. And a stage that calibrates inherits the winning rung, so a
   run calibrated at concurrency 8 and one at 16 measured DIFFERENT configurations under
   the same measurement names. That is the likeliest way to get a confident wrong answer
   out of two runs.
4. **the rank ordering** within each stage, which held across every run. A rung that
   changed places is a finding; a rung that changed by 10% and kept its place is not.

**The floor: a difference under about 12% is not evidence of anything.** Three runs of
identical code gave a median CV of 4.7% and a worst of 12.5%, and one of them sat
systematically below the other two, so there is between-run bias on top of scatter. Only
a delta clearing that floor on SEVERAL measurements at once, in one direction, is a
change. `load_before`/`load_after` are worth reading but will not explain such a delta:
the two runs whose per-rep load medians were 3.39 and 3.40 still disagreed by up to 18%
on individual rungs. A whole run's numbers moving together is usually the working point
it inherited — read the `calibration` block first.

Two `latency_under_load` reports compare rung for rung only if their ramps found the
same rate: the rungs are fractions of a MEASURED offer rate, so a run whose knee moved
measured a different experiment under the same row names. Read `sustainable_rate` before
the rows, the way `calibration` is read before a saturation table.

A partial re-run must include the stage it depends on, or it errors saying so.

## Server uptime is an uncontrolled variable

Every result records `postgres_uptime_s`, because how long the server process has been
running appears to change what you measure — and the harness does not control for it. To
take it out of a comparison, run each stage against a fresh server:

```
docker compose restart db_bench
uv run python manage.py migrate
uv run python -m stages process_scaling
```

That is deliberately not the default: nobody restarts Postgres between workloads in
production, so a cold run measures a best case rather than a representative one. The
figures that once sized this effect came off `latency_under_load`'s upper rungs, back
when they over-offered, so nothing here supports a magnitude for it — only the two
observations under
[Three consistency knobs, measured and refused](#three-consistency-knobs-measured-and-refused),
which point opposite ways.

## The five variables

`db_bench` in the root `compose.yaml` takes `BENCH_SHARED_BUFFERS` (1GB),
`BENCH_MAX_CONNECTIONS` (100), `BENCH_TMPFS_SIZE` (4g), `BENCH_CPUS` (unset) and
`BENCH_MEMORY` (unset), each spelled `${VAR:-default}` the way `PGPORT_BENCH` already
was.

The defaults were picked for the reference machine — 14 cores, 24 GB, an 11.7 GB Docker
VM — and 1 GB of shared buffers plus a 4 GB tmpfs is about 5 GB of that VM. Nobody has
run this on a 4 GB VM, and that is the point: the combination cannot fit one, so
Postgres would either refuse to start or die when the tmpfs filled. A rig meant to run
on more than one machine cannot hardcode the one it was built on.

**The 4g tmpfs is headroom, not a requirement of the data.** Measured: the database
itself is 69 MB, and two full `worker_knobs` runs used 355 MB. 4g covers a full
eight-stage run's WAL; `512m` is comfortable for single-stage work. That is why the
README tells a small machine to lower it rather than declaring itself unsupported.

**A too-small tmpfs fails late and there is no preflight check for it.** The server
starts, the run proceeds, and Postgres dies partway through on a write error, having
burned the run. Nothing in the harness reads the tmpfs size — that check is not built.
If it ever is, it belongs at the top of `stages.main`, before the first measurement,
where the cost of being wrong is a second rather than forty minutes.

**`BENCH_CPUS` / `BENCH_MEMORY` are wired, not tuned.** They exist for deferred
CPU-allocation work. Nothing here has measured what a container CPU limit does to any
number in a results file, and nothing in this directory may claim it has.

### `0`, not empty, is how compose spells "no limit"

`cpus: ${BENCH_CPUS:-}` looks like the natural way to write "unset" and breaks the file
outright:

```
error while interpolating services.x.cpus: failed to cast to expected type:
strconv.ParseFloat: parsing "": invalid syntax
```

At `${BENCH_CPUS:-0}` compose omits `cpus` from the rendered config entirely, which is
Docker's own "unlimited" — verified with `docker compose --profile bench config`, both
unset and with the variable exported empty (`:-` substitutes on an empty value as well
as an absent one, so both land on 0). Getting this wrong is silent in the direction that
matters: a literal `cpus: 0` in the file would not throttle the server, but a wrong
guess in the other direction fails to start a container nobody was watching.

## What is deliberately not a variable

- **`synchronous_commit`, `fsync`** — durability is the experiment's premise. Every task
  rate this rig prints is a commit rate in disguise, so a variable here invites someone
  to benchmark non-durably by accident and publish the number, which is exactly the
  unlabelled claim the harness exists to refuse. Hardcoded on. The tmpfs already makes
  fsync cost a memcpy; that is a change of medium, recorded as one, not a change of
  request.
- **`shared_preload_libraries`, `pg_stat_statements.track`** — the measurement
  instrument. Runs taken with and without the tracking are not comparable, so it is
  always on and never a flag. `all` rather than `top` because Absurd's claim path runs
  inside PL/pgSQL and `top` would record the function call while hiding every statement
  in it.
- **`cluster_name`** — provenance. It has to describe what the server IS (`bench-tmpfs`,
  the only thing in a results file saying the data directory was RAM), not what someone
  passed on the command line. `report.describe_storage_medium` keys its "not for
  publishing" warning off this exact name.

## Three consistency knobs, measured and refused

Autovacuum off per queue table, a `checkpoint_timeout` beyond a run's length with an
explicit inter-rep `CHECKPOINT`, and an `ANALYZE` between preload and workers. All three
were built, run, and **refused on the numbers**; nothing in the harness applies any of
them, which is why no results file records them.

Method: six `worker_knobs` runs at `--max-workers 10` on `db_bench`, three of them
UNCHANGED baselines and one per knob, judged on the two things a knob claims to improve
— the within-rep drain profile (least-squares trend over a rep's 25 equal-count
`profile_slices`, read as total fractional drift across the drain) and the cross-rep
`cv`. The three baselines set the noise bracket: mean cv 3.4/6.3/9.0%, mean slope
+14/+17/+33%, mean `profile_cv` 9.4/9.2/13.7%. A knob landing inside that bracket
measured nothing.

**Autovacuum off: measurably worse, and the interference it removes is load-bearing.**
Mean slope +60% and mean `profile_cv` 20.2% — the slope above every baseline on every
rung of the concurrency ladder (78/94/68/50/32% against the worst baseline's
43/29/28/21/16%), so five independent measurements agreeing, not one outlier. Every rep
accelerated monotonically through its own drain: `concurrency_2` slices ran 173 → 492
tasks/s inside one rep against 211 → 331 with autovacuum on, and the ladder's medians
came in 10-20% LOW. Cross-rep cv did tighten (3.7% overall, 1.1-1.6% on the low rungs)
but sits inside the baseline bracket, so the offer is a measured within-rep loss for an
unmeasurable cross-rep gain. The premise was sound — `r_bench` really does reach 10,000
dead tuples against 5,000 live, 24 autovacuums fired across the six runs, `t_bench`
peaked at 23 — so what the numbers say is that autovacuum's mid-rep work is what keeps a
drain flat rather than what disturbs it. The mechanism was not isolated; the ANALYZE arm
below rules out drain-start statistics being the difference.

**No checkpoint mid-run: the interference is not there to remove.** On this server a
whole run's checkpoints write ~2.7 MB — `pg_stat_checkpointer` after an unchanged run
reads 3 timed checkpoints and 337 buffers written, to RAM, paced over minutes. So there
is nothing for a rep to pay for, and the arm agrees: mean cv 9.7%, slope +30%,
`profile_cv` 11.7%, every figure inside the baseline bracket. The arm did do what it
claimed (`num_timed` 0 across the run, 24 explicit checkpoints, WAL down from 256 MB to
96 MB), so this is a measurement of the knob and not of a knob that failed to apply.
Both halves are refused together: nothing that writes 2.7 MB to a tmpfs can be worth a
compose change plus a step between every rep.

The tmpfs hazard did not materialise either way: `/var/lib/postgresql` peaked at 356 MB
of 4 GB across all six runs, WAL included. That headroom is bounded by `max_wal_size` (1
GB, unchanged) and not by the timeout, which is why raising the timeout alone was safe
to test.

**ANALYZE after the preload: no measurable effect.** Mean cv 5.3%, slope +16%,
`profile_cv` 9.1% — inside the baseline bracket on all three, at its good end. A step
that costs a rep something and buys a number nothing can distinguish from an unchanged
run is not worth keeping. Rate mode therefore never needed its own decision: the mode
with a preload to describe showed nothing, so nothing was carried to the mode without
one.

**The interference that dwarfs all three is between runs, not inside one.** The baseline
run taken FIRST — on a server up an hour but idle since its migrate, so no query on it
had ever run — measured 30-35% below two later unchanged repeats that agreed within 8%
(`concurrency_8` 587 against 936 and 870; `concurrency_4` 432 against 641 and 598), and
the winning batch size changed from run to run. That is the opposite direction from the
uptime drift recorded under
[Server uptime is an uncontrolled variable](#server-uptime-is-an-uncontrolled-variable),
where a long-loaded server read SLOWER than a restarted one, so idle uptime and loaded
uptime are evidently not the same variable. One observation each way is not a rule; it
is where the remaining leverage is, and no knob in this section touches it.

Two mechanics worth knowing before re-running any of this: table `reloptions` survive
`TRUNCATE`, so an `ALTER TABLE ... SET (autovacuum_enabled = false)` applied by hand
holds for a whole run; and `checkpoint_timeout` is SIGHUP-only, so `ALTER SYSTEM` plus
`pg_reload_conf()` tests it without the restart that would empty the tmpfs.

## Recording is what makes them worth configuring

Configurable-but-unrecorded is worse than hardcoded: two results files would differ for
reasons nothing in them explains. So `host.collect_host_context` records
`shared_buffers` and `max_connections` on every measurement, and
`report.describe_server_resources` prints them, mixed-valued the way a git SHA is when a
directory holds stages from two servers.

**Read off the server, never out of the harness's environment.** The variables size the
container at `up` time; the process taking the measurement need never have seen them,
and a server already running answers for what it was actually started with rather than
for whatever the current shell happens to export.

### The container-limit gap, and the choice made about it

Container CPU and memory limits are invisible to SQL. There is nothing to query, so
there are only bad options and one acceptable one.

`host.py` records `cpu_count` from `os.cpu_count()`, and that number is the HOST's — it
is the right number for what it describes, because driver, producer and every worker
process run on the host, and the `process_scaling` ladder is derived from it. It is the
wrong number for the server: limit the container to 4 CPUs and `cpu_count` still
reads 14. So the report labels it `host cpu count` and gives the server its own line,
and `requested_container_cpus` / `requested_container_memory` carry `BENCH_CPUS` /
`BENCH_MEMORY` when the harness's environment has them, printing `unknown` when it does
not.

`unknown` and not `unlimited`: an absent variable means nobody told this process, not
that no limit exists — whoever ran `docker compose up` may have set one in a shell the
harness never saw. The field name says `requested` for the same reason. A requested
limit is the weakest of the fields in that block, and it is still strictly better than
reporting host cores as though they described the server.
