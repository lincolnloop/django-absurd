# Truthful harness, and the admin at volume

## Goal

1. **Truthful harness.** The multi-process numbers `benchmarks/` publishes are measured
   on a tree with no known measurement defect, and the stage whose shape most resembles
   durable agent work has a written finding.
2. **Close the `loadtest/` gap that matters.** A clone seeder, so anyone can put
   millions of rows in front of the admin and click. Recovered from
   `archive/loadtest-harness` (`3b4ac82bad087a3a24d40be81aebfd350f65646f`).

## Success criteria

- `commits_per_task` and `calls_per_task` are exact above one worker process, and the
  note saying they are not is gone because a run shows it gone.
- Multi-process rates in `benchmarks/CLAUDE.md` and `benchmarks/README.md` come from a
  windowed run and name it. **Single-process figures are kept** and the new run is added
  to their range — the spawn bias is absent at one process, so discarding four runs of
  evidence for one sample would be a loss.
- Every restated figure carries its cv and the ~12% between-run noise floor
  `benchmarks/CLAUDE.md` establishes. One sample supports a rank order or a ratio above
  that floor, never a point estimate.
- `benchmarks/CLAUDE.md` carries a `checkpoint_cost` finding. It has none today.
- Someone with the repo can seed a database, start the admin and page it at volume by
  following a README section, and the seeder refuses to run when the queue tables change
  shape under it.

## Non-goals

- **Publishing tuning advice.** Upstream owns process-vs-concurrency guidance.
- **A durable throughput comparison.** The durable workload's body is dominated by a
  sleep, so both `pooled_vs_split` shapes complete the same rounds in the same wall time
  and the ratio is ~1.0 by construction. That is a property of the workload. The arms
  ride along in the stage; no claim rests on their ranking.
- **Admin timings.** The pk-ordering fix is already measured at 3M runs in the commit
  that makes it: ~520k pages and 84.7 s cold before, an `Index Scan Backward` at 33
  buffers with the same latency cold or warm after. A person with a seeded corpus runs
  `EXPLAIN` by hand; a probe that seeds and measures in one process has warm buffers and
  times nothing anyone waits for.
- **A new Django project for the admin.** `tests/settings.py` installs
  `django.contrib.admin` and `tests/urls.py` mounts it, and both read `PGDATABASE` and
  `PGPORT` from the environment. The corpus is a database on the existing `db` service,
  reached by those variables. No new module, no new service, no new port.
- **A timed suspension stage.** `tests/core/test_durable.py` drives a durable sleep
  through the real sync bridge and asserts `drain()` returns the sleeping state — a
  parked bridge thread would hang that drain, so the test passing is the evidence.
- Shipping any of this. Nothing here reaches a wheel.

## What is untrue today

**Per-task counts read low above one process.** `benchmarks/CLAUDE.md` documents it:
commits are counted from after fleet readiness while runs are counted over the whole
drain, so the numerator excludes the stagger and the denominator does not. Concurrent
spawn (already on a branch) shrinks the stagger; it does not remove it, because children
still reach readiness a few hundred milliseconds apart.

**Throughput is not affected.** It is already p10-p90 trimmed on `r.completed_at`, which
absorbs the start stagger. Rate reps are windowed too — `run_rate_rep` captures its mark
after `start_workers` returns — so `latency_under_load` was never biased.

**`checkpoint_cost` has no finding.** The figure lives in a merged PR body, nowhere a
reader would look. It is the stage whose shape most resembles durable agent work — a
`ctx.step` per tool call.

**`process_scaling` mixes two effects.** Its ladder is `max(4000, 2000 * count)`, so
rungs drain 4,000-20,000 tasks and scaling is confounded with a depth penalty. The
report prints `CONFOUNDED:`. Honest, and staying that way — fixing it is a separate
design job.

## Scope

### In

**One branch, four tasks, one short run.**

1. Land the three existing commits and the flake fix; resolve the documentation the
   concurrent-spawn commit falsifies.
2. Window the per-task counts on completion time.
3. The clone seeder, its drift guard, and a README section for browsing volume.
4. One targeted run, then restate.

### Cut, with reasons

- **A separate branch per phase.** The run does not need a merged tree — it needs the
  fixed tree, and the branch is that tree. Findings are named by run label, not by
  commit, and a squash merge makes the branch SHA unreachable anyway.
- **The required-check change as a plan task.** Adding `bench_harness` to the ruleset is
  one line in the PR description after merge. The risk it was ordered around — a flaky
  run blocking the PR that fixes the flake — is one failure in about ten runs with a
  re-run button beside it.
- **An admin probe.** No success criterion consumes cold/warm milliseconds, and a probe
  that seeds then measures in the same process has warm buffers.
- **A suspension stage or test.** Covered; see Non-goals.
- **`latency_under_load` in the run.** Already windowed, so not invalidated.
- **CPU pinning.** `benchmarks/CLAUDE.md` puts median between-run cv at 4.7% and worst
  at 12.5%, and tells the reader to treat a difference under ~12% as noise. Every claim
  is a rank order or a ratio well above that floor.
- **Any new Postgres service.** Only `db_bench` is tmpfs; the plain `db` service is a
  real data directory.

### Deferred

`size_vs_depth` is built and tested but **stays out of the run** unless retention design
is imminent — it is the one stage whose answer would change a decision (whether
retention is a throughput concern or housekeeping), so it is worth ~15 minutes when
someone is about to act on it and not before.

Also deferred: retry storms; cleanup keep-up; suite speed (`bench_harness` is the
matrix's longest job at 248 s against 139 s locally); multi-queue routing, which the
archive exercised with four queues and this harness does not.

### Out of scope, needs its own spec

**A worker re-claims its own in-flight run.** Measured: at `concurrency=2`,
`claim_timeout=1`, one body held 2.5 s ran twice, concurrently, on the same worker,
`attempts=2`, reported `SUCCESSFUL`. Inside the documented at-least-once contract, so
not a contract violation — but the process is alive and holds the run in memory, so
self-redelivery buys nothing and costs a duplicate side effect. It fails open and
silently. A library change, not harness work.

## What the run settles, and who consumes it

| Question                                    | Consumer                                                              | Decision it changes                                                                                                                         |
| ------------------------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Are per-task counts exact above one process | Whoever bisects one of our own changes                                | Whether a claim-path regression can be seen at all above one worker. Today the number moves for two reasons and you cannot tell them apart. |
| What a checkpoint costs                     | Anyone choosing how granular to make `ctx.step` in a durable workflow | Step granularity. It is the harness's most transferable number to the primary use case, and it is unwritten.                                |

The connection budget — `C + 2` backends per process once a body touches the ORM — is
**already measured** on the branch being landed (`1x4` reads 2 idle / 6 working, `4x1`
reads 8 / 12, mutation-checked against an ORM-free body). It needs restating in the
docs, not re-running.

## Component: the clone seeder

**Why not enqueue.** Reaching volume one task at a time does not scale to millions.
Write template rows through the real API, drain them so finished runs exist, then clone
server-side.

**The drain is not optional.** Enqueueing writes `pending` tasks and no finished runs,
so a corpus built by enqueueing alone leaves the runs table empty and the admin's runs
changelist with nothing to page. Include at least one failing template so failed and
retried states appear.

**The drift guard is the load-bearing part.** Cloning writes queue tables directly, so
it encodes their shape; when upstream changes a column it must fail loudly rather than
write plausible-looking wrong rows. A silent drift poisons every number taken on seeded
data.

**Browsing it needs no new code.** `DJANGO_SETTINGS_MODULE=tests.settings` with
`PGDATABASE` pointed at the corpus gives `migrate`, `createsuperuser` and `runserver`
against the existing `db` service, with django-absurd's admin already mounted.

## Branch topology

**One branch**, `benchmarks__truthful-harness`, off `main`: this spec, the plan, three
landed commits, the flake fix, the windowing fix, the seeder, and the restated docs. All
`benchmarks/`, `tests/` and docs.

**`fix__admin-changelist-order-by-pk` stays its own PR and merges first.** It is the
only user-facing change in the pile — a `fix` that renders in the changelog, carrying a
semantics note that `-task_id` orders by creation where `-first_started_at` orders by
execution start, and those differ for deferred tasks.

## Testing

Every probe is driven through its command line by `tests/benchmarks` at a handful of
tasks per stage, the idiom already there. That suite is the only thing standing between
this harness and rot, and it is a required check.

These tests exist because the measurement code had real bugs — an over-offering rate
ramp, a probe that poisoned its connection, exactness claimed above one worker. A test
that executes a stage without constraining what it measures is worse than none, because
it reads as coverage. Assert the SHAPE of a result — which rows counted, which were
discarded — and never a rate, which is the machine's to decide.

## Risks

**The run is one sample on one laptop.** It supports a rank order or a ratio above the
~12% noise floor, never a point estimate. Mitigate by stamping the environment, running
on mains under `caffeinate` (this machine has slept mid-run before, and `perf_counter`
does not count the nap, so a napped arm reads fast and plausible), reporting cv per arm,
and adding to the existing ranges wherever earlier runs remain valid.

**Seeded rows are not lived-in rows.** A cloned corpus has uniform ages and a synthetic
`claimed_by` spread. Say so wherever a number rests on it.

**A window can exclude what it means to count.** The existing `since` mark filters on
`t.enqueue_at`, and saturation reps enqueue everything before the fleet starts — so
reusing it here would select no rows at all. The windowing fix must filter on completion
time and be tested against a recorded count, not a ratio.
