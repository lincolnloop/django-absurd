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

- Every finding in `benchmarks/CLAUDE.md` and `benchmarks/README.md` names the dated run
  that produced it. No figure survives that predates the spawn fix.
- `benchmarks/CLAUDE.md` carries a `checkpoint_cost` finding. It has none today.
- Five questions answered, each with a number and a method: corrected multi-process
  rates; does processes-beat-threads survive a durable body; is the 2.45x table size or
  queue depth; what a checkpoint costs; does a durable sleep release its worker slot.
- Someone with the repo can seed millions of rows and click through the admin at that
  volume, following a README, without reading harness source.
- No measurement defect documented as unfixed. A defect is fixed or the number is gone.

## Non-goals

- Publishing tuning advice. Upstream owns process-vs-concurrency guidance. The harness
  bisects OUR changes; it does not tell users how to size a fleet.
- Benchmarking the admin fix. Ordering by a `uuidv7()` pk is a plan-shape claim, settled
  by `EXPLAIN`, not a stopwatch. The admin project exists so a person can SEE volume,
  not to certify a latency.
- Shipping any of this. Nothing here reaches a wheel.

## What is untrue today

**Spawn bias.** `start_workers` blocked per child while the preload sat queued, so above
one process the fleet started inside the measured drain. `benchmarks/CLAUDE.md` puts it
at 7-15% on `split_8` and biases `commits_per_task` low. Fixed on a branch, unmeasured.

The real mechanism is narrower than the note says, and the note is wrong about it: a
saturation rep preloads BEFORE starting the fleet, so the first child drains alone for
the whole of the others' start-up, and those completions land inside the trimmed p10-p90
window throughput is taken over. Worse the more processes.

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

**Phase 1 — build the two probes the run needs.** Suspension probe; clone seeder.

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
- **CPU pinning (#14).** Measured cv 2.3% on a quiet box. Every claim is a ratio or an
  order of magnitude. Pinning buys precision no finding rests on.
- **"Truncated tables" (part of #41).** Stale note. `measurement.py` truncates before
  every rep already.
- **Admin stages inside `stages.py`.** Different question, different consumer, different
  output. Coupling degrades both.
- **A second permanent Postgres service.** `db_bench` is tmpfs and wrong for admin work;
  the admin project gets its own service in the same compose file behind a profile, so a
  bare `up -d` still starts nothing extra.
- **`benchmarks/` as an importable package.** Path manipulation works and nothing
  consumes it as a library.

### Deferred, named so they are not lost

- Retry storms; cleanup keep-up (rest of #41). Design after the run — retention as a
  throughput concern depends on what `size_vs_depth` says.
- Suite speed (#42). `bench_harness` is 305 s on CI against 139 s locally. Real, not
  blocking truthfulness.
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

## The five questions the run settles

| Question                                           | Stage                                                                             | What makes the answer trustworthy                                                                                                                                                      |
| -------------------------------------------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Corrected multi-process rates                      | `process_scaling`, `pooled_vs_split` split arms, `latency_under_load` calibration | Fleet starts before the measured window; saturation reps windowed on the database clock                                                                                                |
| Does processes-beat-threads survive a durable body | `pooled_vs_split` durable arms                                                    | Same rounds per slot in both shapes, so the arms are comparable                                                                                                                        |
| Is the 2.45x table size or queue depth             | `size_vs_depth`                                                                   | Ballast drained before the measured tasks exist; every metric windowed on a mark taken after it; each rep records live tuples, dead tuples and bytes rather than trusting the arm name |
| What a checkpoint costs                            | `checkpoint_cost`                                                                 | Already built; needs a written finding, not new code                                                                                                                                   |
| Does a durable sleep release its worker slot       | new suspension probe                                                              | Timing ratio corroborated by a clock-independent count of sleeper run states                                                                                                           |

## Component: suspension probe

**The question.** A sync task's `context.sleep_for` hops to the worker's event loop
while the body sits in a thread of a pool sized to `--concurrency`. If that thread
stayed parked, N sleepers would hold N of C slots and everything behind them starves.
Docs say the task suspends durably and resumes later. That is the claim under test, not
a premise.

**Shape.** Two arms per workload, run one at a time so they never measure each other.

- `control` — one worker, no sleepers, drain a fixed batch of quick tasks.
- `sleepers` — same worker, same batch, with N tasks already suspended in a sleep far
  longer than the measurement window.

Both arms pay the same worker start-up and the same warm-up task before the clock
starts. The ratio is the finding: near 1 means the sleep released its slot; far above 1
means it did not. An arm that never drains fails the run rather than recording a number.

**Async twin required.** Only the sync path crosses the thread pool, so the async
sleeper is measured beside it. Without both, a finding cannot say whether the bridge or
the loop is responsible.

**Clock-independent corroboration.** While the quick batch drains, count the sleepers'
own runs by state. A sleeper in `running` holds a claim and therefore a slot; one in
`sleeping` holds neither. Record the worst moment and the least-asleep one. A timing
ratio and a state count that disagree means the probe is wrong, which is the point of
having both.

**Why this is the highest-value probe here.** Three findings now describe what long work
does to a worker: the connection budget (`C + 2` per process once a body touches the
ORM), the claim lease (a body outrunning it is redelivered), and slot occupancy. The
first two are measured. This is the third.

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

**Persistence.** Its Postgres is a real data directory, not tmpfs — a seeded corpus that
evaporates on restart is worse than no corpus. Its own service in the existing compose
file, behind a profile, so a bare `up -d` leaves it alone.

**What it measures.** Per admin arm: wall-clock cold and warm, query count, and
`EXPLAIN (ANALYZE, BUFFERS)` dumped to a file. Arms cover the changelists that order by
pk and at least one filtered view, since a filter changes the plan and the pk ordering
does not help it.

**What it must not claim.** These are one machine's page latencies on one seeded corpus.
Useful for "the admin is usable at 3M rows" and for spotting the next bottleneck. Not a
benchmark anyone should quote as a property of the library.

## Branch topology

**One harness branch**, `benchmarks__truthful-harness`, off `main`. Carries this spec,
the plan, three cherry-picked commits, the flake fix, both new probes, and the restated
docs. All of it `benchmarks/`, `tests/benchmarks/`, `tests/core` and docs. Nothing
reaches a wheel and `test:` is dropped from the changelog, so bundling costs no
changelog fidelity.

Cherry-picks, not merges: `benchmarks__size-vs-depth` was cut off
`benchmarks__durable-workload`, so merging both would drag one commit through twice.

**The admin ordering fix stays separate.** It is the only user-facing change in the pile
— a `fix` that renders in the changelog, carrying a semantics note that `-task_id`
orders by creation where `-first_started_at` orders by execution start, and those differ
for deferred tasks. Folding it into a harness branch buries the one commit a user needs
to read. Already green and independent.

**The admin project gets its own branch**, after the run. New surface — a Django
project, a compose service, a seeder — and bundling would hold the harness fixes hostage
to it.

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
