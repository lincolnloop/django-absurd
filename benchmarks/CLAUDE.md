# `benchmarks/` — the reasoning behind the rig

[`README.md`](README.md) is the actions: start the server, run the stages, read the
report. This file is everything behind them — what the harness measured and how much of
it to believe, the measurement model, the results-file schema, and why the server is
configured the way it is. Rationale belongs here; the README stays short and stays about
what a human does.

## How to read every number in this file

All figures are from the tmpfs runs with the macOS indexer suppressed, on one 14-core
laptop at `--max-workers 10 --reps 3`. Runs `warmup` and `b` are the clean pair (per-rep
load medians 2.54 and 3.38); run `a` ran at load 6.70 with peaks to 15.01 while the
indexer held 1-1.4 cores, and reads 6-10% low. The offer-rate and queue-depth figures
come from three later runs on the same server in the same session — two of
`latency_under_load` after it was changed, and one `worker_knobs` at `--tasks 20000`.

**Every ceiling proposed during this work turned out to be the measurement environment
rather than Absurd: disk fsync, then one connection's commit rate, then CPU. The harness
has not found Absurd's limit — only its own.** That governs how any figure here should
be read, and it is the most important thing the work produced.

Disk-era figures — anything measured with `db_bench`'s data directory on a Docker named
volume — are superseded. A few are kept below because their SHAPE is still the argument;
they are labelled, and their levels mean nothing now.

## Repeatability: rank things, do not confirm small changes

Three full runs of one commit, 25 shared saturation measurements:

    median CV      4.7%      (14 of 25 under 5%)
    mean CV        5.1%
    worst CV      12.5%

The clean pair alone: median run-to-run ratio 0.987, CV of ratios 4.7%, ratios spanning
0.88-1.15. On the disk volume the same comparison read median ratio 1.20, CV 31.7%,
ratios 0.02-1.71 — so the storage change is what bought repeatability.

**4.7% is not a precision guarantee for a single measurement.** The third run sits
systematically BELOW the other two on several of them (`workers_1` 1,066 against
1,211/1,306; `async_dispatch` 888 against 1,137/1,071), which is a mild between-run bias
and not pure scatter. Treat a difference under ~12% as noise.

Two conditions produced it and both matter: the data directory on tmpfs, and the macOS
indexer suppressed. `latency_under_load`'s upper rungs used to be the exception, at CVs
of 160%+; they were over-offered rather than noisy, and now repeat at 0.6-3.7% —
[a drain rate is not an arrival rate](#a-drain-rate-is-not-an-arrival-rate).

## Overhead, itemised

Per `noop_sync` task (empty body), worker side:

    commits per task     1.85 - 2.67   varies with SHAPE, not just with batching
    row updates          ~4            ~2 on r_bench, ~2 on t_bench
    backends per worker  2             SDK connection + Django ORM, at ANY --concurrency

From `pg_stat_statements` with `track=all`, top-level rows only:

    wall 2.82 ms/task = server 1.64 ms + client 1.18 ms

    1.00 calls/task  1.4718 ms  claim_task            (top level)
    1.00 calls/task  0.9058 ms    candidate CTE       (nested)
    1.00 calls/task  0.2684 ms    cancellation scan   (nested)
    1.00 calls/task  0.0990 ms  complete_run          (top level)

- WAL durability was 71% of a full run's active backend time on the volume, which is why
  every rate here is read against a commit ceiling rather than on its own.
- Claiming costs ~15x completing. All per-task database cost is acquiring work.
- 42% of a task's wall time is client-side Python. That is what a GIL serialises, and it
  is the mechanism behind processes beating in-process concurrency.
- The cancellation scan is 18% of the claim, on every claim.

## Throughput: 4,441 tasks/s, and no knee on either axis

    shape    tasks/s   commits/task
    10x16    4,441.7   1.85          <- best measured
     7x16    4,182.3   2.03
     5x16    3,727.7   2.13
     2x16    2,292.8   2.13
     8x1     2,028.3   2.67

Both axes still paid at the top of the sweep:

    concurrency at 1 process:     1->16 gives 360.6 -> 1,231.1 tasks/s   (3.4x)
    processes at concurrency 16:  1->10 gives 1,211.5 -> 4,441.7         (3.7x)
    diagonal at equal total:      4x1 beats 1x4 by 2.08x; 8x1 beats 1x8 by 2.24x

10x16 is 160 concurrent tasks on 14 cores and still the top cell, still climbing. In
particular in-process concurrency does NOT saturate around 3x: 3.4x at concurrency 16
with no rung flattening.

Advice that survives: scale with PROCESSES, concurrency around 16, batch the claims.

## Queue depth costs throughput

    within-rep drift, 46 reps across two runs: median +13.3%, positive in 37 of 46

Throughput RISES as the queue drains. This was the original hypothesis; it was recorded
as refuted off a single disk run whose sign was inverted by storage noise, and it is
confirmed on RAM across two runs. Nothing cumulative can make a drain faster, which is
what makes the sign readable at all.

Consequence: a saturation measurement averages a curve, so its single number depends on
the starting depth and is not comparable across `--tasks` values. `profile_slices` is
what makes that visible.

**How much depth costs, measured directly.** The whole `worker_knobs` concurrency ladder
at `--tasks 20000` against the four baseline runs at 5,000, same machine, same session:

    rung             5,000 (4 runs)      20,000    ratio
    concurrency_1    333-361             147.0     0.42
    concurrency_2    320-376             154.5     0.44
    concurrency_4    514-619             267.4     0.47
    concurrency_8    809-913             415.7     0.47
    concurrency_16   1,096-1,231         716.4     0.60

Four times the backlog costs 40-58% of the throughput on every rung — an order of
magnitude more than the +13.3% within-rep drift, because the drift is measured over the
part of the drain that is left rather than across two starting depths. The within-rep
drift grows to match: at 20,000 the first slice reads 0.46 of the median slice, against
0.79-1.01 at 5,000, so a rep at that size climbs by more than 2x across its own drain.

That is why `--tasks` is a comparability key and not a size knob, and it is the measured
answer to raising `SATURATION_TASKS`:
[the window is long enough already](#saturation_tasks-stays-at-5000).

## A drain rate is not an arrival rate

A saturated fleet has work waiting at every claim; a paced one has to keep up in real
time, claim polls that find nothing included. So the two rates are different quantities
and the drain one is the larger — which is what `latency_under_load` used to get wrong.
Its rungs were fractions of the `process_scaling` drain ceiling, and from `rate_50pct`
up they offered more than the fleet could absorb. Four runs on RAM, 7 workers x
concurrency 16, whose drain rate measured 4,182-4,231 tasks/s:

    rung          offered/s      p50 latency      throughput/s    cv
    rate_25pct    1,013-1,058    15-18 ms         1,013-1,057     3-65%
    rate_50pct    2,026-2,116    30 ms - 2.6 s    1,314-2,093     2-167%
    rate_75pct    3,039-3,174    35-86 SECONDS    529-718         10-18%

The 75% rung is the shape of the error. Offered 3,100/s, the same fleet that drains
4,200/s completed 620 — a seventh of it — because a queue that deep is a different
workload: the tables are 150,000 rows of unfinished work, the producer is competing for
the cores, and every claim scans more. Its p50 is then a function of how long the window
was, not of the system. The 50% rung sat right on the knee and came out bimodal: reps at
31 ms beside reps at 1.3-2.6 s in the same measurement, which is where the 167% CV came
from.

It also aborted runs. Three of those four never recorded a `rate_90pct` row: the drain
after that offer outran `RATE_TIMEOUT_S`, and `MeasurementTimeoutError` reaches no
handler, so the invocation died there — which is why every stage file in those three
runs is missing its closing commit ceiling.

**So the stage measures the arrival rate itself.** A ramp offers for 20 s at a time,
climbing from a tenth of the drain ceiling by half again a step, and stops at the first
offer the fleet did not absorb; the highest one it did absorb is the working point, and
the rungs are fractions of THAT. The whole ramp is recorded probe by probe under
`sustainable_rate`, because a working point nothing can see is a number a reader cannot
argue with. On this server it found 2,142 tasks/s sustainable and 3,213 refused against
a 4,231 drain rate — the knee is around half the drain rate, which is exactly why the
old 50% rung was the bimodal one. What the rungs read after the change:

    rung          offered/s   p50 latency   cv     marks
    rate_25pct        535.5      9.7 ms     3.6%   none
    rate_50pct      1,070.7     15.3 ms     0.6%   none
    rate_75pct      1,605.7     23.1 ms     3.7%   none
    rate_90pct      1,926.7     29.9 ms     1.8%   none

A monotonic latency curve against offered load, which is what the stage is for, and the
first run of it with nothing marked. It cost ~15 minutes against 17-24, because the
rungs that took the longest were the ones carrying no information.

**At a matched offer rate the new rungs reproduce the old ones.** 1,071/s reads 15.3 ms
here against 15.2-15.9 ms at 1,046-1,058/s before, and 1,606/s reads 23.1 ms against
22.1 ms at 1,433/s. The change removed rows, not signal.

**And the stage now repeats.** A second run of it, back to back, absorbed and refused
the same probes and read 10.5/16.1/24.0/28.8 ms against 9.7/15.3/23.1/29.9 — ratios of
0.96-1.08, inside the rig's own 12% floor, every row unmarked in both. Both runs read
the same `stage_process_scaling.json` back, though, so what repeated is which offer the
fleet absorbed and not the drain ceiling the ramp climbs from: a run whose
`process_scaling` lands 12% elsewhere probes 12% elsewhere too, and its rung rates move
with it. That is what makes `sustainable_rate` the first thing to compare between two of
these reports.

**The knee is bracketed, never resolved.** Probes are 1.5x apart, so the ramp says the
rate is between 2,142 and 3,213 tasks/s and nothing here refines it; the working point
is deliberately the low end of that bracket. And a probe offers for 20 s while a rung
offers for 60, so a rate that only diverges over a longer window survives the ramp — the
guard below is what catches that, on the rung itself.

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
a 120,000-task one. In steady state the backlog is one Little's-law-worth of in-flight
work at each end, so the growth cancels to a fraction of a percent — under 0.2% at 160
concurrent tasks over a 60-second offer — while a fleet absorbing 95% of its offer reads
5% and the refused 3,213/s probe read 4,472 tasks of growth on 64,000 offered. Under 8
tasks of growth the share is not consulted, so a smoke-sized offer cannot trip it on
scheduling jitter.

5% rather than tighter, on evidence: `rate_90pct` above ended two of its three reps with
backlogs of 1,149 and 2,345 tasks against ~52 at the midpoint, 2-4% of the offer, and
all three reps still agreed on the p50 within 1.8% — the rep with no spike at all read
30.6 ms against 29.5 and 29.9. A 2% limit would have thrown that measurement away for a
transient that demonstrably did not reach the number.

The blind spot is the other side of the same threshold: a fleet absorbing 96% of its
offer diverges slowly and this guard will not say so. That is part of why the top rung
is 90% of a measured knee rather than 100%.

## `SATURATION_TASKS` stays at 5,000

5,000 was sized when `concurrency_1` drained 67 tasks/s, which made a 75-second window.
On RAM the same rung drains 360/s, so the window is now ~14 s and the fastest rung using
the constant is 4.5 s — short enough to ask whether what it measures is mostly the fleet
getting going. Tested by running the whole ladder at `--tasks 20000` and comparing it
with the four baseline runs:

    what moved            5,000 (4 runs)     20,000
    medians               (the table above)  0.42-0.60x
    cross-rep cv          0.7-7.3%           1.9-4.5%
    within-rep profile_cv 5.6-11.0%          10.9-22.4%
    slowest rep           14.7 s             148.1 s

So a longer window does not tighten anything: every CV at 20,000 lands inside the
bracket the same rungs already spanned at 5,000, and the within-rep profile gets noisier
because the drain now crosses more of the depth curve. What it does change is the
experiment — the medians move by more than half, which is depth and not precision — at
four times the tasks and up to ten times the wall clock. **Rejected.**

The premise behind the question is also wrong for these rungs. Worker startup is outside
the measured window by construction: `start_workers` waits for each child's readiness
line and `measure_phase` opens afterwards, and the p10-p90 trim then drops the first
tenth of the completions. The data agrees — slice 0 sits at 0.79-1.01 of the median
slice across every single-process rung, and the SHORTEST drains show the LEAST deficit
(`concurrency_16` and `batch_32`, 4.5 s, read 1.00-1.07) while the longest shows the
most (`concurrency_1`, 14.7 s, reads 0.79-0.90). Dropping slice 0 moves a rep's fitted
drift by under 3 points. A short drain here is not a ramp; it is a shallower queue.

**Where a ramp IS in the window: the multi-process arms.** `split_8` (8x1, 5,000 tasks,
2.0 s) reads slice 0 at 0.22-0.23 of its median and climbs 482 → 2,440 tasks/s across
its own drain, while the pooled arm of the same pair (1x8, 6.1 s) sits flat at
0.97-0.99. The contamination tracks the PROCESS COUNT and not the window length: eight
fresh interpreters warming their claim path up inside a two-second drain. `split_8`
publishes 1,924-2,028 tasks/s against settled slices of 2,300-2,500, so it understates
by ~17%, and `pooled_vs_split`'s split/pooled ratio is understated with it — its
direction is not in doubt either way, and it would only grow.

That is a real defect and a bigger constant is the wrong fix for it: it would dilute the
warm-up only by measuring a deeper queue for both arms of a pair, at four times the
cost, while degrading every single-process rung it touches. What it wants is a windowed
analysis — a saturation rep capturing the database clock the way a rate rep already
does, and counting completions from there — which moves every saturation figure in this
file and is not done here.

## Storage: two regimes on a volume, and none on RAM

Ten arms each, interleaved so the media alternate, one configuration throughout:

    disk (volume)   618 466 464 679 462 476 590 579 776 1,698   median   584 c/s  cv 54.7%
    tmpfs (RAM)     46,356 ... 187,643                          median 56,659 c/s

The volume was the single largest source of irreproducibility here: its durable commit
rate sits in one of two regimes about 3.5x apart and nothing the harness controls
selects between them. RAM is not better storage; it is storage taken out of the
measurement. At 56,659 commits/s and ~2.5 commits per task the database could sustain
~22,000 tasks/s against the ~4,400 this rig actually reaches, so the commit ceiling
stops constraining anything and its own spread stops reaching the numbers below it. The
runs themselves recorded 200,000-240,000 commits/s. `synchronous_commit=on` and
`fsync=on` are unchanged: the durability request is the same one, only the medium moved.

A stage took ~14 minutes on the volume and ~4-5 minutes on RAM.

Wall-clock cost is not why the volume went, though. The reason is that no single ceiling
probe characterises a run on it: one run opened at 615 c/s and closed at 2,100, a
ceiling that moved 3.4x DURING the run.

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
ceiling is one connection's**. A worker process opens exactly two backends whatever its
`--concurrency` (the SDK's connection and Django's), so its claim traffic funnels
through one connection however many threads are behind it, while the box has several
times that capacity spread across more of them; concurrent backends share an fsync
through group commit, so that scaling is sublinear (measured once on the volume in its
fast regime: 1,953 c/s at one connection, 4,748 at four, 13,238 at sixteen — read the
shape, not the levels, and note that a single-connection ceiling is a per-run
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
own units: reps within 150 ms of each other are not called unstable however far apart
they read relatively — reps of 67/89/173 ms read as a 119% spread.

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

**`--max-workers` is the size flag for topology.** `--tasks`, `--duration` and
`--io-seconds` size the work; nothing sized the fleet, and the fleet tracked the host.
Thirty-two cores means 83 worker processes spawned across `process_scaling` alone, and
128 cores means 323. `--max-workers N` lowers the ceiling the ladder is derived from, so
a bounded ladder is still a ladder rather than one rung repeated: `--max-workers 3`
gives 1, 2, 3. The same bound caps the `poll_interval` idle probes, drops whole any
`pooled_vs_split` pair whose split arm it cannot spawn, and narrows which
`process_scaling` rung `latency_under_load` may calibrate from, so the offered rate
stays a rate that fleet actually measured. Bound both stages together: `--max-workers`
on `latency_under_load` alone reads back a `stage_process_scaling.json` measured on a
larger fleet.

A size below what a stage can measure is refused before anything runs, rather than
crashing partway or writing a number describing work that never happened: fewer than one
worker, fewer than one task, or a rate window of no length. `--io-seconds 0` stays legal
— no simulated IO is a real point on that experiment's axis.

**`pooled_vs_split` measures the diagonal the other two miss.** `worker_knobs` sweeps
concurrency at one process and `process_scaling` sweeps processes at one concurrency, so
the two ways of reaching the SAME total were never put against each other: `pooled_4` is
1x4, `split_4` is 4x1, and the same for 8. The shapes are fixed twice over — not
calibrated from an earlier stage, since configuring one arm from a winner would make the
pair unequal in a second way, and not derived from the host, because a shape that means
something different on every machine cannot be compared across machines. Three mechanics
matter:

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
- **Connection count is measured, because it is a confound.** `1x4` reaches four-way
  concurrency on two backends and `4x1` reaches it on eight, so the shapes differ in
  connection count as well as in claim path, and the report says so above the ratio
  rather than leaving it to be read as the claim path alone. Measured per shape as a
  delta across starting the fleet, so the day a pooled worker opens a connection per
  slot the same line will say the comparison is clean.

## The results files

`results/stage_<name>.json` is written by `python -m stages` and by nothing else,
rewritten atomically after every finished measurement, so a run killed at hour two keeps
everything it measured. The directory is git-ignored on purpose: a committed set would
read as django-absurd's official figures. A rendered `report-<UTC stamp>.md` lands
beside the JSON so a run and its reading stay together, stamped because a second run
would otherwise overwrite the first reading while its own JSON sat right there.

**Each file says which configuration produced it.** Beside `measurements` sits an
`options` block holding `--tasks`, `--duration`, `--io-seconds`, `--max-workers` and
`--reps` RESOLVED — an unset flag records what the run actually used, not a null to go
look up. `--max-workers` is the one nothing else recovers: unset it tracks the host, so
an unbounded ten-core run and a fourteen-core run bounded to ten write the same ladder.
`--tasks` and `--duration` stay `null`, meaning the stage sized itself — neither has one
default to name, and every measurement's `spec` carries the size it ran at. The whole
set prints on one header line, and a flag two stage files disagree about reads as
`mixed (8, 60)`, the way a mixed git SHA does. A file written before this block existed
has none and the report will not render it; re-measure rather than hand-adding one. The
same goes for one written before a measurement carried `invalid`, `unstable`, `cv` and
its rep endpoints: it records a single `flagged` the report no longer reads. Results
from before the stages were named cannot be diffed against these either.

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
`NONDURABLE_PROBE_COMMITS` 5,000 — because the non-durable rate reached ~370,000
commits/s on the same stack, where 300 commits would be timing under a millisecond. A
round read straight after a run once came back at 719/s: that was a cold session, not a
depressed server, and it is why all three probes warm.

The opening probe runs before the first measurement and the closing one after the last.
The closing probe is not the opening one repeated: a run leaves behind the bloat and the
WAL churn it made, and only a probe taken afterwards says whether the ceiling its
throughputs were read against still held.

The probe loops server-side, a `do` block committing single-row inserts timed with
`clock_timestamp()`, because a client loop would measure its own round trips as well as
the server's fsync. It writes to a real table it creates and drops rather than a
temporary one, since a temp table is not WAL-logged and a probe against one skips the
WAL the run itself pays for (84,000/s against 953/s on a disk, measured side by side).
Any of the three blocks can be `null`: the probe was refused, the run went ahead
regardless, and the report says the calibration is missing rather than dropping the
line.

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
`stage_pooled_vs_split.json` carries `shape_connections` (the backends each shape
opened), `run_order` (the arms as they actually ran, which is what says no arm always
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
   `calls_per_task` in `statement_stats`, `shape_connections`. These are exact and
   portable, so movement in them is the change itself and is worth chasing; a claim path
   that grew a statement shows here and nowhere else.
3. **the `options` and `calibration` blocks.** Same flags, or the runs measured
   different experiments — a deeper queue is slower, so a saturation rate does not
   compare across `--tasks`. And a stage that calibrates inherits the winning rung, so a
   run calibrated at concurrency 8 and one at 16 measured DIFFERENT configurations under
   the same measurement names. That is the likeliest way to get a confident wrong answer
   out of two runs.
4. **the rank ordering** within each stage, which held across every run. A rung that
   changed places is a finding; a rung that changed by 10% and kept its place is not.

**The floor: a difference under about 12% is not evidence of anything.** Three runs of
identical code gave a median CV of 4.7% and a worst of 12.5%, and the third sat
systematically below the other two, so there is between-run bias on top of scatter. Only
a delta clearing that floor on SEVERAL measurements at once, in one direction, is a
change. Check `load_before`/`load_after` too: a run at load 6.7 measured 6-10% low
against one at 2.5.

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
