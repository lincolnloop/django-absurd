# Truthful harness, and the admin at volume

## Goal

Two things, one spec because they share a deadline and a machine.

1. **Truthful harness.** Every number `benchmarks/` publishes traces to one dated run on
   a tree with no known measurement defect. Today the docs carry numbers measured with a
   spawn bias they themselves document, and the harness's most durable-relevant stage
   has no written finding at all.
2. **Close the `loadtest/` gap.** Three capabilities the archived harness had and this
   one lacks: a clone seeder, an admin probe, and a suspension probe. Recovered as
   `archive/loadtest-harness` (`3b4ac82bad087a3a24d40be81aebfd350f65646f`).

## Success criteria

- Every **multi-process** figure is replaced by one measured on a windowed, concurrently
  spawned fleet, and names the run that produced it. Single-process figures are KEPT and
  the new run is added to their range — the spawn bias is absent at one process
  (`benchmarks/CLAUDE.md`), so discarding those ranges would trade four runs of evidence
  for one.
- Every restated figure carries its cv and the ~12% between-run noise floor
  `benchmarks/CLAUDE.md` establishes. A one-sample run supports rank orders and ratios
  above that floor. It does not support a point estimate, and the docs must not read as
  though it does.
- `benchmarks/CLAUDE.md` carries a `checkpoint_cost` finding. It has none today.
- Four questions answered, each with a number and a method: corrected multi-process
  rates; is the 2.45x table size or queue depth; what a checkpoint costs; does the
  connection budget hold when bodies hold their threads.
- Someone with the repo can seed millions of rows and click through the admin at that
  volume, following a README, without reading harness source.
- No measurement defect documented as unfixed. A defect is fixed or the number is gone.

## Non-goals

- Publishing tuning advice. Upstream owns process-vs-concurrency guidance. The harness
  bisects OUR changes; it does not tell users how to size a fleet.
- Benchmarking the admin fix. Ordering by a `uuidv7()` pk is a plan-shape claim, settled
  by `EXPLAIN`, not a stopwatch. The admin project exists so a person can SEE volume,
  not to certify a latency.
- **Asking whether processes beat threads for a durable body.** The durable workload's
  body is dominated by a sleep, so both shapes complete the same rounds in the same wall
  time whatever the shape and the ratio is ~1.0 before the run starts. That is a
  property of the workload, not of the library. The durable arms earn their place by
  exercising the CONNECTION BUDGET under real hold times; they are not a throughput
  comparison, and they run at the 2 s default rather than 30 s because the sleep buys no
  signal.
- **A timed suspension stage.** Whether a durable sleep releases its worker slot is
  settled deterministically by a test, not by a rate. See the component note below.
- Shipping any of this. Nothing here reaches a wheel.

## What is untrue today

**Spawn bias.** `start_workers` blocked per child while the preload sat queued, so above
one process the fleet started inside the measured drain. `benchmarks/CLAUDE.md` puts it
at 7-15% on `split_8` and biases `commits_per_task` low. Fixed on a branch, unmeasured.

Worse the more processes.

**The window is still not taken on a database mark.** Concurrent spawn shrinks the
stagger; it does not remove it, because children still reach readiness a few hundred
milliseconds apart. `benchmarks/CLAUDE.md` documents `commits_per_task` and
`calls_per_task` reading low above one process for exactly this reason and names
windowing as the fix. Only `size_vs_depth` passes a mark today, and it captures one
BEFORE the fleet starts, so it excludes ballast rather than stagger. Until a mark is
taken after `start_workers` returns, that defect stays true and no run can close it.

**`process_scaling` mixes two effects.** Ladder is `max(4000, 2000 * count)`, so rungs
drain 4,000-20,000 tasks: scaling confounded with a depth penalty. Report prints
`CONFOUNDED:`. Honest, not fixed.

**`checkpoint_cost` has no finding.** The 4.16x figure lives in a merged PR body,
nowhere a reader would look. It is the stage whose shape most resembles durable agent
work — a `ctx.step` per tool call.

**Every published rate is nano-task.** Bodies finish in microseconds and touch no ORM.
The primary use case is durable agent tool calls: seconds to minutes, checkpointed,
often suspended.

## Scope

### In

**Phase 0 — land what exists.** Three commits on branches, one flake fix, one required
check.

**Phase 1 — make the run's own trust conditions true.** Window saturation reps on a
database mark taken after the fleet is up; build the clone seeder. Settle the suspension
question with a deterministic test rather than a stage.

**Phase 2 — the run.** One pass, all stages, `db_bench`, on mains under `caffeinate`,
env-stamped.

**Phase 3 — restate.** Findings rewritten from that run. Defect notes deleted where the
defect is gone.

**Phase 4 — admin at volume.** Dev-only Django project, seeded, clickable, with timed
HTTP arms and `EXPLAIN` dumps.

### Cut, with reasons

- **`load_barrier` port.** Bug it found is fixed and guarded by
  `tests/core/test_worker_run_refill.py`. Porting an instrument to re-find a closed bug
  is cost with no finding attached.
- **CPU pinning.** `benchmarks/CLAUDE.md` puts median between-run cv at 4.7% and worst
  at 12.5%, and tells the reader to treat a difference under ~12% as noise. Every claim
  the harness makes is a rank order or a ratio well above that floor. Pinning narrows a
  band no finding rests on.
- **"All measurements run on truncated tables".** Stale note. `measurement.py` truncates
  before every rep already.
- **Admin stages inside `stages.py`.** Different question, different consumer, different
  output. Coupling degrades both.
- **Any new Postgres service for the admin.** Only `db_bench` is tmpfs; the plain `db`
  service the test suites already use is a real data directory. The admin corpus is its
  own DATABASE on that existing server, so there is no third service, no new port, and
  no new volume.
- **`benchmarks/` as an importable package.** Path manipulation works and nothing
  consumes it as a library.

### Deferred, named so they are not lost

- Retry storms; cleanup keep-up. Design after the run — retention as a throughput
  concern depends on what `size_vs_depth` says.
- Suite speed. `bench_harness` is the matrix's longest job — 248 s of pytest on a runner
  against 139 s locally. Real, not blocking truthfulness.
- `worker_knobs` durable arm. Decide once durable `pooled_vs_split` is in hand. That
  ladder is where a concurrency recommendation would come from, so it is the one stage
  where a durable number could change user-facing advice.
- Multi-queue routing under load. The archive ran four queues; this harness runs one.
  Genuinely unexercised, low value against the rest.

### Out of scope, needs its own spec

**A worker re-claims its own in-flight run.** Measured: at `concurrency=2`,
`claim_timeout=1`, one body held 2.5 s ran twice, concurrently, on the same worker,
`attempts=2`, reported `SUCCESSFUL`. Inside the documented at-least-once contract, so
not a contract violation — but the process is alive and holds the run in memory, so
self-redelivery buys nothing and costs a duplicate side effect. It fails open and
silently. A library change, not harness work.

## The four questions the run settles

| Question                                                       | Stage                                                                             | What makes the answer trustworthy                                                                                                                                                      |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Corrected multi-process rates                                  | `process_scaling`, `pooled_vs_split` split arms, `latency_under_load` calibration | Fleet starts before the measured window; saturation reps windowed on the database clock                                                                                                |
| Is the 2.45x table size or queue depth                         | `size_vs_depth`                                                                   | Ballast drained before the measured tasks exist; every metric windowed on a mark taken after it; each rep records live tuples, dead tuples and bytes rather than trusting the arm name |
| What a checkpoint costs                                        | `checkpoint_cost`                                                                 | Already built; needs a written finding, not new code                                                                                                                                   |
| Does the connection budget hold when bodies hold their threads | `pooled_vs_split` durable arms + the shape backend probe                          | Backends sampled while bodies run, not only across fleet startup; mutation-checked against an ORM-free body                                                                            |

## Component: the suspension question, answered by a test

**The question.** A sync task's durable sleep hops to the worker's event loop while the
body sits in a thread of a pool sized to `--concurrency`. If that thread stayed parked,
N sleepers would hold N of C slots and everything behind them starves.

**Why this is NOT a timed stage.** A run's own state cannot witness the failure. The
SDK's sleep sets `state = 'sleeping', claimed_by = null, claim_expires_at = null` before
`SuspendTask` propagates (`django_absurd/migrations/0001_initial_0_5_0.sql:1160-1168`),
so a parked thread leaves exactly the same row a released one does. Counting sleeper
runs by state therefore proves nothing, and a stage built on that count would report a
tautology as corroboration.

**What already covers most of it.** `tests/core/test_durable.py` drives a durable sleep
through the real sync bridge and asserts `drain()` returns the sleeping state — and a
parked thread would hang that drain at the bridge timeout, so the test passing IS
evidence the thread returned. `tests/core/test_worker_run_refill.py` covers a worker
continuing to claim while a slot is occupied.

**The uncovered case, and its whole cost.** Neither exercises N sleepers against C slots
with a quick task queued behind them. That is one deterministic test: concurrency 1, two
sleepers suspended in a long sleep, one quick task enqueued after, asserting the quick
task reaches its completed state. The observable is positive and binary — the quick task
ran, so the sleepers held no slot — and it needs no clock, no arms, and no rate.

## Component: clone seeder

**Why not enqueue.** `benchmarks/` reaches volume by enqueueing tasks one at a time. The
archived clone did 250k tasks + 375k runs in 21 s: write template rows through the real
API, then clone them server-side in SQL.

**The drift guard is the load-bearing part.** Server-side cloning writes queue tables
directly, which means it encodes their shape. When upstream changes a column the clone
must fail loudly, not write plausible-looking wrong rows. The archive guarded this by
checking `information_schema` against what the clone knows. Keep that; a silent drift
here poisons every number taken on seeded data.

**Consumers.** The admin project (Phase 4). Also makes retention and table-size
questions answerable in an afternoon instead of requiring a harness rebuild — which is
the argument that survived the review's cut of everything else in this area.

**Not a stage.** Seeding is a setup step, not a measurement. It records how long it took
and what it wrote, and stops there.

## Component: admin at volume

**The consumer is a person.** Not a report. Someone starts a stack, seeds millions of
rows, opens the admin, and clicks. That is the deliverable; the timings come along
because they are cheap once the stack exists.

**Placement: dev-only under `benchmarks/`.** Not shipped in any wheel, not in the
examples CI matrix. `examples/` was considered and rejected: `examples/web` is a
tutorial whose README is a walkthrough and whose suite is a required check, and a volume
seeder does not belong in a teaching example.

**Rot control.** Dev-only code with no check on it rots. Its tiny-N tests live in
`tests/benchmarks`, which is already a CI job (`bench_harness`), so a required check
exercises the seeder and the probe at a few hundred rows on every push. The project is
free to be rough; it is not free to be broken.

**Persistence, with no new service.** Only `db_bench` is tmpfs; the plain `db` service
the suites already use is a real data directory, and a seeded corpus that evaporates on
restart is worse than no corpus. The corpus is its own DATABASE on that existing server
— no third service, no new port, no new volume, and nothing extra started by a bare
`up -d`.

**What it measures.** Per admin arm: wall-clock cold and warm, query count, and
`EXPLAIN (ANALYZE, BUFFERS)` dumped to a file. Arms cover the changelists that order by
pk and at least one filtered view, since a filter changes the plan and the pk ordering
does not help it.

**What it must not claim.** These are one machine's page latencies on one seeded corpus.
Useful for "the admin is usable at 3M rows" and for spotting the next bottleneck. Not a
benchmark anyone should quote as a property of the library.

**What it must not re-measure.** The pk-ordering fix is already measured at 3M runs, in
the commit that makes it: ~520k pages and 84.7 s cold before, an `Index Scan Backward`
at 33 buffers with the same latency cold or warm after. The probe's arms exist to find
the NEXT bottleneck — filtered views, search, foreign-key lookups — not to re-establish
that.

## Branch topology

Four branches, because three tasks have a genuine "the previous thing is MERGED"
precondition and a single branch cannot satisfy its own.

1. **`benchmarks__truthful-harness`** — this spec, the plan, three cherry-picked
   commits, the flake fix, the windowing fix, the seeder, the suspension test. All
   `benchmarks/`, `tests/benchmarks/`, `tests/core` and docs. Nothing reaches a wheel
   and `test:` is dropped from the changelog, so bundling costs no changelog fidelity.
   Cherry-picks, not merges: `benchmarks__size-vs-depth` was cut off
   `benchmarks__durable-workload`, so merging both would drag one commit through twice.
2. **The required-check change**, after (1) merges. Requiring `bench_harness` while the
   flake fix is still in review would block the PR that fixes it. Not a code change — a
   ruleset edit.
3. **The restated docs**, after the run. The run needs a merged, defect-free tree, so
   the docs written FROM it cannot share a branch with the code it measures.
4. **The admin project**, after `fix__admin-changelist-order-by-pk` merges. Its probe
   arms read changelists ordered by pk, which is that branch's change. New surface — a
   Django project, a seeded database, a probe — and bundling would hold the harness
   fixes hostage.

**`fix__admin-changelist-order-by-pk` stays its own PR and can merge first.** It is the
only user-facing change in the pile — a `fix` that renders in the changelog, carrying a
semantics note that `-task_id` orders by creation where `-first_started_at` orders by
execution start, and those differ for deferred tasks. Folding it into a harness branch
buries the one commit a user needs to read.

## Testing

Every probe is driven through its command line by `tests/benchmarks` at a handful of
tasks per stage, the idiom already there. That suite is the only thing standing between
this harness and rot.

**The standard a new test must meet.** These tests exist because the measurement code
had real bugs — an over-offering rate ramp, a probe that poisoned its connection,
exactness claimed above one worker. A test that executes a stage without constraining
what it measures is worse than none, because it reads as coverage. Each new probe's test
asserts the SHAPE of its result — which rounds counted, which were discarded, which arm
won — and must fail when the property it names is broken. Mutation-proved, not assumed.

**Ordering assertions are positive.** Assert the row order and the observable that can
only hold if the behaviour is right. Never assert absence.

## Risks

**The run is one sample on one laptop.** Mitigate by stamping the environment, running
on mains under `caffeinate` (this machine has slept mid-run before, and `perf_counter`
does not count it, so a napped arm reads fast and plausible), and reporting cv per arm.
A finding whose cv is wide is reported as wide, not averaged into confidence.

**Seeded rows are not lived-in rows.** A cloned corpus has uniform ages and a synthetic
`claimed_by` spread. Say so wherever a number rests on it.

**The suspension probe could measure its own scheduling.** Two arms sharing one worker
start-up and one warm-up task control for that; the state count catches it if they do
not.

**Restating docs invites drift.** Each finding names its run. A number without a run is
a defect, catchable by reading for it.
