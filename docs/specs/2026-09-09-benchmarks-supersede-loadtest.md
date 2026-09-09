# Spec: `benchmarks/` supersedes `loadtest/`

## Goal

Close every coverage gap between `benchmarks/` and the retired `loadtest/` harness, so
`origin/worktree-load-test-harness` can be deleted without losing a measurement anyone
can re-run. Written differently is fine; same coverage is the requirement.

## Verified gap table

| `loadtest/`     | `benchmarks/` today                                                                                                              | verdict       |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `load_seed`     | `seed.py` — templates, server-side clone, `information_schema` drift                                                             | parity        |
| `load_drain`    | `worker_knobs`, `process_scaling`, `pooled_vs_split`, `poll_interval`, `sync_vs_async`, `producer_ceiling`, `latency_under_load` | beyond parity |
| `load_barrier`  | `pooled_vs_split` has the topology only                                                                                          | **gap**       |
| `load_sleepers` | nothing (`grep` for sleeping/suspend in `analysis.py`, `stages.py`, `measurement.py`: no hits)                                   | **gap**       |
| `load_admin`    | `serve_admin.sh` serves it; no timing arms                                                                                       | **gap**       |

Three stages close the three gaps. `batch_barrier` first: its uniform-vs-mixed method
surfaced the `--concurrency N delivers ~1` bug fixed in PR #156, and it is the only gap
whose absence loses a DETECTOR rather than a number.

## Stage 1: `batch_barrier`

**Question.** What a batch claim's barrier costs when a backlog's tasks differ in
length. The async loop claims `batch_size` (defaults to concurrency) tasks, launches
all, then `gather`s before claiming again — so C slots wait on the slowest of C.

**Arms.** 8, interleaved: distribution (`uniform`, `mixed`) x shape (`pooled` = 1 worker
x C, `split` = C workers x 1) x workload (`sync`, `async`). Both distributions carry the
same task count AND the same total service time — uniform length is the mixed mean — so
they differ in variance alone. Mixed = mostly fast tasks with slow ones spread evenly.

**Metric is NOT a throughput ratio.** Wall clock cannot separate "the barrier stalled
claims" from "slow tasks are slow". Deliverable is `idle_slot_s`: idle slot-seconds
integrated over the window where the backlog STILL held work — slots available, wanted,
unused. Plus `mean_busy` and `span_s`.

**Occupancy from Absurd's own columns, no new table.** `loadtest` wrote an
`OccupancyLog` from task bodies. `r_bench.started_at`/`completed_at` already carry every
interval, and `analysis.py` exists to turn those columns into metrics. Derive the
timeline from them; add no task-side bookkeeping.

**Invalidates a rep:** an arm that never drains, and the existing suspension guard.

## Stage 2: `parked_runs`

**Question.** Does a durable sleep cost a worker slot? `context.sleep_for` on a SYNC
body hops to the worker loop while the body holds a pool thread; if that thread stayed
parked, N sleepers would hold N of C slots and work behind them would starve. Async
measured beside it because only the sync path crosses the pool. Closes #112's fourth
axis.

**Arms.** workload (`sync`, `async`) x (`control`, `sleepers`), one at a time so they
never measure each other. `control` drains quick tasks on one worker. `sleepers` drains
the same quick tasks with N tasks already parked in a sleep far longer than the window.
Both pay the same worker start-up and the same warm-up task before the clock starts.

**Two metrics, one clock-free.** Ratio `sleepers/control` elapsed: near 1 means the
sleep released its slot. Corroborated by counting the sleepers' run states DURING the
drain — `running` holds a claim and a slot, `sleeping` holds neither, so `running_max`
and `sleeping_min` answer the same question with no clock in it. Prior evidence to
reproduce: `running_max=0`, `sleeping_min=N`.

**Invalidates a rep:** an arm that never drains; a sleeper that woke inside the window.

## Stage 3: `admin_at_volume`

**Question.** What the Absurd admin changelists cost at volume, and what plans they get.

**Arms per entity:** `unfiltered`, `queue` filter, `state` filter, `deep-page`. Drive
the REAL admin through Django's test client — same middleware, same `ChangeList`.

**Records per arm:** wall ms, query count, `result_count`, and
`EXPLAIN (ANALYZE, BUFFERS)` for the paginator `COUNT(*)` and the paged `SELECT`. Plans
matter as much as the clock: `natural_key` is a computed expression Django appends to
every changelist ordering as pk tiebreaker, so it lands in every `Sort Key`.

**Skips are mandatory, not optional.** A `state` arm only where the entity declares one
(`EntitySpec.has_state`); a `deep-page` arm only where the table paginates —
`ChangeList.get_results` ignores `?p=` unless `multi_page`, so a deep page on a small
table renders page 1 and answers 200 while measuring nothing. Every arm records
`result_count` so it says how many rows it measured rather than leaving it inferred.

**Trap to respect.** `response.context` is populated only under
`setup_test_environment()`; a stage driving the client outside pytest must read
`TemplateResponse.context_data`. That mistake passed a whole suite and crashed on the
first real run.

## Cross-cutting

- Every stage is a `stages.py` stage with a results file, a report block, and tests —
  not a management command. `record_interleaved_measurements` already exists for arms
  that divide each other.
- Seeded volume is bounded by the server: data directory is a 4 GB tmpfs and a million
  tasks is 1.09 GB of tables. `admin_at_volume` sizes to that or raises
  `BENCH_TMPFS_SIZE` explicitly.
- Levels are RAM rates. Ratios travel; milliseconds do not. Every finding names its run.
- A number is quotable only from a run on mains power with the suspension guard armed
  and no marks on the arms it rests on.

## Out of scope

- Cleanup/fleet interference (decided against: cleanup deletes terminal rows, claims
  select pending ones; the measured cost is a table scan, not contention).
- Any fix to Absurd's SQL or the SDK's batch loop. Findings go to `docs/UPSTREAM.md`.
- Re-litigating questions these detectors already answered (#156, #142/#273, #241).
