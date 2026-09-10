# Spec: `benchmarks/` supersedes `loadtest/`

## Goal

Close every coverage gap between `benchmarks/` and the retired `loadtest/` harness, so
`origin/worktree-load-test-harness` can be deleted without losing a measurement anyone
can re-run. Written differently is fine; same coverage is the requirement. Coverage
means the DETECTOR survives, not that the arm matrix is copied.

## Verified gap table

| `loadtest/`     | `benchmarks/` today                                                                                                              | verdict       |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `load_seed`     | `seed.py` — templates, server-side clone, `information_schema` drift                                                             | parity        |
| `load_drain`    | `worker_knobs`, `process_scaling`, `pooled_vs_split`, `poll_interval`, `sync_vs_async`, `producer_ceiling`, `latency_under_load` | beyond parity |
| `load_barrier`  | `pooled_vs_split` has the topology only                                                                                          | **gap**       |
| `load_sleepers` | nothing (grep for sleeping/suspend in `analysis.py`, `stages.py`, `measurement.py`: no hits)                                     | **gap**       |
| `load_admin`    | `serve_admin.sh` serves it; no timing arms                                                                                       | **gap**       |

`batch_barrier` first: its uniform-vs-mixed method surfaced the
`--concurrency N delivers ~1` bug fixed in PR #156, so that gap loses a DETECTOR rather
than a number.

## Stage 1: `batch_barrier`

**Question.** What a batch claim's barrier costs when a backlog's tasks differ in
length. The async loop claims `batch_size` (defaults to concurrency) tasks, launches
all, then `gather`s before claiming again — so C slots wait on the slowest of C.

**Arms: 2.** `uniform` and `mixed`, both pooled (one worker at concurrency C), same task
count and same total service time — the uniform length is the mixed mean — so they
differ in variance alone. Mixed is mostly fast tasks with slow ones spread evenly.

**Deliverable is `idle_slot_s`, not a ratio.** Idle slot-seconds integrated over the
window where the backlog STILL held work: slots available, wanted, unused. Wall clock
cannot separate "the barrier stalled claims" from "slow tasks are slow"; that qualifier
can, which is why it is the metric. CAPPED by the work waiting — three free slots with
one task waiting is one wanted slot-second a second, not three — which is how
`loadtest`'s occupancy figure counted, so the two are comparable.

**Occupancy from Absurd's own columns, no new table.** `loadtest` wrote an
`OccupancyLog` from task bodies. `r_bench.started_at`/`completed_at` already carry every
interval and `analysis.py` exists to turn those columns into metrics.

**Invalidates a rep:** an arm that never drains; the existing suspension guard.

## Stage 2: `parked_runs`

**Question.** Does a durable sleep cost a worker slot? `context.sleep_for` on a SYNC
body hops to the worker loop while the body holds a pool thread; if that thread stayed
parked, N sleepers would hold N of C slots and work behind them would starve. Closes
#112's fourth axis.

**Arms: 3.** One `control` (drain quick tasks, no sleepers) plus `sleepers_sync` and
`sleepers_async` — the same quick drain with N tasks already parked in a sleep longer
than the window. One control, because the quick tasks do not depend on the sleeper's
workload. Both workloads, because only the sync path crosses the pool thread and that is
the mechanism at risk.

**Primary metric has no clock in it.** Count the sleepers' runs by state during the
drain: `running` holds a claim and a slot, `sleeping` holds neither. `sleeping_min == N`
with `running_max == 0` answers the question outright. The `sleepers/control` elapsed
ratio is corroboration, and catches a slot released but the fleet slowed anyway.

**Invalidates a rep:** an arm that never drains; a sleeper that woke inside the window.

## Stage 3: `admin_at_volume`

**Question.** What the tasks and runs changelists cost at volume, and what plans they
get.

**Arms: tasks and runs only**, each `unfiltered`, `queue`, `state`, `last_page` — named
for what it asks for, since the page it lands on is taken off the changelist's own
paginator rather than assumed. Both live findings (#142, #241) are about these two;
`loadtest`'s own note says checkpoints, events and waits are a handful of workflow rows
each, so their arms measure nothing at volume and the `has_state`/`multi_page` skip
machinery they needed is not built.

**Three requests an arm, one of them timed.** A warm-up first, discarded, because the
first arm to touch a page otherwise pays for caches the rest find warm. Then the timed
pass, clean. Then a capture pass for the query count and the two plans that matter (the
paginator `COUNT(*)` and the paged `SELECT`), because capture means instrumentation and
`refuse_measuring_under_debug` exists to keep that out of a timed number. Plans go in
the results file as text, not into dump files beside it.

**Records per arm:** wall ms, query count, `result_count`, the two plans. Plans matter
as much as the clock: `natural_key` is a computed expression Django appends to every
changelist ordering as a pk tiebreaker, so it lands in every `Sort Key`.

**Two traps, and what they actually require.** `response.context` is populated only
under `setup_test_environment()`, so the stage reads `TemplateResponse.context_data`
instead — a `TemplateResponse` attribute that needs no test runner. And
`setup_test_environment()` itself is NOT the answer to the rest: it refuses to run
twice, so a stage calling it works under `python -m stages` and raises under pytest,
which has already called it. What it was covering for is a settings gap — no
`ALLOWED_HOSTS`, so with DEBUG off every admin request answers 400 — and that is fixed
where it belongs.

## Cross-cutting

- Every stage is a `stages.py` stage with a results file, a report block and tests, not
  a management command. `record_interleaved_measurements` already exists for arms that
  divide each other.
- Seeded volume is bounded by the server: the data directory is a 4 GB tmpfs and a
  million tasks is 1.09 GB of tables. **A stage that seeds truncates on the way out** —
  rows left behind are RAM every later stage of a run pays for, which is what killed the
  first end-to-end attempt.
- Levels are RAM rates. Ratios travel; milliseconds do not. Every finding names its run.
- A number is quotable only from a run on mains with the suspension guard armed and no
  marks on the arms it rests on.

## Deliberately cut — do not re-add without a reason

- **`sync`/`async` arms on `batch_barrier`.** The barrier is in the CLAIM loop and
  django-absurd drives the async client for every task; body type changes whether a pool
  thread is held, which is `pooled_vs_split`'s question and already answered.
- **A `split` arm on `batch_barrier`.** `idle_slot_s` already excludes "slow tasks are
  slow", so `uniform` is the only control the detector needs. Add `split` only if a
  measured number comes out ambiguous.
- **Checkpoint, event and wait admin arms**, and with them the skip conditionals.
- **`mean_busy` and `span_s`.** Not published and not computed: the reducer returns the
  one figure, and a drain's span is already in `phase_s`.

## Out of scope

- Cleanup/fleet interference (cleanup deletes terminal rows, claims select pending ones;
  the measured cost is a table scan, not contention).
- Any fix to Absurd's SQL or the SDK's batch loop — findings go to `docs/UPSTREAM.md`.
- Re-litigating what these detectors already settled (#156, #142/#273, #241).
