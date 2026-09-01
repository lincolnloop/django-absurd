# Benchmarks harness — rework and consolidation

PR: https://github.com/lincolnloop/django-absurd/pull/257 Issue:
https://github.com/lincolnloop/django-absurd/issues/112

Goal: land the benchmark harness with its structure fixed and its **unsupported claims
removed**, then consolidate the two parallel harnesses into one.

Two harnesses exist. `benchmarks/` (PR #257) measures worker knobs, throughput, latency
under paced load, producer ceiling. `loadtest/` (branch `worktree-load-test-harness`,
pushed, unreviewed) measures the admin at volume, slot occupancy, the batch barrier.
Overlapping cores, disjoint questions.

## The core problem

Four categories, deliberately separated — lumping them under one accusation overstates
the case.

### A. Asserted as measured, never measured

- **"Concurrency is capacity for overlapping IO waits, not CPU parallelism, so it does
  not scale with core count."** Every workload in BOTH harnesses is a database round
  trip or a sleep. Nothing burns Python bytecode; nothing exercises a GIL-releasing C
  extension. The CPU half of the claim has no measurement behind it. Load-bearing: it is
  the stated reason the concurrency ladders are not host-scaled.
- **"Only the ratios travel."** Presented as a general property. One ratio is known to
  have failed on a second host; the rest of the generalisation is untested.

### B. Failed to replicate

- **12x enqueue batching, "the largest single improvement anywhere in these
  measurements."** On a second host the ordering inverted: threading reached 3501
  enqueues/s against batching's 2277. Batching's absolute rate nearly matched the
  reference host (2277 against 2398); the single-connection baseline did not (776
  against 199), and that baseline is what the 12x divides by. That baseline was also
  flagged at 39% spread on the second host.

  Honest statement: **the ranking failed to replicate.** Not "the ranking is an
  artifact" — the reference host's own baseline stability is unknown, since its results
  are git-ignored and absent. And any ratio quoted against that flagged denominator
  repeats the error it diagnoses; compare absolute rates instead.

### C. Defects

- **`--reps 1` records as perfectly stable** — spread `0.0`, unflagged, no warning in
  the report. Two independent implementations have it: the shared spread helper and the
  producer stage's own summariser. The current smoke test asserts the unflagged result,
  so it depends on the bug.
- **Saturation latency percentiles are published** — in the report table and on the
  console — while the same README calls them meaningless and says latency guidance comes
  only from rate mode.

### D. Structure, not claims

- **A–G lettering implies a total order.** The README does state the real dependencies
  twice, so this is a legibility problem rather than a false claim. The letters are also
  load-bearing in code: the report dispatches on them.

### Not a problem after checking

The concurrency ladder's 16x → 2.35x is already qualified in place as round-trip-bound,
and a separate stage measured IO-bound ladders at three concurrencies. The number is
measured and contextualised. It needs its framing tightened, not deleting.

### Elsewhere

This repo's own `CLAUDE.md` says a bare root `uv run pytest` "collects nothing and exits
code 5". It exits **4**, raising `ImproperlyConfigured` while importing
`django.contrib.auth.models` from the root test conftest — surfaced as a conftest import
error. Wrong since that conftest landed.

## Rule

A claim removed costs a reader nothing. A claim that is wrong costs them a decision.
**Delete unsupported claims, never soften them.** They come back only with a measurement
and its host context. This applies to this document too.

## Decisions

**Container-only.** No host-run path, no published database port. Everything through
compose, mirroring `examples/`. Their README's "container numbers are only comparable
with container numbers" caveat stops being a footnote and becomes uniformly true — one
measurement regime instead of two.

Consequence: **the cold-start capability cannot be a driver flag.** Restarting the
database is a host operation; a driver inside the runner container has no docker socket.
Mounting one to get a flag is not worth it. The capability survives as a documented
two-command sequence per stage, and the shell script goes.

**Standalone project.** `benchmarks/` gets its own `pyproject.toml`, `uv.lock`,
`Dockerfile`, pinned exactly, working directory `benchmarks/`. Same scaffolding as
`examples/`. Deterministic versions, `uv sync --locked` fails the build on drift.
Renovate must re-lock it or every root bump stalls auto-merge.

**`DATABASE_URL`, not `PG*`.** One variable instead of five, and it is what the examples
already do. Note the two places the database reaches code: settings, and the environment
handed to worker subprocesses — the second currently serialises `PG*` names and must
move with the first, or children silently connect to the persistent database instead of
the test one.

**Stages are a DAG, not a sequence.** Letters out, descriptive names in, dependency
graph explicit:

```
worker_knobs             -> none
process_scaling          -> worker_knobs
poll_interval            -> worker_knobs
sync_vs_async            -> none
checkpoint_cost          -> worker_knobs
producer_ceiling         -> none
latency_under_load       -> process_scaling
concurrency_vs_processes -> none          (new, phase 3)
```

Default invocation runs all of them in topological order. Naming a stage narrows to it,
and a named stage whose prerequisite is missing still refuses rather than running it.

**Entrypoint-only tests, happy path first.** No unit tests on internal helpers.
Everything through the public entrypoint at small sizes so CI stays fast. Direct size
flags, not a scale multiplier — the saturation/rate split stays explicit rather than
hidden behind one number.

As implemented this is a default, not an absolute: ten of the 38 tests enter below the
entrypoint, either because no CLI input reaches the failure they exercise or because the
smallest stage that would is six measurements to observe one. Each says so where it
sits; the plan's §1.9 lists them.

**Coverage: 100%, reached in that order.** Cover the happy path through the entrypoint
first, then triage whatever remains. Each leftover branch is reachable with more
entrypoint effort, dead and deleted, or a guard whose contract should be asserted
directly. No carve-out declared in advance — an unreachable line is a design signal, and
deciding that before looking is how a carve-out becomes permanent. Note the harness
executes task bodies only in subprocesses, so subprocess coverage must be configured or
those read zero.

**Two databases.** The admin probe needs millions of rows resident and permanent; the
stages need a quiet, predictable server. Sharing an instance means every stage measures
against a server holding that dataset. Whether that outweighs the documented
server-uptime effect is unmeasured — the point is that it is an uncontrolled variable
introduced for no benefit, when a second service costs one compose stanza.

**The admin probe is not a stage.** Different database, no workers, no queue draining,
different artifacts (query plans in a directory), and its prerequisite is a seed rather
than another stage's results. Folding it in would make a bare run require an hour-long
seed. It reuses the measurement core — host context, suspension guard, reps, median,
flags — and not the stage-runner shell. That reuse is what makes it one harness; sharing
a CLI is not.

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

Kept as a consequence: the **occupancy model**. It is not seeder scaffolding — the
pooled/split arms reconstruct their timelines, idle-slot integrals and ramp from its
intervals. Only the execution-log model is seeder-only, and it goes once the seeder's
template tasks stop writing rows. Rebuilding occupancy from server-side claim columns
instead is plausible but is design work, not a port, and is not assumed here.

Dropped: the sleeper probe (answered), the drain matrix (overlaps `worker_knobs` and
`process_scaling`, which measure it better from server-side columns).

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
- Publishing a user-facing performance page. The guide is not useful in a user guide.
  Flag guidance belongs in the worker docs and methodology in the harness README — but
  the supported findings must land somewhere before the page goes, not vanish with it.
