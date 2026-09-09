# Plan: `benchmarks/` supersedes `loadtest/`

Spec:
[`docs/specs/2026-09-09-benchmarks-supersede-loadtest.md`](../specs/2026-09-09-benchmarks-supersede-loadtest.md).

Branch `bench__durable-checkpoints` (already holds `durable_checkpoints` and
`cleanup_vs_size`). One commit per task. TDD: RED first, always.

Gate per task: `uv run pytest tests/benchmarks/<file> -q --no-cov` while iterating,
`uv run pre-commit run --all-files`, then `tox -e bench_harness` before the commit.
Coverage on `benchmarks/` is gated at 100%, so every branch needs a test or must not
exist.

## Task 1 — occupancy from Absurd's own columns

Shared by tasks 2 and 3. No new table.

**RED.** `tests/benchmarks/test_analysis.py`: hand-write run rows with known
`started_at`/`completed_at` and a known backlog window, assert the derived `mean_busy`,
`idle_slot_s` and `span_s`. Cases that must discriminate: all slots busy throughout
(idle 0); one long task with C-1 slots free while the backlog still holds work (idle =
the free slot-seconds); every task finished before the backlog emptied (idle counted
only while work remained).

**Implementation, prose.** New SQL in `analysis.py` reading run intervals for a queue
since a window start, plus a reducer turning intervals + slot count into the three
figures. Integrate idle slots only over the sub-window where unfinished tasks existed —
that qualifier IS the metric; without it idle time after the drain inflates it.

**Verify.** Unit-level assertions on the reducer through the real SQL, no mocks.

## Task 2 — `batch_barrier`

**RED.** `tests/benchmarks/test_cli.py`: drive `batch_barrier` at `--reps 1` with a tiny
backlog; assert the 8 arm names, that each arm's spec records its distribution, shape
and workload, that both distributions carry equal total service time, and that
`run_order` interleaves. `tests/benchmarks/test_report.py`: assert the rendered block
reports `idle_slot_s` per arm and the mixed-over-uniform comparison per shape.

**Implementation, prose.** Two task bodies (fast, slow) in `benchmarks/tasks.py` at a
duration the stage passes; the mixed backlog is preloaded as a spread of the two so the
slow ones are not clustered. Sizing derives the uniform length from the mixed mean so
the two distributions cost the same total service time — assert that in the test rather
than trusting arithmetic in a comment. Arms run through
`record_interleaved_measurements`. Metrics come from task 1.

**Verify.** A real run at production size on mains under `caffeinate -is`; record
`idle_slot_s` per arm in `benchmarks/CLAUDE.md`, naming the run. The barrier signature
is `mixed`/`pooled`; state whether it reproduces the ~1.09x of the last clean loadtest
run or not.

## Task 3 — `parked_runs`

**RED.** `tests/benchmarks/test_cli.py`: drive `parked_runs` tiny; assert the 4 arm
names, that the sleepers arms recorded `running_max` and `sleeping_min`, and that the
sleeper count parked is the count the spec asked for. The positive observable that
matters: `sleeping_min` equals the parked count — a broken park would leave it 0.

**Implementation, prose.** Sync and async task bodies that call `context.sleep_for` for
longer than any measured window, plus quick bodies for the drain. The stage parks N
sleepers, waits until their runs report `sleeping`, then drains the quick batch on one
worker while sampling the sleepers' run states by state on the harness's own connection.
Both arms warm up before the clock starts.

**Verify.** Real run; record the ratio and `running_max`/`sleeping_min`. Reproduce or
contradict `running_max=0`. If a sleeper is ever `running` inside the window, that is a
finding, not a broken test.

## Task 4 — `admin_at_volume`

**RED.** `tests/benchmarks/test_cli.py`: drive `admin_at_volume` against a small seed;
assert one arm per entity per applicable probe, that every arm records `result_count`
above zero, that a `state` arm exists only where the entity declares one, and that a
`deep-page` arm is absent where the table does not paginate. `test_report.py`: the block
renders wall ms, query count and the plan file names per arm.

**Implementation, prose.** Seed with `seed.py`, then drive the admin URLs through
Django's test client with `setup_test_environment()` called explicitly, reading
`TemplateResponse.context_data` for the result count. Wrap the connection to count
queries and capture the paginator `COUNT(*)` and paged `SELECT`, then
`EXPLAIN (ANALYZE, BUFFERS)` each and write the dumps beside the results file. Skip
logic per the spec: no state arm without `has_state`, no deep page without `multi_page`.

**Verify.** Real run at a seed the 4 GB tmpfs holds; record the timings and the plan
SHAPES (not the clocks) in `benchmarks/CLAUDE.md`.

## Task 5 — retire `loadtest/`

**Do last, and only once tasks 2-4 have recorded real runs.** Write the parity table
into `benchmarks/CLAUDE.md` as fact rather than intent, then say in `docs/HISTORY.md`
that `loadtest/` is superseded and name the branch its history lives on. Deleting the
remote branch is a separate outward action and needs explicit sign-off; the doc change
does not.

## Order and cost

1 -> 2 -> 3 -> 4 -> 5. Roughly 2-3 h of session each for 2-4, ~1 h for 1, minutes for 5,
plus machine time per real run. Stop after any task; each leaves the harness green and
the branch commitable.
