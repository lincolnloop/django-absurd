# Benchmarks harness — rework and consolidation

PR: https://github.com/lincolnloop/django-absurd/pull/257 Issue:
https://github.com/lincolnloop/django-absurd/issues/112

Goal: land the benchmark harness with its structure fixed and its **unvalidated claims
removed**, then consolidate the two parallel harnesses into one.

Two harnesses exist. `benchmarks/` (PR #257) measures worker knobs, throughput, latency
under paced load, producer ceiling. `loadtest/` (local, unpushed) measures the admin at
volume, slot occupancy, the batch barrier. Overlapping cores, disjoint questions.

## The core problem

Claims outran evidence. Every one of these is asserted as measured and is not:

- **"Concurrency is capacity for overlapping IO waits, not CPU parallelism, so it does
  not scale with core count."** Never measured. Every workload in BOTH harnesses is a DB
  round trip or a sleep. Nothing burns Python bytecode; nothing exercises a
  GIL-releasing C extension. The CPU half of the claim has no measurement behind it.
  Load-bearing: it is the stated reason the concurrency ladders are not host-scaled.
- **12x enqueue batching, "the largest single improvement anywhere in these
  measurements."** Its 1.00x denominator is the least stable arm in its own table —
  flagged at 39% spread on a second host, three runs out of three. On that host
  threading beat batching, 4.51x against 2.93x. The ranking is an artifact of an
  unstable denominator.
- **"Only the ratios travel."** Several did not reproduce on a second host.
- **Concurrency ladder, 16x buys 2.35x.** Measured on a no-op, which is
  claim-round-trip-bound. Measures round-trip cost, not concurrency's ceiling.
- **A–G stage lettering implies a total order.** Real shape is a partial DAG.
- **`--reps 1` records as perfectly stable** — spread `0.0`, unflagged. `measure_spread`
  assumes >=2 reps.
- **Saturation latency percentiles are published** while the same README calls them
  meaningless and says latency guidance comes only from rate mode.

Also, in this repo's own `CLAUDE.md`: bare root `uv run pytest` does NOT "collect
nothing and exit 5". It ImportErrors on `tests/conftest.py`, which imports
`django.contrib.auth.models` with no settings configured.

## Rule

A claim removed costs a reader nothing. A claim that is wrong costs them a decision.
**Delete unsupported claims, never soften them.** They come back only with a measurement
and its host context.

## Decisions

**Container-only.** No host-run path, no published database port. Everything through
compose, mirroring `examples/`. Their README's "container numbers are only comparable
with container numbers" caveat stops being a footnote and becomes uniformly true — one
measurement regime instead of two.

**Standalone project.** `benchmarks/` gets its own `pyproject.toml`, `uv.lock`,
`Dockerfile`, pinned exactly, working directory `benchmarks/`. Same scaffolding as
`examples/`. Deterministic versions, `uv sync --locked` fails the build on drift.
Renovate must re-lock it or every root bump stalls auto-merge.

**`DATABASE_URL`, not `PG*`.** Managed platforms rotate credentials inside the URL, so
`PG*` vars go stale. `PGPORT_BENCH` deleted outright.

**Stages are a DAG, not a sequence.** Letters out, descriptive names in, dependency
graph explicit:

```
worker_knobs          -> none
process_scaling       -> worker_knobs
poll_interval         -> worker_knobs
sync_vs_async         -> none
checkpoint_cost       -> worker_knobs
producer_ceiling      -> none
latency_under_load    -> process_scaling
concurrency_vs_processes -> none          (new, phase 3)
```

Default invocation runs all of them in topological order. Naming a stage narrows.

**Entrypoint-only tests.** No unit tests on internal helpers. Everything through the
public entrypoint at small sizes (1-10 tasks) so CI stays fast. Direct size flags
(`--tasks`, `--duration`), not a scale multiplier — the saturation/rate split stays
explicit rather than hidden behind one number.

**Coverage applies.** Once the entrypoint tests exercise every path, the harness holds
the same 100% bar as the rest of the repo, with its own flag. No carve-out.

**Two databases.** The admin probe needs millions of rows resident and permanent. The
stages need a quiet, predictable server. Same instance means every stage measures
against a server holding a multi-million-row dataset — buffer pressure and autovacuum
churn, a bigger lever than the server-uptime effect already documented. Separate
service, separate volume.

**The admin probe is not a stage.** Different database, no workers, no queue draining,
different artifacts (query plans in a directory), and its prerequisite is a seed rather
than another stage's results. Folding it in would make bare `stages` require an
hour-long seed. It reuses the measurement core — host context, suspension guard, reps,
median, flags — and not the stage-runner shell. That reuse is what makes it one harness;
sharing a CLI is not.

## What survives from `loadtest/`

Kept: the admin probe and its seeder (the ordering decision in #142 is made and
unimplemented, and needs a before/after at volume), the schema drift guard, the results
conventions, the two-clock suspension guard, the migration-state refusal.

Kept, reversing an earlier call: the **pooled/split apparatus**. The batch-barrier
hypothesis was answered and shipped, but pooled (1 worker × concurrency C) against split
(C workers × concurrency 1) **at equal total slots** is the instrument that grounds
every worker-scaling claim. `process_scaling` structurally cannot: it varies worker
count at the winning concurrency, so total slots grow with workers and the comparison
never isolates.

Dropped: the sleeper probe (answered), the drain matrix (overlaps `worker_knobs` and
`process_scaling`, and those measure it better from server-side columns), and both
models — the seeder's template tasks are the only reason they exist, so a seeder whose
tasks write nothing leaves the harness model-free.

## Upstream grounding

Absurd's Python SDK, "Starting a Worker":

- "Set concurrency > 1 to execute sync handlers in a worker thread pool."
- "The worker refills free slots as tasks finish, so one slow task does not block new
  claims while capacity remains."
- "the recommended way to scale workers is to spawn multiple processes, regardless of if
  you are using a sync or async worker setup."

No numeric guidance exists upstream for either knob — the SDK examples show
`concurrency=1` (sync) and `concurrency=4` (async) with no rationale.

**Upstream docs bug:** the refill sentence holds for the sync worker only.
`django_absurd/worker.py` exists because the async `start_worker` awaits its whole
claimed batch before claiming again; both loops are ported from absurd PR #137.
Candidate upstream issue; belongs in the gaps ledger.

## Non-goals

- Running benchmark stages in CI. 75 minutes, shared vCPU, and the harness's own finding
  (15-47% swing from database uptime alone) rules out numbers as a gate. Only the
  entrypoint tests run there.
- Tuning anything. The harness measures; it ships no production code.
- Publishing a user-facing performance page. The guide is not useful in a user guide;
  concrete flag guidance belongs in the worker docs, methodology in the harness README.
